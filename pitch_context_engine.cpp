#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

/*
 * Pitch Context Engine — Music Engine / OMR module (C++17)
 *
 * Image-agnostic Y-axis reasoning:
 *   staff geometry + clef + key signature + measure-local accidental state
 *   -> diatonic pitch and chromatic pitch.
 *
 * It also:
 *   - associates accidental glyph bounding boxes with candidate noteheads;
 *   - preserves ambiguous accidental-to-note assignments for visual review;
 *   - tracks accidental scope for the current measure only;
 *   - checks chord stem attachment using the chord's actual attachment notehead;
 *   - returns page-coordinate guidance for the visual recognizer.
 *
 * This module does not read pixels and does not reject music because it is
 * dissonant. Geometry and notational state are hard constraints; harmony is
 * contextual evidence, never proof that a written note must be changed.
 *
 * Build:
 *   clang++ -std=c++17 -O3 -DNDEBUG pitch_context_engine.cpp -o pitch_context_engine
 *
 * One request per line:
 *   RESOLVE clef=treble top_y=100 spacing=10 origin_x=0 slope=0 key=G measure=m1 \
 *     note=n1,200,120,0.99,190,210,115,125,s1,ch1 \
 *     note=n2,200,110,0.99,190,210,105,115,s1,ch1 \
 *     accidental=a1,sharp,170,103,182,117,0.85 \
 *     stem=s1,ch1,210,80,120,up
 *   EXIT
 *
 * note fields:
 *   id,x,y,confidence,bbox_left,bbox_right,bbox_top,bbox_bottom,stem_id,chord_id
 *   Use '-' for unavailable bbox / stem / chord values.
 *
 * accidental fields:
 *   id,kind,bbox_left,bbox_top,bbox_right,bbox_bottom,confidence
 *   kind = sharp, flat, natural, double_sharp, double_flat
 *
 * stem fields:
 *   id,chord_id,x,y_top,y_bottom,direction
 *   direction = up | down
 *
 * Staff top_y is the top line at origin_x. slope is dy/dx for a skewed staff.
 * Each request describes one staff/voice and one measure. A new request starts
 * with a fresh accidental register.
 */

namespace {

std::string jsonEscape(const std::string& s) {
    std::string out;
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    const char* h = "0123456789abcdef";
                    out += "\\u00";
                    out += h[(c >> 4) & 15];
                    out += h[c & 15];
                } else out += static_cast<char>(c);
        }
    }
    return out;
}

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

std::vector<std::string> split(const std::string& s, char delimiter) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delimiter)) out.push_back(item);
    return out;
}

double parseDouble(const std::string& s, const std::string& name) {
    size_t used = 0;
    const double value = std::stod(s, &used);
    if (used != s.size() || !std::isfinite(value))
        throw std::invalid_argument("invalid " + name);
    return value;
}

std::string optionalString(const std::string& s) {
    return s == "-" ? "" : s;
}

struct Box {
    double left = 0;
    double top = 0;
    double right = 0;
    double bottom = 0;
    bool valid = false;

    double centerX() const { return (left + right) * 0.5; }
    double centerY() const { return (top + bottom) * 0.5; }
    double width() const { return right - left; }
    double height() const { return bottom - top; }
};

struct StaffConfig {
    std::string clef = "treble";
    double topY = 0;
    double spacing = 10;
    double originX = 0;
    double slope = 0;
    std::string key = "C";
    std::string measureId = "0";
    double gridTolerance = 0.28;  // fraction of one line-to-line spacing
    double associationMargin = 0.22;
    double associationMaxCost = 0.95;
};

struct NoteInput {
    std::string id;
    double x = 0;
    double y = 0;
    double confidence = 1.0;
    Box box;
    std::string stemId;
    std::string chordId;
};

struct AccidentalInput {
    std::string id;
    std::string kind;
    Box box;
    double confidence = 1.0;
};

struct StemInput {
    std::string id;
    std::string chordId;
    double x = 0;
    double top = 0;
    double bottom = 0;
    std::string direction;
};

struct PitchPosition {
    int step = 0;
    double residualPx = 0;
    bool onGrid = false;
    char letter = 'C';
    int octave = 4;
};

struct AssociationCandidate {
    size_t noteIndex = 0;
    double cost = 0;
};

