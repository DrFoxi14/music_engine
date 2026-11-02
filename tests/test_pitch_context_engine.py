"""Integration tests for the pitch-context C++ engine."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from pitch_context_client import PitchContextClient

ROOT = Path(__file__).resolve().parents[1]


def note(
    note_id, x, y, stem=None, chord=None, tie_id=None,
    tie_from_previous=False, tie_to_next=False,
):
    return {
        "id": note_id, "x": x, "y": y, "confidence": 0.99,
        "bbox": {"left": x - 10, "top": y - 5, "right": x + 10, "bottom": y + 5},
        "stem_id": stem, "chord_id": chord,
        "tie_id": tie_id,
        "tie_from_previous": tie_from_previous,
        "tie_to_next": tie_to_next,
    }


class PitchContextEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temp.name) / "pitch_context_engine"
        compiler = "clang++"
        if subprocess.run(["which", compiler], capture_output=True).returncode != 0:
            compiler = "g++"
        subprocess.run(
            [compiler, "-std=c++17", "-O2", str(ROOT / "pitch_context_engine.cpp"), "-o", str(cls.binary)],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.engine = PitchContextClient(self.binary)

    def tearDown(self):
        self.engine.close()

    def resolve(self, notes, **kwargs):
        return self.engine.resolve(
            clef="treble", staff_top_y=100, line_spacing=10,
            notes=notes, **kwargs
        )

    def test_treble_bottom_line_maps_to_e4(self):
        pitch = self.resolve([note("n1", 200, 140)])["notes"][0]
        self.assertEqual((pitch["letter"], pitch["octave"]), ("E", 4))
        self.assertEqual(pitch["midi_pitch"], 64)
        self.assertTrue(pitch["on_staff_grid"])

    def test_key_signature_applies_to_pitch_letter(self):
        pitch = self.resolve([note("f5", 200, 100)], key_signature="G")["notes"][0]
        self.assertEqual(pitch["letter"], "F")
        self.assertEqual(pitch["key_signature_alteration"], 1)
        self.assertEqual(pitch["effective_alteration"], 1)
        self.assertEqual(pitch["midi_pitch"], 78)

    def test_local_accidental_persists_same_letter_and_octave(self):
        result = self.resolve(
            [note("d4a", 200, 145), note("d4b", 300, 145), note("d5", 400, 110)],
            accidentals=[{
                "id": "sharp1", "kind": "sharp",
                "bbox": {"left": 170, "top": 141, "right": 182, "bottom": 149},
            }],
        )
        pitches = {p["id"]: p for p in result["notes"]}
        self.assertEqual(pitches["d4a"]["effective_alteration"], 1)
        self.assertEqual(pitches["d4b"]["effective_alteration"], 1)
        self.assertEqual(pitches["d4b"]["alteration_source"], "measure_local_memory")
        self.assertEqual(pitches["d5"]["effective_alteration"], 0)

    def test_accidental_memory_resets_on_next_measure_call(self):
        first = self.resolve(
            [note("d4", 200, 145)],
            accidentals=[{
                "id": "sharp1", "kind": "sharp",
                "bbox": {"left": 170, "top": 141, "right": 182, "bottom": 149},
            }],
            measure_id="m1",
        )
        second = self.resolve([note("d4", 200, 145)], measure_id="m2")
        self.assertEqual(first["notes"][0]["effective_alteration"], 1)
        self.assertEqual(second["notes"][0]["effective_alteration"], 0)

    def test_accidental_overlapping_two_note_positions_is_ambiguous(self):
        result = self.resolve(
            [note("b4", 200, 120), note("a4", 200, 125)],
            accidentals=[{
                "id": "uncertain_sharp", "kind": "sharp",
                "bbox": {"left": 170, "top": 118, "right": 182, "bottom": 127},
            }],
        )
        association = result["accidental_associations"][0]
        self.assertEqual(association["status"], "ambiguous")
        self.assertEqual({c["note_id"] for c in association["candidates"]}, {"b4", "a4"})
        self.assertEqual(result["status"], "review_required")
        self.assertIn("uncertain_sharp", result["notes"][0]["ambiguous_accidentals"])
        self.assertIn(1, result["notes"][0]["effective_alteration_options"])
        self.assertIn(0, result["notes"][0]["effective_alteration_options"])

    def test_visual_anchor_y_overrides_bbox_center(self):
        result = self.resolve(
            [note("b4", 200, 120), note("a4", 200, 125)],
            accidentals=[{
                "id": "anchored_sharp", "kind": "sharp",
                "bbox": {"left": 170, "top": 116, "right": 182, "bottom": 130},
                "anchor_y": 120,
            }],
        )
        association = result["accidental_associations"][0]
        self.assertEqual(association["status"], "associated")
        self.assertEqual(association["selected_note_id"], "b4")
        self.assertEqual(association["anchor_source"], "visual_anchor")

    def test_accidental_assigned_when_vertical_margin_is_clear(self):
        result = self.resolve(
            [note("b4", 200, 120), note("a4", 200, 125)],
            accidentals=[{
                "id": "sharp_b4", "kind": "sharp",
                "bbox": {"left": 170, "top": 118, "right": 182, "bottom": 122},
            }],
        )
        association = result["accidental_associations"][0]
        self.assertEqual(association["status"], "associated")
        self.assertEqual(association["selected_note_id"], "b4")
        self.assertEqual(result["notes"][0]["effective_alteration"], 1)

    def test_natural_cancels_key_signature_for_remainder_of_measure(self):
        result = self.resolve(
            [note("f5a", 200, 100), note("f5b", 300, 100)],
            key_signature="G",
            accidentals=[{
                "id": "natural1", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        pitches = {p["id"]: p for p in result["notes"]}
        self.assertEqual(pitches["f5a"]["key_signature_alteration"], 1)
        self.assertEqual(pitches["f5a"]["effective_alteration"], 0)
        self.assertEqual(pitches["f5b"]["effective_alteration"], 0)

    def test_chord_interval_diagnostic_is_advisory_not_rejection(self):
        result = self.resolve(
            [
                note("c4", 200, 150, chord="ch1"),
                note("d4", 200, 145, chord="ch1"),
            ],
            accidentals=[{
                "id": "flat_d4", "kind": "flat",
                "bbox": {"left": 170, "top": 141, "right": 182, "bottom": 149},
            }],
        )
        chord = next(item for item in result["chord_diagnostics"] if item["chord_id"] == "ch1")
        self.assertEqual(chord["status"], "advisory_only")
        self.assertEqual(chord["intervals"][0]["interval_class"], 1)
        self.assertIn("semitone_cluster", chord["intervals"][0]["flag"])
        guidance = next(item for item in result["visual_guidance"] if item["target"] == "ch1")
        self.assertTrue(guidance["must_not_auto_correct"])
        self.assertEqual(result["status"], "resolved")

    def test_shared_up_stem_anchors_at_lower_chord_notehead(self):
        result = self.resolve(
            [
                note("upper", 200, 110, stem="s1", chord="ch1"),
                note("lower", 200, 120, stem="s1", chord="ch1"),
            ],
            stems=[{
                "id": "s1", "chord_id": "ch1", "x": 210,
                "y_top": 70, "y_bottom": 120, "direction": "up",
            }],
        )
        stem = result["stem_checks"][0]
        self.assertEqual(stem["status"], "attached")
        self.assertEqual(stem["anchor_note_id"], "lower")
        self.assertEqual(stem["attached_note_count"], 2)

    def test_chord_members_with_multiple_stems_request_review(self):
        result = self.resolve(
            [
                note("upper", 200, 110, stem="s1", chord="ch1"),
                note("lower", 200, 120, stem="s2", chord="ch1"),
            ],
            stems=[
                {"id": "s1", "chord_id": "ch1", "x": 210, "y_top": 70, "y_bottom": 110, "direction": "up"},
                {"id": "s2", "chord_id": "ch1", "x": 210, "y_top": 80, "y_bottom": 120, "direction": "up"},
            ],
        )
        chord = result["chord_stem_checks"][0]
        self.assertEqual(chord["status"], "multiple_stems_needs_review")
        self.assertEqual(result["status"], "review_required")

    def test_missing_stem_reference_requests_visual_guidance(self):
        result = self.resolve([
            note("n1", 200, 120, stem="not_detected", chord="ch1")
        ])
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(result["visual_guidance"][0]["reason"], "missing_stem_reference")

    def test_invalid_stem_attachment_requests_review(self):
        result = self.resolve(
            [note("n1", 200, 120, stem="s1", chord="ch1")],
            stems=[{
                "id": "s1", "chord_id": "ch1", "x": 230,
                "y_top": 70, "y_bottom": 120, "direction": "up",
            }],
        )
        self.assertEqual(result["stem_checks"][0]["status"], "needs_visual_review")
        self.assertEqual(result["status"], "review_required")

    def test_skewed_staff_geometry_is_compensated(self):
        result = self.engine.resolve(
            clef="treble", staff_top_y=100, line_spacing=10,
            origin_x=0, staff_slope=0.01,
            notes=[note("f5", 1000, 110)],
        )
        self.assertTrue(result["notes"][0]["on_staff_grid"])
        self.assertEqual((result["notes"][0]["letter"], result["notes"][0]["octave"]), ("F", 5))

    def test_cross_barline_tie_carries_accidental_only_for_tied_note(self):
        first = self.resolve(
            [note("f-natural-source", 200, 100, tie_id="tie-f", tie_to_next=True)],
            key_signature="G",
            measure_id="m1",
            staff_id="treble-1",
            voice_id="soprano",
            accidentals=[{
                "id": "natural-f", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        self.assertEqual(first["notes"][0]["effective_alteration"], 0)
        self.assertEqual(first["notes"][0]["tie_status"], "pending_next_measure")

        second = self.resolve(
            [
                note("f-natural-target", 200, 100, tie_id="tie-f", tie_from_previous=True),
                note("f-sharp-independent", 300, 100),
            ],
            key_signature="G",
            measure_id="m2",
            staff_id="treble-1",
            voice_id="soprano",
        )
        pitches = {item["id"]: item for item in second["notes"]}
        tied = pitches["f-natural-target"]
        independent = pitches["f-sharp-independent"]
        self.assertEqual(tied["effective_alteration"], 0)
        self.assertEqual(tied["alteration_source"], "tie_carry")
        self.assertEqual(tied["tie_status"], "continued")
        self.assertEqual(tied["tie_source_note_id"], "f-natural-source")
        self.assertEqual(independent["effective_alteration"], 1)
        self.assertEqual(independent["alteration_source"], "key_signature")
        self.assertEqual(second["tie_events"][0]["status"], "continued")

    def test_tie_chain_relays_carried_pitch_across_multiple_bars(self):
        first = self.resolve(
            [note("source", 200, 100, tie_id="chain-1", tie_to_next=True)],
            key_signature="G", measure_id="m1",
            accidentals=[{
                "id": "natural-f", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        second = self.resolve(
            [note("middle", 200, 100, tie_id="chain-1",
                  tie_from_previous=True, tie_to_next=True)],
            key_signature="G", measure_id="m2",
        )
        third = self.resolve(
            [note("end", 200, 100, tie_id="chain-1", tie_from_previous=True)],
            key_signature="G", measure_id="m3",
        )
        self.assertEqual(first["notes"][0]["effective_alteration"], 0)
        self.assertEqual(second["notes"][0]["effective_alteration"], 0)
        self.assertEqual(second["notes"][0]["tie_status"], "continued_and_relayed")
        self.assertEqual(third["notes"][0]["effective_alteration"], 0)
        self.assertEqual(third["notes"][0]["tie_status"], "continued")

    def test_tie_state_is_isolated_by_staff_and_voice(self):
        self.resolve(
            [note("source", 200, 100, tie_id="same-tie", tie_to_next=True)],
            key_signature="G", measure_id="m1",
            staff_id="staff-A", voice_id="soprano",
            accidentals=[{
                "id": "natural-f", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        other_voice = self.resolve(
            [note("target", 200, 100, tie_id="same-tie", tie_from_previous=True)],
            key_signature="G", measure_id="m2",
            staff_id="staff-A", voice_id="alto",
        )
        target = other_voice["notes"][0]
        self.assertEqual(target["tie_status"], "missing_source")
        self.assertFalse(target["pitch_certain"])
        self.assertEqual(target["effective_alteration"], 1)

    def test_tie_target_diatonic_pitch_mismatch_is_not_carried(self):
        self.resolve(
            [note("source-f5", 200, 100, tie_id="bad-link", tie_to_next=True)],
            key_signature="G", measure_id="m1",
            accidentals=[{
                "id": "natural-f", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        second = self.resolve(
            [note("target-d5", 200, 110, tie_id="bad-link", tie_from_previous=True)],
            key_signature="G", measure_id="m2",
        )
        target = second["notes"][0]
        self.assertEqual(target["tie_status"], "pitch_mismatch")
        self.assertEqual(target["effective_alteration"], 0)
        self.assertFalse(target["pitch_certain"])
        self.assertEqual(second["status"], "review_required")

    def test_unmatched_tie_source_requests_visual_review(self):
        self.resolve(
            [note("source", 200, 100, tie_id="unmatched", tie_to_next=True)],
            key_signature="G", measure_id="m1",
        )
        second = self.resolve(
            [note("other", 200, 120)],
            key_signature="G", measure_id="m2",
        )
        self.assertEqual(second["status"], "review_required")
        self.assertTrue(any(
            event["status"] == "target_not_detected_in_next_measure"
            for event in second["tie_events"]
        ))

    def test_tie_state_can_be_explicitly_reset(self):
        self.resolve(
            [note("source", 200, 100, tie_id="reset-me", tie_to_next=True)],
            key_signature="G", measure_id="m1",
        )
        reset_result = self.engine.reset_ties(staff_id="staff0", voice_id="voice0")
        self.assertEqual(reset_result["type"], "tie_state_reset")
        target = self.resolve(
            [note("target", 200, 100, tie_id="reset-me", tie_from_previous=True)],
            key_signature="G", measure_id="m2",
        )["notes"][0]
        self.assertEqual(target["tie_status"], "missing_source")

    def test_ambiguous_accidental_on_tied_target_keeps_tie_pitch_and_requests_review(self):
        self.resolve(
            [note("source", 200, 100, tie_id="ambiguous-tie", tie_to_next=True)],
            key_signature="G", measure_id="m1",
            accidentals=[{
                "id": "natural-f", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        result = self.resolve(
            [
                note("tied-f5", 200, 100, tie_id="ambiguous-tie", tie_from_previous=True),
                note("neighbor-e5", 200, 105),
            ],
            key_signature="G", measure_id="m2",
            accidentals=[{
                "id": "ambiguous-symbol", "kind": "sharp",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 109},
            }],
        )
        tied = next(item for item in result["notes"] if item["id"] == "tied-f5")
        self.assertEqual(tied["effective_alteration"], 0)
        self.assertEqual(tied["alteration_source"], "tie_carry")
        self.assertEqual(tied["tie_status"], "ambiguous_target_accidental")
        self.assertFalse(tied["pitch_certain"])
        self.assertEqual(result["status"], "review_required")
        self.assertTrue(any(
            item["reason"] == "tie_ambiguous_target_accidental"
            for item in result["visual_guidance"]
        ))

    def test_tied_target_cautionary_accidental_must_match_carried_pitch(self):
        self.resolve(
            [note("source", 200, 100, tie_id="cautionary", tie_to_next=True)],
            key_signature="G", measure_id="m1",
            accidentals=[{
                "id": "natural-f", "kind": "natural",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        result = self.resolve(
            [note("target", 200, 100, tie_id="cautionary", tie_from_previous=True)],
            key_signature="G", measure_id="m2",
            accidentals=[{
                "id": "sharp-f", "kind": "sharp",
                "bbox": {"left": 170, "top": 96, "right": 182, "bottom": 104},
            }],
        )
        target = result["notes"][0]
        self.assertEqual(target["tie_status"], "accidental_conflict")
        self.assertEqual(target["effective_alteration"], 0)
        self.assertFalse(target["pitch_certain"])
        self.assertEqual(result["status"], "review_required")

    def test_bass_bottom_line_maps_to_g2(self):
        result = self.engine.resolve(
            clef="bass", staff_top_y=100, line_spacing=10,
            notes=[note("g2", 200, 140)],
        )
        pitch = result["notes"][0]
        self.assertEqual((pitch["letter"], pitch["octave"]), ("G", 2))
        self.assertEqual(pitch["midi_pitch"], 43)


if __name__ == "__main__":
    unittest.main()