struct Association {
    size_t accidentalIndex = 0;
    std::string status = "orphan";
    int selectedNote = -1;
    std::vector<AssociationCandidate> candidates;
};

struct KeySignature {
    std::array<int, 7> alteration{{0,0,0,0,0,0}}; // C D E F G A B

    int get(char letter) const {
        return alteration[static_cast<size_t>(letterIndex(letter))];
    }

    static int letterIndex(char letter) {
        switch (letter) {
            case 'C': return 0;
            case 'D': return 1;
            case 'E': return 2;
            case 'F': return 3;
            case 'G': return 4;
            case 'A': return 5;
            case 'B': return 6;
            default: throw std::invalid_argument("invalid note letter");
        }
    }
};

KeySignature parseKeySignature(std::string key) {
    key = lower(key);
    // Major and relative-minor spellings. Minor keys share their relative
    // major's key signature.
    static const std::map<std::string, int> fifths = {
        {"c",0}, {"am",0},
        {"g",1}, {"em",1},
        {"d",2}, {"bm",2},
        {"a",3}, {"f#m",3},
        {"e",4}, {"c#m",4},
        {"b",5}, {"g#m",5},
        {"f#",6}, {"d#m",6},
        {"c#",7}, {"a#m",7},
        {"f",-1}, {"dm",-1},
        {"bb",-2}, {"gm",-2},
        {"eb",-3}, {"cm",-3},
        {"ab",-4}, {"fm",-4},
        {"db",-5}, {"bbm",-5},
        {"gb",-6}, {"ebm",-6},
        {"cb",-7}, {"abm",-7}
    };

    auto it = fifths.find(key);
    if (it == fifths.end())
        throw std::invalid_argument("unsupported key signature; use C, G, D, A, E, B, F#, C#, F, Bb, Eb, Ab, Db, Gb, Cb or relative minor");

    KeySignature result;
    static const std::array<char, 7> sharpOrder{{'F','C','G','D','A','E','B'}};
    static const std::array<char, 7> flatOrder{{'B','E','A','D','G','C','F'}};
    const int count = it->second;

    if (count > 0) {
        for (int i = 0; i < count; ++i)
            result.alteration[static_cast<size_t>(KeySignature::letterIndex(sharpOrder[static_cast<size_t>(i)]))] = 1;
    } else {
        for (int i = 0; i < -count; ++i)
            result.alteration[static_cast<size_t>(KeySignature::letterIndex(flatOrder[static_cast<size_t>(i)]))] = -1;
    }
    return result;
}

int accidentalValue(const std::string& rawKind) {
    const std::string kind = lower(rawKind);
    if (kind == "sharp" || kind == "#") return 1;
    if (kind == "flat" || kind == "b") return -1;
    if (kind == "natural" || kind == "nat") return 0;
    if (kind == "double_sharp" || kind == "x") return 2;
    if (kind == "double_flat" || kind == "bb") return -2;
    throw std::invalid_argument("unknown accidental kind: " + rawKind);
}

std::string accidentalName(int value) {
    switch (value) {
        case -2: return "double_flat";
        case -1: return "flat";
        case 0: return "natural";
        case 1: return "sharp";
        case 2: return "double_sharp";
        default: return "alteration_" + std::to_string(value);
    }
}

struct ClefAnchor {
    char letter;
    int octave;
};

ClefAnchor anchorForClef(const std::string& clef) {
    const std::string value = lower(clef);
    if (value == "treble" || value == "g") return {'E', 4}; // bottom line
    if (value == "bass" || value == "f") return {'G', 2};   // bottom line
    if (value == "alto" || value == "c3") return {'F', 3};  // bottom line
    if (value == "tenor" || value == "c4") return {'D', 3}; // bottom line
    throw std::invalid_argument("clef must be treble, bass, alto or tenor");
}

int naturalSemitone(char letter) {
    switch (letter) {
        case 'C': return 0;
        case 'D': return 2;
        case 'E': return 4;
        case 'F': return 5;
        case 'G': return 7;
        case 'A': return 9;
        case 'B': return 11;
        default: throw std::invalid_argument("invalid pitch letter");
    }
}

PitchPosition mapStaffY(const StaffConfig& cfg, const NoteInput& note) {
    const ClefAnchor anchor = anchorForClef(cfg.clef);
    const int anchorIndex = KeySignature::letterIndex(anchor.letter);
    const double topAtX = cfg.topY + cfg.slope * (note.x - cfg.originX);
    const double bottomLineY = topAtX + 4.0 * cfg.spacing;
    const double stepHeight = cfg.spacing / 2.0;
    const double rawStep = (bottomLineY - note.y) / stepHeight;
    const long roundedStep = std::lround(rawStep);
    const double residual = std::abs(rawStep - static_cast<double>(roundedStep)) * stepHeight;

    int absoluteLetter = anchorIndex + static_cast<int>(roundedStep);
    int octave = anchor.octave;
    while (absoluteLetter < 0) {
        absoluteLetter += 7;
        --octave;
    }
    while (absoluteLetter >= 7) {
        absoluteLetter -= 7;
        ++octave;
    }
    static const std::array<char, 7> letters{{'C','D','E','F','G','A','B'}};

    PitchPosition result;
    result.step = static_cast<int>(roundedStep);
    result.residualPx = residual;
    result.onGrid = residual <= cfg.gridTolerance * cfg.spacing;
    result.letter = letters[static_cast<size_t>(absoluteLetter)];
    result.octave = octave;
    return result;
}

std::string pitchKey(char letter, int octave) {
    return std::string(1, letter) + std::to_string(octave);
}

int midiPitch(char letter, int octave, int alteration) {
    return (octave + 1) * 12 + naturalSemitone(letter) + alteration;
}

std::vector<std::string> splitCsv(const std::string& value, size_t expected, const std::string& label) {
    auto fields = split(value, ',');
    if (fields.size() != expected)
        throw std::invalid_argument(label + " expects " + std::to_string(expected) + " comma-separated fields");
    return fields;
}

NoteInput parseNote(const std::string& value) {
    auto f = splitCsv(value, 10, "note");
    NoteInput n;
    n.id = f[0];
    n.x = parseDouble(f[1], "note x");
    n.y = parseDouble(f[2], "note y");
    n.confidence = parseDouble(f[3], "note confidence");
    if (n.confidence <= 0 || n.confidence > 1) throw std::invalid_argument("note confidence must be in (0,1]");
    if (f[4] != "-" && f[5] != "-" && f[6] != "-" && f[7] != "-") {
        n.box = Box{parseDouble(f[4], "note bbox left"), parseDouble(f[6], "note bbox top"),
                    parseDouble(f[5], "note bbox right"), parseDouble(f[7], "note bbox bottom"), true};
        if (n.box.right <= n.box.left || n.box.bottom <= n.box.top)
            throw std::invalid_argument("note bounding box has non-positive size");
    }
    n.stemId = optionalString(f[8]);
    n.chordId = optionalString(f[9]);
    return n;
}

AccidentalInput parseAccidental(const std::string& value) {
    auto f = splitCsv(value, 7, "accidental");
    AccidentalInput a;
    a.id = f[0];
    a.kind = lower(f[1]);
    (void)accidentalValue(a.kind);
    a.box = Box{parseDouble(f[2], "accidental bbox left"), parseDouble(f[3], "accidental bbox top"),
                parseDouble(f[4], "accidental bbox right"), parseDouble(f[5], "accidental bbox bottom"), true};
    if (a.box.right <= a.box.left || a.box.bottom <= a.box.top)
        throw std::invalid_argument("accidental bounding box has non-positive size");
    a.confidence = parseDouble(f[6], "accidental confidence");
    if (a.confidence <= 0 || a.confidence > 1)
        throw std::invalid_argument("accidental confidence must be in (0,1]");
    return a;
}

StemInput parseStem(const std::string& value) {
    auto f = splitCsv(value, 6, "stem");
    StemInput s;
    s.id = f[0];
    s.chordId = f[1];
    s.x = parseDouble(f[2], "stem x");
    s.top = parseDouble(f[3], "stem top");
    s.bottom = parseDouble(f[4], "stem bottom");
    s.direction = lower(f[5]);
    if (s.direction != "up" && s.direction != "down")
        throw std::invalid_argument("stem direction must be up or down");
    if (s.bottom < s.top) std::swap(s.top, s.bottom);
    return s;
}

double accidentalAssociationCost(const StaffConfig& cfg, const AccidentalInput& accidental,
                                 const NoteInput& note) {
    const double gap = (note.box.valid ? note.box.left : note.x) - accidental.box.right;
    const double maxGap = 5.0 * cfg.spacing;
    if (gap < -0.15 * cfg.spacing || gap > maxGap) return std::numeric_limits<double>::infinity();

    const double verticalDistance = std::abs(note.y - accidental.box.centerY());
    const double verticalAllowance = std::max(0.85 * cfg.spacing, 0.58 * accidental.box.height());
    if (verticalDistance > verticalAllowance) return std::numeric_limits<double>::infinity();

    // Prefer vertical alignment strongly. Horizontal proximity is a weaker
    // tie-breaker because accidentals may be displaced to avoid collisions.
    return verticalDistance / cfg.spacing + 0.08 * std::max(0.0, gap) / maxGap;
}

Association associateAccidental(size_t accidentalIndex, const StaffConfig& cfg,
                                const AccidentalInput& accidental,
                                const std::vector<NoteInput>& notes) {
    Association result;
    result.accidentalIndex = accidentalIndex;

    for (size_t i = 0; i < notes.size(); ++i) {
        const double cost = accidentalAssociationCost(cfg, accidental, notes[i]);
        if (std::isfinite(cost)) result.candidates.push_back({i, cost});
    }

    std::stable_sort(result.candidates.begin(), result.candidates.end(),
        [](const AssociationCandidate& a, const AssociationCandidate& b) {
            return a.cost < b.cost;
        });

    if (result.candidates.empty()) {
        result.status = "orphan";
        return result;
    }

    const double best = result.candidates.front().cost;
    const double second = result.candidates.size() > 1
        ? result.candidates[1].cost : std::numeric_limits<double>::infinity();

    if (best > cfg.associationMaxCost) {
        result.status = "orphan";
    } else if (result.candidates.size() == 1 ||
               second - best >= cfg.associationMargin) {
        result.status = "associated";
        result.selectedNote = static_cast<int>(result.candidates.front().noteIndex);
    } else {
        result.status = "ambiguous";
    }
    return result;
}

struct StemCheck {
    const StemInput* stem = nullptr;
    std::vector<size_t> notes;
    int anchorNote = -1;
    double horizontalError = 0;
    bool hasBBox = false;
    bool attached = false;
    std::string status = "unverified";
};

StemCheck checkStem(const StemInput& stem, const std::vector<NoteInput>& notes,
                    const std::vector<size_t>& noteIndices, double spacing) {
    StemCheck result;
    result.stem = &stem;
    result.notes = noteIndices;
    if (noteIndices.empty()) {
        result.status = "orphan_stem";
        return result;
    }

    size_t anchor = noteIndices.front();
    for (size_t index : noteIndices) {
        if (!notes[index].box.valid) continue;
        if (result.anchorNote < 0) {
            anchor = index;
            result.anchorNote = static_cast<int>(index);
            continue;
        }
        // Up stems attach to the lower notehead in a chord (largest image Y);
        // down stems attach to the upper notehead (smallest image Y).
        if ((stem.direction == "up" && notes[index].y > notes[anchor].y) ||
            (stem.direction == "down" && notes[index].y < notes[anchor].y)) {
            anchor = index;
            result.anchorNote = static_cast<int>(index);
        }
    }

    if (result.anchorNote < 0 || !notes[anchor].box.valid) {
        result.status = "unverified";
        return result;
    }

    result.hasBBox = true;
    const Box& box = notes[anchor].box;
    const double expectedX = stem.direction == "up" ? box.right : box.left;
    result.horizontalError = std::abs(stem.x - expectedX);

    const double attachmentY = stem.direction == "up" ? stem.bottom : stem.top;
    const bool yTouches = attachmentY >= box.top - 0.5 * spacing &&
                          attachmentY <= box.bottom + 0.5 * spacing;
    result.attached = result.horizontalError <= 0.45 * spacing && yTouches;
    result.status = result.attached ? "attached" : "needs_visual_review";
    return result;
}

std::string boxJson(const Box& box) {
    if (!box.valid) return "null";
    std::ostringstream out;
    out << "{\"left\":" << box.left << ",\"top\":" << box.top
        << ",\"right\":" << box.right << ",\"bottom\":" << box.bottom << '}';
    return out.str();
}

class PitchContextEngine {
public:
    void resolve(const StaffConfig& cfg,
                 const std::vector<NoteInput>& notes,
                 const std::vector<AccidentalInput>& accidentals,
                 const std::vector<StemInput>& stems) const {
        if (cfg.spacing <= 0 || cfg.spacing > 10000)
            throw std::invalid_argument("staff spacing must be positive");
        if (cfg.gridTolerance <= 0 || cfg.gridTolerance > 0.5)
            throw std::invalid_argument("grid tolerance must be in (0,0.5]");
        if (notes.empty()) throw std::invalid_argument("at least one note is required");

        const KeySignature key = parseKeySignature(cfg.key);
        (void)anchorForClef(cfg.clef);

        std::set<std::string> ids;
        for (const auto& n : notes) if (!ids.insert(n.id).second) throw std::invalid_argument("duplicate note id");
        ids.clear();
        for (const auto& a : accidentals) if (!ids.insert(a.id).second) throw std::invalid_argument("duplicate accidental id");
        ids.clear();
        for (const auto& s : stems) if (!ids.insert(s.id).second) throw std::invalid_argument("duplicate stem id");

        std::vector<PitchPosition> positions;
        positions.reserve(notes.size());
        for (const auto& note : notes) positions.push_back(mapStaffY(cfg, note));

        std::vector<Association> associations;
        associations.reserve(accidentals.size());
        for (size_t i = 0; i < accidentals.size(); ++i)
            associations.push_back(associateAccidental(i, cfg, accidentals[i], notes));

        std::unordered_map<std::string, size_t> stemIndex;
        for (size_t i = 0; i < stems.size(); ++i) stemIndex[stems[i].id] = i;

        std::vector<std::vector<size_t>> chordStemNoteIndices(stems.size());
        std::map<std::string, std::vector<size_t>> notesByChord;
        for (size_t i = 0; i < notes.size(); ++i) {
            if (!notes[i].chordId.empty()) notesByChord[notes[i].chordId].push_back(i);
            if (!notes[i].stemId.empty()) {
                auto it = stemIndex.find(notes[i].stemId);
                if (it != stemIndex.end()) chordStemNoteIndices[it->second].push_back(i);
            }
        }

        std::unordered_map<size_t, std::vector<size_t>> attachedAccidentals;
        std::unordered_map<size_t, std::vector<size_t>> ambiguousAccidentals;
        for (size_t ai = 0; ai < associations.size(); ++ai) {
            const Association& a = associations[ai];
            if (a.status == "associated") {
                attachedAccidentals[static_cast<size_t>(a.selectedNote)].push_back(ai);
            } else if (a.status == "ambiguous") {
                for (const auto& c : a.candidates)
                    ambiguousAccidentals[c.noteIndex].push_back(ai);
            }
        }

        std::vector<size_t> noteOrder(notes.size());
        std::iota(noteOrder.begin(), noteOrder.end(), 0);
        std::stable_sort(noteOrder.begin(), noteOrder.end(),
            [&](size_t a, size_t b) { return notes[a].x < notes[b].x; });

        // One request represents one measure. This map is intentionally local:
        // it is discarded at the end of resolve(), so accidental scope resets
        // at the next bar/request.
        std::map<std::string, int> localAccidentalRegister;
        std::vector<int> resolvedAlteration(notes.size(), 0);
        std::vector<bool> alterationCertain(notes.size(), true);
        std::vector<std::string> alterationSource(notes.size(), "key_signature");

        size_t cursor = 0;
        while (cursor < noteOrder.size()) {
            const double x = notes[noteOrder[cursor]].x;
            size_t end = cursor;
            while (end < noteOrder.size() &&
                   std::abs(notes[noteOrder[end]].x - x) <= 0.30 * cfg.spacing) ++end;

            // Apply all uniquely associated accidentals at this onset before
            // evaluating the chord's noteheads. This handles simultaneous
            // unisons in the same chord consistently.
            for (size_t p = cursor; p < end; ++p) {
                const size_t noteIndex = noteOrder[p];
                auto found = attachedAccidentals.find(noteIndex);
                if (found == attachedAccidentals.end()) continue;
                const std::string key = pitchKey(positions[noteIndex].letter, positions[noteIndex].octave);
                if (found->second.size() > 1) {
                    alterationCertain[noteIndex] = false;
                    alterationSource[noteIndex] = "conflicting_accidentals";
                    continue;
                }
                const size_t ai = found->second.front();
                localAccidentalRegister[key] = accidentalValue(accidentals[ai].kind);
                alterationSource[noteIndex] = "local_accidental";
            }

            for (size_t p = cursor; p < end; ++p) {
                const size_t noteIndex = noteOrder[p];
                const PitchPosition& position = positions[noteIndex];
                const std::string keyName = pitchKey(position.letter, position.octave);
                auto local = localAccidentalRegister.find(keyName);
                const int alteration = local == localAccidentalRegister.end()
                    ? key.get(position.letter) : local->second;

                resolvedAlteration[noteIndex] = alteration;
                if (local != localAccidentalRegister.end() &&
                    alterationSource[noteIndex] != "local_accidental") {
                    alterationSource[noteIndex] = "measure_local_memory";
                }

                if (!ambiguousAccidentals[noteIndex].empty()) {
                    alterationCertain[noteIndex] = false;
                    alterationSource[noteIndex] = "ambiguous_accidental_assignment";
                }
                if (!position.onGrid) {
                    alterationCertain[noteIndex] = false;
                    if (alterationSource[noteIndex] == "key_signature")
                        alterationSource[noteIndex] = "staff_position_unquantized";
                }
            }
            cursor = end;
        }

        std::vector<StemCheck> stemChecks;
        stemChecks.reserve(stems.size());
        for (size_t i = 0; i < stems.size(); ++i)
            stemChecks.push_back(checkStem(stems[i], notes, chordStemNoteIndices[i], cfg.spacing));

        bool reviewRequired = false;
        for (const auto& p : positions) if (!p.onGrid) reviewRequired = true;
        for (const auto& a : associations) if (a.status != "associated") reviewRequired = true;
        for (const auto& s : stemChecks)
            if (s.status == "needs_visual_review" || s.status == "orphan_stem") reviewRequired = true;

        emitJson(cfg, key, notes, accidentals, stems, positions, associations,
                 stemChecks, resolvedAlteration, alterationCertain, alterationSource,
                 attachedAccidentals, ambiguousAccidentals, reviewRequired);
    }

private:
    void emitJson(const StaffConfig& cfg, const KeySignature& key,
                  const std::vector<NoteInput>& notes,
                  const std::vector<AccidentalInput>& accidentals,
                  const std::vector<StemInput>& stems,
                  const std::vector<PitchPosition>& positions,
                  const std::vector<Association>& associations,
                  const std::vector<StemCheck>& stemChecks,
                  const std::vector<int>& alterations,
                  const std::vector<bool>& alterationCertain,
                  const std::vector<std::string>& alterationSource,
                  const std::unordered_map<size_t, std::vector<size_t>>& attached,
                  const std::unordered_map<size_t, std::vector<size_t>>& ambiguous,
                  bool reviewRequired) const {
        (void)key;
        std::ostringstream out;
        out << std::fixed << std::setprecision(4);
        out << "{\"type\":\"pitch_resolution\",\"status\":\""
            << (reviewRequired ? "review_required" : "resolved")
            << "\",\"measure_id\":\"" << jsonEscape(cfg.measureId)
            << "\",\"clef\":\"" << jsonEscape(cfg.clef)
            << "\",\"key_signature\":\"" << jsonEscape(cfg.key)
            << "\",\"accidental_scope\":\"measure_local_same_letter_and_octave\"";

        out << ",\"notes\":[";
        for (size_t i = 0; i < notes.size(); ++i) {
            if (i) out << ',';
            const auto& n = notes[i];
            const auto& p = positions[i];
            const int midi = midiPitch(p.letter, p.octave, alterations[i]);

            out << "{\"id\":\"" << jsonEscape(n.id)
                << "\",\"x\":" << n.x << ",\"y\":" << n.y
                << ",\"staff_step_from_bottom_line\":" << p.step
                << ",\"staff_grid_residual_px\":" << p.residualPx
                << ",\"on_staff_grid\":" << (p.onGrid ? "true" : "false")
                << ",\"letter\":\"" << p.letter << "\",\"octave\":" << p.octave
                << ",\"key_signature_alteration\":" << key.get(p.letter)
                << ",\"effective_alteration\":" << alterations[i]
                << ",\"effective_accidental\":\"" << accidentalName(alterations[i])
                << "\",\"midi_pitch\":" << midi
                << ",\"alteration_source\":\"" << alterationSource[i]
                << "\",\"pitch_certain\":" << (alterationCertain[i] && p.onGrid ? "true" : "false")
                << ",\"bbox\":" << boxJson(n.box);

            out << ",\"assigned_accidentals\":[";
            auto a = attached.find(i);
            if (a != attached.end()) {
                for (size_t j = 0; j < a->second.size(); ++j) {
                    if (j) out << ',';
                    out << '"' << jsonEscape(accidentals[a->second[j]].id) << '"';
                }
            }
            out << "],\"ambiguous_accidentals\":[";
            auto amb = ambiguous.find(i);
            if (amb != ambiguous.end()) {
                for (size_t j = 0; j < amb->second.size(); ++j) {
                    if (j) out << ',';
                    out << '"' << jsonEscape(accidentals[amb->second[j]].id) << '"';
                }
            }
            out << "],\"effective_alteration_options\":[";
            std::set<int> alterationOptions{alterations[i]};
            if (amb != ambiguous.end()) {
                for (size_t accidentalIndex : amb->second)
                    alterationOptions.insert(accidentalValue(accidentals[accidentalIndex].kind));
            }
            size_t optionIndex = 0;
            for (int option : alterationOptions) {
                if (optionIndex++) out << ',';
                out << option;
            }
            out << "],\"candidate_midi_pitches\":[";
            optionIndex = 0;
            for (int option : alterationOptions) {
                if (optionIndex++) out << ',';
                out << midiPitch(p.letter, p.octave, option);
            }
            out << "]}";
        }
        out << "],\"accidental_associations\":[";

        for (size_t i = 0; i < associations.size(); ++i) {
            if (i) out << ',';
            const auto& association = associations[i];
            const auto& accidental = accidentals[association.accidentalIndex];
            out << "{\"accidental_id\":\"" << jsonEscape(accidental.id)
                << "\",\"kind\":\"" << jsonEscape(accidental.kind)
                << "\",\"status\":\"" << association.status << "\"";
            if (association.selectedNote >= 0) {
                out << ",\"selected_note_id\":\""
                    << jsonEscape(notes[static_cast<size_t>(association.selectedNote)].id) << '"';
            }
            out << ",\"bbox\":" << boxJson(accidental.box) << ",\"candidates\":[";
            for (size_t j = 0; j < association.candidates.size(); ++j) {
                if (j) out << ',';
                const auto& candidate = association.candidates[j];
                out << "{\"note_id\":\"" << jsonEscape(notes[candidate.noteIndex].id)
                    << "\",\"cost\":" << candidate.cost << '}';
            }
            out << "]}";
        }

        out << "],\"stem_checks\":[";
        for (size_t i = 0; i < stemChecks.size(); ++i) {
            if (i) out << ',';
            const auto& s = stemChecks[i];
            out << "{\"stem_id\":\"" << jsonEscape(s.stem->id)
                << "\",\"chord_id\":\"" << jsonEscape(s.stem->chordId)
                << "\",\"direction\":\"" << s.stem->direction
                << "\",\"status\":\"" << s.status
                << "\",\"attached_note_count\":" << s.notes.size();
            if (s.anchorNote >= 0) {
                out << ",\"anchor_note_id\":\""
                    << jsonEscape(notes[static_cast<size_t>(s.anchorNote)].id) << '"';
            }
            if (s.hasBBox) out << ",\"horizontal_attachment_error_px\":" << s.horizontalError;
            out << '}';
        }

        out << "],\"visual_guidance\":[";
        bool firstGuidance = true;
        for (const auto& a : associations) {
            if (a.status == "associated") continue;
            if (!firstGuidance) out << ',';
            firstGuidance = false;
            const auto& accidental = accidentals[a.accidentalIndex];
            out << "{\"target\":\"" << jsonEscape(accidental.id)
                << "\",\"reason\":\"accidental_" << a.status
                << "\",\"bbox\":" << boxJson(accidental.box)
                << ",\"requested_checks\":[\"glyph_identity\",\"vertical_anchor\",\"candidate_notehead_alignment\"]}";
        }
        for (size_t i = 0; i < notes.size(); ++i) {
            if (positions[i].onGrid) continue;
            if (!firstGuidance) out << ',';
            firstGuidance = false;
            out << "{\"target\":\"" << jsonEscape(notes[i].id)
                << "\",\"reason\":\"notehead_off_staff_grid\",\"bbox\":" << boxJson(notes[i].box)
                << ",\"requested_checks\":[\"notehead_center\",\"staff_line_geometry\",\"staff_skew\"]}";
        }
        for (const auto& stem : stemChecks) {
            if (stem.status != "needs_visual_review" && stem.status != "orphan_stem") continue;
            if (!firstGuidance) out << ',';
            firstGuidance = false;
            out << "{\"target\":\"" << jsonEscape(stem.stem->id)
                << "\",\"reason\":\"stem_attachment_" << stem.status
                << "\",\"requested_checks\":[\"stem_x\",\"stem_endpoint\",\"chord_anchor_notehead\"]}";
        }
        out << "],\"scope_note\":\"One staff/voice and one measure per request. Dissonance is not a rejection rule. Ambiguous glyph-to-note assignment is returned for visual review, never guessed.\"}";
        std::cout << out.str() << '\n' << std::flush;
    }
};

StaffConfig parseConfig(const std::map<std::string, std::string>& values) {
    StaffConfig cfg;
    auto required = [&](const std::string& key) -> const std::string& {
        auto it = values.find(key);
        if (it == values.end()) throw std::invalid_argument("missing required option: " + key);
        return it->second;
    };
    cfg.clef = required("clef");
    cfg.topY = parseDouble(required("top_y"), "top_y");
    cfg.spacing = parseDouble(required("spacing"), "spacing");
    cfg.originX = values.count("origin_x") ? parseDouble(values.at("origin_x"), "origin_x") : 0;
    cfg.slope = values.count("slope") ? parseDouble(values.at("slope"), "slope") : 0;
    cfg.key = values.count("key") ? values.at("key") : "C";
    cfg.measureId = values.count("measure") ? values.at("measure") : "0";
    if (values.count("grid_tolerance"))
        cfg.gridTolerance = parseDouble(values.at("grid_tolerance"), "grid_tolerance");
    if (values.count("association_margin"))
        cfg.associationMargin = parseDouble(values.at("association_margin"), "association_margin");
    if (values.count("association_max_cost"))
        cfg.associationMaxCost = parseDouble(values.at("association_max_cost"), "association_max_cost");
    return cfg;
}

void emitError(const std::string& message) {
    std::cout << "{\"type\":\"error\",\"message\":\"" << jsonEscape(message) << "\"}\n" << std::flush;
}

} // namespace

int main() {
    std::cout << "{\"type\":\"ready\",\"engine\":\"pitch_context_engine\",\"version\":1}\n" << std::flush;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        std::istringstream input(line);
        std::string command;
        input >> command;
        command = lower(command);
        if (command == "exit" || command == "quit") break;
        if (command != "resolve") {
            emitError("unknown command; use RESOLVE or EXIT");
            continue;
        }

        try {
            std::map<std::string, std::string> values;
            std::vector<NoteInput> notes;
            std::vector<AccidentalInput> accidentals;
            std::vector<StemInput> stems;
            std::string token;

            while (input >> token) {
                const size_t equals = token.find('=');
                if (equals == std::string::npos || equals == 0 || equals + 1 >= token.size())
                    throw std::invalid_argument("arguments must use key=value syntax");
                const std::string key = lower(token.substr(0, equals));
                const std::string value = token.substr(equals + 1);
                if (key == "note") notes.push_back(parseNote(value));
                else if (key == "accidental") accidentals.push_back(parseAccidental(value));
                else if (key == "stem") stems.push_back(parseStem(value));
                else {
                    if (values.count(key)) throw std::invalid_argument("duplicate option: " + key);
                    values[key] = value;
                }
            }

            StaffConfig cfg = parseConfig(values);
            PitchContextEngine engine;
            engine.resolve(cfg, notes, accidentals, stems);
        } catch (const std::exception& error) {
            emitError(error.what());
        }
    }
    return 0;
}
