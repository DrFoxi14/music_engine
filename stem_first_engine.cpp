#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

/*
 * STEM-first Engine — OMR = STEM (see DrFoxi14/stem, STEM_CENTRIC_OMR.md).
 *
 * Image-agnostic reasoning over primitives already detected by the Visual
 * Engine. For every stem it decides which end is free and which end carries
 * the notehead, then derives duration candidates for the solver:
 *
 *   1. The stem's two real endpoints A and B are taken from the image; no
 *      up/down orientation is assumed.
 *   2. A flag or beam attached at one end marks that end as FREE; the
 *      notehead is searched at the opposite end only.
 *   3. Without flag/beam (quarter-note branch) both ends are tested and the
 *      end with a connected notehead is accepted; the other is rejected.
 *   4. Stem length is never a rule: it does not decide the end nor the pitch.
 *      All tolerances are multiples of the staff spacing.
 *   5. The notehead confirms the note; its fill only separates half vs
 *      quarter in the flag/beam-free branch. Flag/beam count gives the
 *      subdivision, dots and tuplets complete the duration.
 *   6. Notehead shape (X, diamond, ...) is classified after localization and
 *      never changes stem logic. Rests come from a dedicated recognizer and
 *      are only placed on the measure timeline here.
 *
 * Build:
 *   clang++ -std=c++17 -O3 -DNDEBUG stem_first_engine.cpp -o stem_first_engine
 *
 * Protocol, one request per line (image coordinates, y grows downwards):
 *   ANALYZE spacing=10 [meter=4/4] [unchecked=fill,dots] [unchecked_max_dots=1] \
 *     stem=id,x1,y1,x2,y2 \
 *     notehead=id,left,top,right,bottom,fill,fill_confidence \
 *     flag=id,left,top,right,bottom,count \
 *     beam=id,x1,y1,x2,y2,thickness \
 *     dot=id,x,y,confidence \
 *     rest=id,left,top,right,bottom,value,dots \
 *     tuplet=id,label,member;member;...
 *   PLAN spacing=10 stem=... flag=... beam=...
 *     STEM-first search plan: from stems, flags and beams alone, where to
 *     look for each notehead (one region, or one per end for quarter
 *     notes), for chord members and for dots. Noteheads are not needed.
 *   EXIT
 *
 *   fill      = filled | hollow | unknown   (fill_confidence in (0,1])
 *   thickness = beam thickness in px, or '-' for half a staff spacing
 *   value     = rest base value: 1, 2, 4, 8, ... B (breve), or 'measure'
 *   label     = "3" (normal inferred) or "5:6" (explicit a:n)
 *   members   = stem, rest or notehead ids enclosed by the tuplet bracket
 *   unchecked = visual features the Visual Engine has NOT inspected yet.
 *               fill: every head is treated as unknown (half or quarter in
 *               the flag/beam-free branch); dots: every note may carry
 *               0..unchecked_max_dots dots. The measure constraint then
 *               decides, and only real ambiguity becomes a visual question.
 *               Each candidate reports the fill and dot count it implies.
 */
namespace {

constexpr double kInf = std::numeric_limits<double>::infinity();

std::string jsonEscape(const std::string& s) {
    std::string out;
    for (unsigned char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
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

std::vector<std::string> split(const std::string& s, char delimiter) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delimiter)) out.push_back(item);
    return out;
}

double parseDouble(const std::string& s, const std::string& name) {
    size_t used = 0;
    double v = 0;
    try { v = std::stod(s, &used); } catch (...) { throw std::invalid_argument("invalid " + name); }
    if (used != s.size() || !std::isfinite(v)) throw std::invalid_argument("invalid " + name);
    return v;
}

int parseInt(const std::string& s, const std::string& name) {
    size_t used = 0;
    int v = 0;
    try { v = std::stoi(s, &used); } catch (...) { throw std::invalid_argument("invalid " + name); }
    if (used != s.size()) throw std::invalid_argument("invalid " + name);
    return v;
}

std::string checkId(const std::string& id, const std::string& label) {
    if (id.empty()) throw std::invalid_argument(label + " id is empty");
    for (unsigned char c : id)
        if (!(std::isalnum(c) || c == '_' || c == '-'))
            throw std::invalid_argument(label + " ids may contain only letters, digits, _ and -");
    return id;
}

std::vector<std::string> fields(const std::string& value, size_t expected, const std::string& label) {
    auto f = split(value, ',');
    if (f.size() != expected)
        throw std::invalid_argument(label + " expects " + std::to_string(expected) + " comma-separated fields");
    return f;
}

// ---------------------------------------------------------------------------
// Exact durations.
// ---------------------------------------------------------------------------
struct Fraction {
    int64_t n = 0;
    int64_t d = 1;
    static Fraction make(int64_t n, int64_t d) {
        const int64_t g = std::gcd(n, d);
        return Fraction{n / g, d / g};
    }
    Fraction operator*(const Fraction& o) const { return make(n * o.n, d * o.d); }
    bool operator<(const Fraction& o) const { return n * o.d < o.n * d; }
    bool operator==(const Fraction& o) const { return n == o.n && d == o.d; }
    std::string str() const { return std::to_string(n) + "/" + std::to_string(d); }
};

// ---------------------------------------------------------------------------
// Geometry.
// ---------------------------------------------------------------------------
struct Point { double x = 0, y = 0; };

struct Box {
    double left = 0, top = 0, right = 0, bottom = 0;
    Point center() const { return {(left + right) / 2, (top + bottom) / 2}; }
};

double distance(Point a, Point b) { return std::hypot(a.x - b.x, a.y - b.y); }

// Distance from p to segment ab; t receives the clamped parameter in [0,1].
double pointSegment(Point p, Point a, Point b, double* t = nullptr) {
    const double dx = b.x - a.x, dy = b.y - a.y;
    const double len2 = dx * dx + dy * dy;
    double u = len2 > 0 ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2 : 0;
    u = std::clamp(u, 0.0, 1.0);
    if (t) *t = u;
    return distance(p, {a.x + u * dx, a.y + u * dy});
}

double boxPoint(const Box& b, Point p) {
    const double dx = std::max({b.left - p.x, 0.0, p.x - b.right});
    const double dy = std::max({b.top - p.y, 0.0, p.y - b.bottom});
    return std::hypot(dx, dy);
}

// Liang–Barsky: does segment ab cross the box?
bool segmentHitsBox(Point a, Point b, const Box& box) {
    double t0 = 0, t1 = 1;
    const double dx = b.x - a.x, dy = b.y - a.y;
    const double p[4] = {-dx, dx, -dy, dy};
    const double q[4] = {a.x - box.left, box.right - a.x, a.y - box.top, box.bottom - a.y};
    for (int i = 0; i < 4; ++i) {
        if (p[i] == 0) {
            if (q[i] < 0) return false;
        } else {
            const double r = q[i] / p[i];
            if (p[i] < 0) t0 = std::max(t0, r);
            else t1 = std::min(t1, r);
            if (t0 > t1) return false;
        }
    }
    return true;
}

double boxSegment(const Box& box, Point a, Point b) {
    if (segmentHitsBox(a, b, box)) return 0;
    double best = std::min(boxPoint(box, a), boxPoint(box, b));
    const Point corners[4] = {{box.left, box.top}, {box.right, box.top}, {box.left, box.bottom}, {box.right, box.bottom}};
    for (const Point& c : corners) best = std::min(best, pointSegment(c, a, b));
    return best;
}

// Closest approach between segments; tA receives the parameter along the first.
double segmentSegment(Point a1, Point a2, Point b1, Point b2, double* tA) {
    const double d1x = a2.x - a1.x, d1y = a2.y - a1.y;
    const double d2x = b2.x - b1.x, d2y = b2.y - b1.y;
    const double denom = d1x * d2y - d1y * d2x;
    if (std::abs(denom) > 1e-12) {
        const double s = ((b1.x - a1.x) * d2y - (b1.y - a1.y) * d2x) / denom;
        const double u = ((b1.x - a1.x) * d1y - (b1.y - a1.y) * d1x) / denom;
        if (s >= 0 && s <= 1 && u >= 0 && u <= 1) { *tA = s; return 0; }
    }
    double best = kInf, t = 0;
    double cand = pointSegment(a1, b1, b2);
    if (cand < best) { best = cand; t = 0; }
    cand = pointSegment(a2, b1, b2);
    if (cand < best) { best = cand; t = 1; }
    double u = 0;
    cand = pointSegment(b1, a1, a2, &u);
    if (cand < best) { best = cand; t = u; }
    cand = pointSegment(b2, a1, a2, &u);
    if (cand < best) { best = cand; t = u; }
    *tA = t;
    return best;
}

// ---------------------------------------------------------------------------
// Inputs.
// ---------------------------------------------------------------------------
struct Stem { std::string id; Point a, b; };
struct Notehead { std::string id; Box box; std::string fill; double fillConfidence = 1; };
struct Flag { std::string id; Box box; int count = 1; };
struct Beam { std::string id; Point a, b; double thickness = 0; };
struct Dot { std::string id; Point p; double confidence = 1; };
struct Rest { std::string id; Box box; std::string value; int dots = 0; };
struct Tuplet { std::string id; int actual = 3; int normal = 0; std::vector<std::string> members; };

struct Request {
    double spacing = 0;
    bool fillUnchecked = false;
    bool dotsUnchecked = false;
    int uncheckedMaxDots = 1;
    int meterNum = 0, meterDen = 0;
    std::vector<Stem> stems;
    std::vector<Notehead> noteheads;
    std::vector<Flag> flags;
    std::vector<Beam> beams;
    std::vector<Dot> dots;
    std::vector<Rest> rests;
    std::vector<Tuplet> tuplets;
};

Box parseBox(const std::vector<std::string>& f, size_t i, const std::string& label) {
    Box b{parseDouble(f[i], label + " left"), parseDouble(f[i + 1], label + " top"),
          parseDouble(f[i + 2], label + " right"), parseDouble(f[i + 3], label + " bottom")};
    if (b.right <= b.left || b.bottom <= b.top) throw std::invalid_argument(label + " box has non-positive size");
    return b;
}

double parseConfidence(const std::string& s, const std::string& label) {
    const double v = parseDouble(s, label);
    if (v <= 0 || v > 1) throw std::invalid_argument(label + " must be in (0,1]");
    return v;
}

Request parseRequest(std::istringstream& input) {
    Request r;
    std::string token;
    std::set<std::string> ids;
    auto unique = [&](const std::string& id, const std::string& label) {
        if (!ids.insert(checkId(id, label)).second) throw std::invalid_argument("duplicate id: " + id);
        return id;
    };
    while (input >> token) {
        const size_t eq = token.find('=');
        if (eq == std::string::npos || eq == 0) throw std::invalid_argument("arguments must use key=value syntax");
        const std::string key = token.substr(0, eq);
        const std::string value = token.substr(eq + 1);
        if (key == "spacing") {
            r.spacing = parseDouble(value, "spacing");
        } else if (key == "meter") {
            const auto p = split(value, '/');
            if (p.size() != 2) throw std::invalid_argument("meter must look like 4/4");
            r.meterNum = parseInt(p[0], "meter numerator");
            r.meterDen = parseInt(p[1], "meter denominator");
            if (r.meterNum <= 0 || r.meterDen <= 0) throw std::invalid_argument("meter must be positive");
        } else if (key == "unchecked") {
            for (const auto& item : split(value, ',')) {
                if (item == "fill") r.fillUnchecked = true;
                else if (item == "dots") r.dotsUnchecked = true;
                else if (!item.empty()) throw std::invalid_argument("unchecked accepts fill and dots");
            }
        } else if (key == "unchecked_max_dots") {
            r.uncheckedMaxDots = parseInt(value, "unchecked_max_dots");
            if (r.uncheckedMaxDots < 0 || r.uncheckedMaxDots > 3)
                throw std::invalid_argument("unchecked_max_dots must be 0..3");
        } else if (key == "stem") {
            auto f = fields(value, 5, "stem");
            Stem s{unique(f[0], "stem"), {parseDouble(f[1], "stem x1"), parseDouble(f[2], "stem y1")},
                   {parseDouble(f[3], "stem x2"), parseDouble(f[4], "stem y2")}};
            if (distance(s.a, s.b) <= 0) throw std::invalid_argument("stem endpoints coincide: " + s.id);
            r.stems.push_back(s);
        } else if (key == "notehead") {
            auto f = fields(value, 7, "notehead");
            Notehead n{unique(f[0], "notehead"), parseBox(f, 1, "notehead"), f[5], parseConfidence(f[6], "fill confidence")};
            if (n.fill != "filled" && n.fill != "hollow" && n.fill != "unknown")
                throw std::invalid_argument("notehead fill must be filled, hollow or unknown");
            r.noteheads.push_back(n);
        } else if (key == "flag") {
            auto f = fields(value, 6, "flag");
            Flag fl{unique(f[0], "flag"), parseBox(f, 1, "flag"), parseInt(f[5], "flag count")};
            if (fl.count < 1 || fl.count > 8) throw std::invalid_argument("flag count must be 1..8");
            r.flags.push_back(fl);
        } else if (key == "beam") {
            auto f = fields(value, 6, "beam");
            Beam b{unique(f[0], "beam"), {parseDouble(f[1], "beam x1"), parseDouble(f[2], "beam y1")},
                   {parseDouble(f[3], "beam x2"), parseDouble(f[4], "beam y2")},
                   f[5] == "-" ? 0 : parseDouble(f[5], "beam thickness")};
            r.beams.push_back(b);
        } else if (key == "dot") {
            auto f = fields(value, 4, "dot");
            r.dots.push_back(Dot{unique(f[0], "dot"), {parseDouble(f[1], "dot x"), parseDouble(f[2], "dot y")},
                                 parseConfidence(f[3], "dot confidence")});
        } else if (key == "rest") {
            auto f = fields(value, 7, "rest");
            Rest rest{unique(f[0], "rest"), parseBox(f, 1, "rest"), f[5], parseInt(f[6], "rest dots")};
            if (rest.dots < 0 || rest.dots > 4) throw std::invalid_argument("rest dots must be 0..4");
            r.rests.push_back(rest);
        } else if (key == "tuplet") {
            auto f = fields(value, 3, "tuplet");
            Tuplet t;
            t.id = unique(f[0], "tuplet");
            const auto ratio = split(f[1], ':');
            t.actual = parseInt(ratio[0], "tuplet number");
            if (ratio.size() == 2) t.normal = parseInt(ratio[1], "tuplet normal");
            else if (ratio.size() != 1) throw std::invalid_argument("tuplet label must look like 3 or 5:6");
            if (t.actual < 2 || t.actual > 64 || (ratio.size() == 2 && (t.normal < 1 || t.normal > 64)))
                throw std::invalid_argument("tuplet numbers out of range");
            for (const auto& m : split(f[2], ';')) if (!m.empty()) t.members.push_back(checkId(m, "tuplet member"));
            if (t.members.empty()) throw std::invalid_argument("tuplet needs at least one member");
            r.tuplets.push_back(t);
        } else {
            throw std::invalid_argument("unknown argument: " + key);
        }
    }
    if (r.spacing <= 0) throw std::invalid_argument("spacing (staff line spacing in px) is required");
    return r;
}

// ---------------------------------------------------------------------------
// Analysis.
// ---------------------------------------------------------------------------
struct Candidate {
    Fraction duration;
    double confidence = 1;
    std::string reading;
    std::string fill = "any"; // head fill this reading implies: filled | hollow | any | rest
    int dots = 0;             // augmentation dots this reading implies
};

struct StemResult {
    const Stem* stem = nullptr;
    int flagsAt[2] = {0, 0};   // [A, B]
    int beamsAt[2] = {0, 0};
    std::vector<std::string> flagIds[2];
    std::vector<std::string> beamIds[2];
    std::vector<size_t> nearEnd[2];      // noteheads touching the stem near each end
    std::vector<size_t> touching;        // every notehead touching the stem
    std::string branch;                  // flag | beam | flag_and_beam | quarter_branch | conflict
    int freeEnd = -1;                    // 0 = A, 1 = B
    int headEnd = -1;
    std::string status;
    std::vector<size_t> chord;           // noteheads assigned to this stem
    int anchor = -1;
    std::string direction;
    std::vector<size_t> atFreeEnd;       // noteheads found at the free end (conflict)
    std::vector<size_t> across;          // touching heads across the stem: another voice's, not a second or unison
};

struct Event {
    std::string id;
    std::string kind;     // note | chord | rest
    std::string source;   // stem | stemless | rest
    std::string stemId;
    std::vector<size_t> heads;
    const Rest* rest = nullptr;
    double x = 0;
    std::string direction;
    int dots = 0;
    double dotConfidence = 1;
    std::vector<std::string> tupletIds;
    std::vector<Candidate> candidates;
    bool confirmed = true;
    std::string basis;
};

struct Guidance { std::string target; std::string reason; std::vector<std::string> checks; };

std::string noteName(int denominator) {
    switch (denominator) {
        case 1: return "whole";
        case 2: return "half";
        case 4: return "quarter";
        case 8: return "eighth";
        case 16: return "sixteenth";
        case 32: return "thirty-second";
        case 64: return "sixty-fourth";
        default: return "1/" + std::to_string(denominator);
    }
}

std::vector<int> conventionalNormals(int actual) {
    std::vector<int> out;
    if (actual & (actual - 1)) {
        int p = 1;
        while (p * 2 < actual) p *= 2;
        out.push_back(p);
    } else {
        out.push_back(actual * 3 / 2);
        if (actual >= 4) out.push_back(actual * 3 / 4);
    }
    return out;
}

class Analyzer {
public:
    explicit Analyzer(const Request& r) : r_(r), s_(r.spacing) {}

    std::string run() {
        for (const auto& stem : r_.stems) stems_.push_back(analyzeStem(stem));
        markSharedNoteheads();
        buildStemEvents();
        buildStemlessEvents();
        buildRestEvents();
        applyDotsAndTuplets();
        std::stable_sort(events_.begin(), events_.end(), [](const Event& a, const Event& b) { return a.x < b.x; });
        return json();
    }

private:
    const Request& r_;
    double s_;
    std::vector<StemResult> stems_;
    std::vector<Event> events_;
    std::vector<Guidance> guidance_;
    std::map<size_t, std::vector<std::string>> headOwners_;

    // Tolerances, all relative to staff spacing; never to stem length.
    double endTolerance() const { return 0.75 * s_; }
    double touchTolerance() const { return 0.35 * s_; }
    double beamReach() const { return 6.0 * s_; }   // room for 8 stacked beams

    Point end(const Stem& stem, int which) const { return which == 0 ? stem.a : stem.b; }

    // The stem's x at height y (stems may be tilted).
    static double stemXAt(const Stem& stem, double y) {
        const double dy = stem.b.y - stem.a.y;
        return std::abs(dy) < 1e-9 ? stem.a.x : stem.a.x + (stem.b.x - stem.a.x) * (y - stem.a.y) / dy;
    }

    // Flags and beams at each end of a stem (no notehead is needed for this).
    void attachments(const Stem& stem, StemResult& res) {
        const double length = distance(stem.a, stem.b);

        // Flags: attached at the end they touch.
        for (const auto& flag : r_.flags) {
            const double dA = boxPoint(flag.box, stem.a), dB = boxPoint(flag.box, stem.b);
            const int which = dA <= dB ? 0 : 1;
            if (std::min(dA, dB) > endTolerance()) continue;
            res.flagsAt[which] += flag.count;
            res.flagIds[which].push_back(flag.id);
        }
        // Beams. A beam CONNECTED at an end (touching the endpoint itself)
        // marks that end as free. The beam count is then the stack of beams
        // crossing the stem within reach of that free end. The stem's
        // midpoint is never used, so beam stacks on short stems still work.
        struct Crossing { const Beam* beam; double from[2]; };
        std::vector<Crossing> crossings;
        bool connected[2] = {false, false};
        for (const auto& beam : r_.beams) {
            double t = 0;
            const double thick = beam.thickness > 0 ? beam.thickness : 0.5 * s_;
            const double d = segmentSegment(stem.a, stem.b, beam.a, beam.b, &t);
            if (d > thick / 2 + touchTolerance()) continue;
            const Crossing c{&beam, {t * length, (1 - t) * length}};
            for (int e = 0; e < 2; ++e)
                if (c.from[e] <= thick / 2 + endTolerance()) connected[e] = true;
            crossings.push_back(c);
        }
        for (int e = 0; e < 2; ++e) {
            if (!connected[e]) continue;
            for (const auto& c : crossings) {
                if (c.from[e] > beamReach()) continue;
                res.beamsAt[e] += 1;
                res.beamIds[e].push_back(c.beam->id);
            }
        }
        if (!crossings.empty() && !connected[0] && !connected[1])
            guidance_.push_back({stem.id, "beam_crosses_stem_away_from_its_ends", {"beam_geometry", "stem_endpoints"}});
    }

    StemResult analyzeStem(const Stem& stem) {
        StemResult res;
        res.stem = &stem;
        attachments(stem, res);
        // Noteheads connected to the stem, and which end they sit at.
        for (size_t i = 0; i < r_.noteheads.size(); ++i) {
            const Box& box = r_.noteheads[i].box;
            if (boxSegment(box, stem.a, stem.b) > touchTolerance()) continue;
            res.touching.push_back(i);
            const double dA = boxPoint(box, stem.a), dB = boxPoint(box, stem.b);
            if (std::min(dA, dB) > endTolerance()) continue;
            res.nearEnd[dA <= dB ? 0 : 1].push_back(i);
        }

        const bool freeA = res.flagsAt[0] + res.beamsAt[0] > 0;
        const bool freeB = res.flagsAt[1] + res.beamsAt[1] > 0;
        if (freeA && freeB) {
            res.branch = "conflict";
            res.status = "flag_or_beam_at_both_ends";
            guidance_.push_back({stem.id, "flag_or_beam_at_both_ends", {"flag_detection", "beam_detection", "stem_endpoints"}});
            return res;
        }
        if (freeA || freeB) {
            res.freeEnd = freeA ? 0 : 1;
            res.headEnd = 1 - res.freeEnd;
            const bool hasFlag = res.flagsAt[res.freeEnd] > 0, hasBeam = res.beamsAt[res.freeEnd] > 0;
            res.branch = hasFlag && hasBeam ? "flag_and_beam" : (hasFlag ? "flag" : "beam");
            if (hasFlag && hasBeam)
                guidance_.push_back({stem.id, "flag_and_beam_at_same_end", {"flag_vs_partial_beam"}});
        } else {
            // Quarter-note branch: test both ends, accept the confirmed one.
            res.branch = "quarter_branch";
            const bool headA = !res.nearEnd[0].empty(), headB = !res.nearEnd[1].empty();
            if (headA && headB) {
                res.status = "noteheads_at_both_ends";
                guidance_.push_back({stem.id, "noteheads_at_both_ends", {"stem_split_into_two_stems", "voice_separation"}});
                return res;
            }
            if (!headA && !headB) {
                res.status = "no_notehead_at_either_end";
                guidance_.push_back({stem.id, "stem_without_notehead", {"notehead_detection", "is_this_a_stem"}});
                return res;
            }
            res.headEnd = headA ? 0 : 1;
            res.freeEnd = 1 - res.headEnd;
        }

        // Noteheads at the free end contradict the flag/beam evidence.
        for (size_t i : res.nearEnd[res.freeEnd]) res.atFreeEnd.push_back(i);
        if (!res.atFreeEnd.empty())
            guidance_.push_back({stem.id, "notehead_at_free_end", {"notehead_detection", "flag_or_beam_end"}});

        const Point head = end(stem, res.headEnd), freeP = end(stem, res.freeEnd);
        res.direction = head.y > freeP.y ? "up" : "down";

        // Chord: the connected noteheads except those at the free end, on the head
        // side of the stem (left of an up stem, right of a down stem). Across the
        // stem a head belongs to the chord only as the displaced head of a second
        // (one step from a member, toward the free end) or of a unison (level with
        // a member). Otherwise it is the head of another voice whose stem runs
        // close to this one.
        const double side = res.direction == "up" ? -1.0 : 1.0;
        const double towardFree = freeP.y < head.y ? -1.0 : 1.0;
        std::vector<size_t> across;
        for (size_t i : res.touching) {
            if (std::find(res.atFreeEnd.begin(), res.atFreeEnd.end(), i) != res.atFreeEnd.end()) continue;
            const Point c = r_.noteheads[i].box.center();
            (side * (c.x - stemXAt(stem, c.y)) > -0.15 * s_ ? res.chord : across).push_back(i);
        }
        for (size_t i : across) {
            const double y = r_.noteheads[i].box.center().y;
            const bool second = std::any_of(res.chord.begin(), res.chord.end(), [&](size_t j) {
                const double step = (y - r_.noteheads[j].box.center().y) * towardFree;
                return step > -0.25 * s_ && step <= 0.75 * s_;
            });
            (second ? res.chord : res.across).push_back(i);
        }
        if (!res.across.empty())
            guidance_.push_back({stem.id, "notehead_across_stem_not_a_second", {"voice_separation", "stem_detection"}});

        double best = kInf;
        for (size_t i : res.nearEnd[res.headEnd]) {
            if (std::find(res.chord.begin(), res.chord.end(), i) == res.chord.end()) continue;
            const double d = boxPoint(r_.noteheads[i].box, end(stem, res.headEnd));
            if (d < best) { best = d; res.anchor = static_cast<int>(i); }
        }
        if (res.anchor < 0) {
            res.status = "notehead_missing_at_notehead_end";
            guidance_.push_back({stem.id, "notehead_missing_at_notehead_end", {"notehead_detection_near_endpoint"}});
        } else {
            res.status = "resolved";
        }
        for (size_t i : res.chord) headOwners_[i].push_back(stem.id);
        return res;
    }

    void markSharedNoteheads() {
        for (const auto& [head, owners] : headOwners_)
            if (owners.size() > 1)
                guidance_.push_back({r_.noteheads[head].id, "notehead_shared_by_several_stems", {"voice_separation", "unison_or_seconds"}});
    }

    std::vector<Candidate> fillCandidates(const Notehead& head) const {
        // Flag/beam-free branch: only here does the notehead fill matter.
        const Candidate quarter{Fraction::make(1, 4), 1, "quarter (stem, no flag/beam, filled head)", "filled", 0};
        const Candidate half{Fraction::make(1, 2), 1, "half (stem, no flag/beam, hollow head)", "hollow", 0};
        if (head.fill == "unknown" || r_.fillUnchecked) {
            return {Candidate{quarter.duration, 0.5, quarter.reading, "filled", 0},
                    Candidate{half.duration, 0.5, half.reading, "hollow", 0}};
        }
        const bool filled = head.fill == "filled";
        Candidate primary = filled ? quarter : half;
        Candidate other = filled ? half : quarter;
        primary.confidence = head.fillConfidence;
        if (head.fillConfidence >= 1) return {primary};
        other.confidence = 1 - head.fillConfidence;
        return {primary, other};
    }

    void buildStemEvents() {
        for (const auto& st : stems_) {
            if (st.headEnd < 0) continue;
            Event e;
            e.id = st.stem->id;
            e.source = "stem";
            e.stemId = st.stem->id;
            e.heads = st.chord;
            e.kind = st.chord.size() > 1 ? "chord" : "note";
            e.direction = st.direction;
            e.confirmed = st.anchor >= 0;
            const Point head = end(*st.stem, st.headEnd);
            e.x = st.anchor >= 0 ? r_.noteheads[static_cast<size_t>(st.anchor)].box.center().x : head.x;

            const int k = std::max(st.flagsAt[st.freeEnd], st.beamsAt[st.freeEnd]);
            if (k > 0) {
                const int den = 4 << k;
                const std::string what = st.beamsAt[st.freeEnd] >= st.flagsAt[st.freeEnd] ? " beam" : " flag";
                e.basis = std::to_string(k) + what + (k > 1 ? "s" : "") + " at free end";
                e.candidates.push_back({Fraction::make(1, den), 1, noteName(den) + " (" + e.basis + ")"});
                if (st.anchor >= 0 && r_.noteheads[static_cast<size_t>(st.anchor)].fill == "hollow")
                    guidance_.push_back({e.id, "hollow_head_with_flag_or_beam", {"notehead_fill", "tremolo_marking"}});
            } else if (st.anchor >= 0) {
                e.basis = "no flag/beam; head fill decides half vs quarter";
                e.candidates = fillCandidates(r_.noteheads[static_cast<size_t>(st.anchor)]);
                for (size_t i : st.chord)
                    if (r_.noteheads[i].fill != "unknown" &&
                        r_.noteheads[i].fill != r_.noteheads[static_cast<size_t>(st.anchor)].fill) {
                        guidance_.push_back({e.id, "chord_noteheads_disagree_on_fill", {"notehead_fill"}});
                        break;
                    }
            } else {
                continue; // quarter branch without a notehead never reaches here
            }
            events_.push_back(e);
        }
    }

    void buildStemlessEvents() {
        std::vector<size_t> free;
        for (size_t i = 0; i < r_.noteheads.size(); ++i) {
            // A head touching a stem belongs to it, unless it lies across the stem as
            // another voice's head: then it is free if no stem claims it.
            bool owned = false;
            for (const auto& st : stems_)
                if (std::find(st.touching.begin(), st.touching.end(), i) != st.touching.end() &&
                    std::find(st.across.begin(), st.across.end(), i) == st.across.end()) owned = true;
            if (!owned) free.push_back(i);
        }
        // Group stemless heads that overlap horizontally into one chord event.
        std::sort(free.begin(), free.end(), [&](size_t a, size_t b) {
            return r_.noteheads[a].box.left < r_.noteheads[b].box.left;
        });
        std::vector<std::vector<size_t>> groups;
        for (size_t i : free) {
            if (!groups.empty()) {
                const Box& last = r_.noteheads[groups.back().front()].box;
                const Box& cur = r_.noteheads[i].box;
                if (cur.left <= last.right && cur.right >= last.left) { groups.back().push_back(i); continue; }
            }
            groups.push_back({i});
        }
        for (const auto& group : groups) {
            const Notehead& head = r_.noteheads[group.front()];
            Event e;
            e.id = head.id;
            e.source = "stemless";
            e.heads = group;
            e.kind = group.size() > 1 ? "chord" : "note";
            e.x = head.box.center().x;
            if (head.fill == "hollow" || head.fill == "unknown" || r_.fillUnchecked) {
                // A stem was searched for and not found: the stemless value.
                e.basis = "stemless head";
                e.candidates.push_back({Fraction::make(1, 1), 1, "whole (stemless head)", "hollow", 0});
            } else {
                // A filled head needs a stem; list every filled-head reading
                // and let the measure constraint and a visual recheck decide.
                e.confirmed = false;
                e.basis = "filled head without detected stem";
                for (int den = 4; den <= 32; den *= 2)
                    e.candidates.push_back({Fraction::make(1, den), 0.25, noteName(den) + " (stem not detected)", "filled", 0});
                guidance_.push_back({head.id, "filled_notehead_without_stem", {"stem_detection", "flag_or_beam_detection"}});
            }
            events_.push_back(e);
        }
    }

    void buildRestEvents() {
        for (const auto& rest : r_.rests) {
            Event e;
            e.id = rest.id;
            e.kind = "rest";
            e.source = "rest";
            e.rest = &rest;
            e.x = rest.box.center().x;
            Fraction base;
            std::string name;
            if (rest.value == "measure") {
                if (r_.meterNum == 0) throw std::invalid_argument("a measure rest needs meter=");
                base = Fraction::make(r_.meterNum, r_.meterDen);
                name = "full-measure rest";
            } else if (rest.value == "B") {
                base = Fraction::make(2, 1);
                name = "breve rest";
            } else {
                const int den = parseInt(rest.value, "rest value");
                if (den < 1 || den > 1024 || (den & (den - 1))) throw std::invalid_argument("rest value must be a power of two");
                base = Fraction::make(1, den);
                name = noteName(den) + " rest";
            }
            e.basis = "rest recognizer";
            e.dots = rest.dots;
            e.candidates.push_back({base, 1, name, "rest", 0});
            events_.push_back(e);
        }
    }

    // Dots: to the right of a head of the event, at the head's height
    // (raised by half a space when the head sits on a line).
    void collectDots(Event& e) {
        if (e.source == "rest" || r_.dotsUnchecked) return;
        int best = 0;
        double conf = 1;
        for (size_t h : e.heads) {
            const Box& box = r_.noteheads[h].box;
            const Point c = box.center();
            std::vector<const Dot*> row;
            for (const auto& dot : r_.dots) {
                if (std::abs(dot.p.y - c.y) > 0.75 * s_) continue;
                if (dot.p.x < box.right - 0.1 * s_ || dot.p.x > box.right + 4.0 * s_) continue;
                row.push_back(&dot);
            }
            std::sort(row.begin(), row.end(), [](const Dot* a, const Dot* b) { return a->p.x < b->p.x; });
            int count = 0;
            double lastX = box.right, c2 = 1;
            for (const Dot* dot : row) {
                if (dot->p.x - lastX > (count == 0 ? 2.0 : 1.2) * s_) break;
                ++count;
                c2 = std::min(c2, dot->confidence);
                lastX = dot->p.x;
            }
            if (count > best) { best = count; conf = c2; }
        }
        e.dots = best;
        e.dotConfidence = conf;
    }

    void applyDotsAndTuplets() {
        std::map<std::string, std::vector<const Tuplet*>> byMember;
        for (const auto& t : r_.tuplets) for (const auto& m : t.members) byMember[m].push_back(&t);

        for (Event& e : events_) {
            collectDots(e);
            // Members: stem id, rest id, or any notehead id of the event.
            std::set<const Tuplet*> tuplets;
            auto add = [&](const std::string& id) {
                auto it = byMember.find(id);
                if (it != byMember.end()) tuplets.insert(it->second.begin(), it->second.end());
            };
            add(e.id);
            if (!e.stemId.empty()) add(e.stemId);
            for (size_t h : e.heads) add(r_.noteheads[h].id);

            std::vector<Candidate> out = e.candidates;
            auto withDots = [](Candidate c, int dots, double confidence) {
                const Fraction factor = Fraction::make((int64_t(2) << dots) - 1, int64_t(1) << dots);
                c.duration = c.duration * factor;
                c.confidence *= confidence;
                c.dots = dots;
                c.reading += " + " + std::to_string(dots) + " dot" + (dots > 1 ? "s" : "");
                return c;
            };
            if (r_.dotsUnchecked && e.source != "rest") {
                // Dots not inspected: every dot count up to the limit stays possible.
                std::vector<Candidate> expanded;
                for (const auto& c : out) {
                    Candidate plain = c;
                    plain.confidence *= 0.7;
                    plain.reading += " (dots unchecked)";
                    expanded.push_back(plain);
                    for (int k = 1; k <= r_.uncheckedMaxDots; ++k)
                        expanded.push_back(withDots(c, k, 0.3 / r_.uncheckedMaxDots));
                }
                out = expanded;
            } else if (e.dots > 0) {
                std::vector<Candidate> dotted;
                for (const auto& c : out) {
                    dotted.push_back(withDots(c, e.dots, e.dotConfidence));
                    if (e.dotConfidence < 1) {
                        Candidate plain = c;
                        plain.confidence *= 1 - e.dotConfidence;
                        plain.reading += " (dot rejected)";
                        dotted.push_back(plain);
                    }
                }
                out = dotted;
            }
            for (const Tuplet* t : tuplets) {
                e.tupletIds.push_back(t->id);
                std::vector<int> normals = t->normal ? std::vector<int>{t->normal} : conventionalNormals(t->actual);
                std::vector<Candidate> scaled;
                for (const auto& c : out) {
                    for (int n : normals) {
                        const double share = 1.0 / static_cast<double>(normals.size());
                        Candidate scaledCandidate = c;
                        scaledCandidate.duration = c.duration * Fraction::make(n, t->actual);
                        scaledCandidate.confidence = c.confidence * share;
                        scaledCandidate.reading += " in " + std::to_string(t->actual) + ":" + std::to_string(n);
                        scaled.push_back(scaledCandidate);
                    }
                }
                out = scaled;
            }
            // Merge equal durations, keep the most confident reading.
            std::vector<Candidate> merged;
            for (const auto& c : out) {
                auto it = std::find_if(merged.begin(), merged.end(), [&](const Candidate& m) { return m.duration == c.duration; });
                if (it == merged.end()) merged.push_back(c);
                else if (c.confidence > it->confidence) *it = c;
            }
            std::stable_sort(merged.begin(), merged.end(), [](const Candidate& a, const Candidate& b) {
                return a.confidence > b.confidence;
            });
            e.candidates = merged;
        }
        for (const auto& t : r_.tuplets) {
            for (const auto& m : t.members) {
                bool found = false;
                for (const Event& e : events_) {
                    if (e.id == m || e.stemId == m) found = true;
                    for (size_t h : e.heads) if (r_.noteheads[h].id == m) found = true;
                }
                if (!found) guidance_.push_back({t.id, "tuplet_member_not_found:" + m, {"tuplet_bracket_extent"}});
            }
        }
    }

    std::string idsJson(const std::vector<size_t>& heads) const {
        std::string out = "[";
        for (size_t i = 0; i < heads.size(); ++i) out += (i ? ",\"" : "\"") + jsonEscape(r_.noteheads[heads[i]].id) + "\"";
        return out + "]";
    }

    static std::string strList(const std::vector<std::string>& v) {
        std::string out = "[";
        for (size_t i = 0; i < v.size(); ++i) out += (i ? ",\"" : "\"") + jsonEscape(v[i]) + "\"";
        return out + "]";
    }

public:
    // STEM-first search plan: only stems, flags and beams are known. For each
    // stem, say where the Visual Engine must look for the notehead (one
    // region, or one per end in the quarter-note branch), for chord members
    // along the stem, and for augmentation dots. Every size is a multiple of
    // the staff spacing; the stem's length is never used.
    std::string plan() {
        std::ostringstream out;
        out << std::setprecision(6);
        out << "{\"type\":\"search_plan\",\"stems\":[";
        double area = 0;
        size_t regionCount = 0;
        for (size_t i = 0; i < r_.stems.size(); ++i) {
            const Stem& stem = r_.stems[i];
            StemResult res;
            res.stem = &stem;
            attachments(stem, res);
            const bool freeA = res.flagsAt[0] + res.beamsAt[0] > 0;
            const bool freeB = res.flagsAt[1] + res.beamsAt[1] > 0;
            std::string branch, direction;
            int freeEnd = -1;
            if (freeA && freeB) {
                branch = "conflict";
                guidance_.push_back({stem.id, "flag_or_beam_at_both_ends", {"flag_detection", "beam_detection", "stem_endpoints"}});
            } else if (freeA || freeB) {
                freeEnd = freeA ? 0 : 1;
                const bool hasFlag = res.flagsAt[freeEnd] > 0, hasBeam = res.beamsAt[freeEnd] > 0;
                branch = hasFlag && hasBeam ? "flag_and_beam" : (hasFlag ? "flag" : "beam");
                direction = end(stem, 1 - freeEnd).y > end(stem, freeEnd).y ? "up" : "down";
            } else {
                branch = "quarter_branch";
            }

            std::vector<std::string> regions;
            auto addRegion = [&](const std::string& purpose, const char* hypothesis, Box box, const std::string& reason) {
                area += (box.right - box.left) * (box.bottom - box.top);
                ++regionCount;
                std::ostringstream r;
                r << std::setprecision(6) << "{\"purpose\":\"" << purpose << "\",\"hypothesis\":"
                  << (hypothesis ? std::string("\"") + hypothesis + "\"" : std::string("null"))
                  << ",\"box\":{\"left\":" << box.left << ",\"top\":" << box.top << ",\"right\":" << box.right
                  << ",\"bottom\":" << box.bottom << "},\"reason\":\"" << jsonEscape(reason) << "\"}";
                regions.push_back(r.str());
            };
            const char* names[2] = {"A", "B"};
            // Head geometry: about 1.3 spaces wide, beside the stem; dots up to
            // three spaces to its right, on the head's row (+/- one space).
            auto headBox = [&](Point p, const std::string& dir) {
                const double left = dir == "up" ? p.x - 1.8 * s_ : (dir == "down" ? p.x - 0.5 * s_ : p.x - 1.8 * s_);
                const double right = dir == "up" ? p.x + 0.5 * s_ : (dir == "down" ? p.x + 1.8 * s_ : p.x + 1.8 * s_);
                return Box{left, p.y - 1.0 * s_, right, p.y + 1.0 * s_};
            };
            auto dotBox = [&](const Box& head, double top, double bottom) {
                return Box{head.right, top, head.right + 3.0 * s_, bottom};
            };
            // Chord members sit along the stem from the notehead end, never within
            // 1.5 spaces of the free end. The head of a second is set on the other
            // side of the stem, so the box spans both sides.
            auto chordBox = [&](Point h, Point f, Box* box) {
                const double towardFree = f.y < h.y ? -1.0 : 1.0;
                const double chordFar = f.y - towardFree * 1.5 * s_;
                if ((chordFar - h.y) * towardFree <= 0.5 * s_) return false;
                *box = Box{h.x - 1.8 * s_, std::min(h.y, chordFar), h.x + 1.8 * s_, std::max(h.y, chordFar)};
                return true;
            };
            if (freeEnd >= 0) {
                const int headEnd = 1 - freeEnd;
                const Point h = end(stem, headEnd), f = end(stem, freeEnd);
                const std::string because = std::string(res.flagsAt[freeEnd] ? "flag" : "beam") + " at " +
                                            names[freeEnd] + " marks it free; notehead at " + names[headEnd];
                const Box head = headBox(h, direction);
                addRegion("notehead", nullptr, head, because);
                Box chord = head;
                if (chordBox(h, f, &chord))
                    addRegion("chord_noteheads", nullptr, chord,
                              "further heads of a chord share this stem, away from the free end (a second on either side)");
                addRegion("augmentation_dots", nullptr,
                          dotBox(head, std::min(head.top, chord.top), std::max(head.bottom, chord.bottom)),
                          "dots right of the head(s)");
            } else if (branch == "quarter_branch") {
                for (int e = 0; e < 2; ++e) {
                    const Point p = end(stem, e);
                    const Box head = headBox(p, "");
                    addRegion("notehead", names[e], head,
                              std::string("no flag or beam: test end ") + names[e] + " (exactly one should hold the head)");
                    // A chord is as common without flag or beam: its members along
                    // the stem, if the head is at this end.
                    Box chord = head;
                    if (chordBox(p, end(stem, 1 - e), &chord))
                        addRegion("chord_noteheads", names[e], chord,
                                  std::string("chord members along the stem, if the head is at ") + names[e]);
                    addRegion("augmentation_dots", names[e],
                              dotBox(head, std::min(head.top, chord.top), std::max(head.bottom, chord.bottom)),
                              std::string("dots, if the head is at ") + names[e]);
                }
            }

            out << (i ? "," : "") << "{\"id\":\"" << jsonEscape(stem.id) << "\",\"branch\":\"" << branch << "\""
                << ",\"free_end\":" << (freeEnd >= 0 ? std::string("\"") + names[freeEnd] + "\"" : std::string("null"))
                << ",\"notehead_end\":" << (freeEnd >= 0 ? std::string("\"") + names[1 - freeEnd] + "\"" : std::string("null"))
                << ",\"direction\":" << (direction.empty() ? std::string("null") : "\"" + direction + "\"")
                << ",\"flags\":{\"A\":" << res.flagsAt[0] << ",\"B\":" << res.flagsAt[1] << "},\"beams\":{\"A\":"
                << res.beamsAt[0] << ",\"B\":" << res.beamsAt[1] << "}";
            // What the stem already says about the duration, before any head is seen.
            const int k = freeEnd >= 0 ? std::max(res.flagsAt[freeEnd], res.beamsAt[freeEnd]) : 0;
            out << ",\"fill_decides\":" << (branch == "quarter_branch" ? "true" : "false")
                << ",\"durations_before_heads\":[";
            if (branch == "quarter_branch") out << "\"1/4\",\"1/2\"";
            else if (k > 0) out << "\"" << Fraction::make(1, int64_t(4) << k).str() << "\"";
            out << "],\"regions\":[";
            for (size_t r = 0; r < regions.size(); ++r) out << (r ? "," : "") << regions[r];
            out << "]}";
        }
        out << "],\"region_count\":" << regionCount << ",\"region_area\":" << area << ",\"visual_guidance\":[";
        for (size_t i = 0; i < guidance_.size(); ++i) {
            out << (i ? "," : "") << "{\"target\":\"" << jsonEscape(guidance_[i].target) << "\",\"reason\":\""
                << jsonEscape(guidance_[i].reason) << "\"}";
        }
        out << "],\"next_step\":\"Detect noteheads only inside the notehead/chord regions (and dots inside the dot regions), then send ANALYZE with the stems and what was found.\"}";
        return out.str();
    }

private:
    std::string json() const {
        std::ostringstream out;
        out << std::setprecision(6);
        const bool review = !guidance_.empty();
        out << "{\"type\":\"stem_analysis\",\"status\":\"" << (review ? "review_required" : "resolved")
            << "\",\"unchecked\":{\"fill\":" << (r_.fillUnchecked ? "true" : "false")
            << ",\"dots\":" << (r_.dotsUnchecked ? "true" : "false") << "},\"stems\":[";
        for (size_t i = 0; i < stems_.size(); ++i) {
            const auto& st = stems_[i];
            const char* names[2] = {"A", "B"};
            out << (i ? "," : "") << "{\"id\":\"" << jsonEscape(st.stem->id)
                << "\",\"A\":[" << st.stem->a.x << ',' << st.stem->a.y << "],\"B\":[" << st.stem->b.x << ',' << st.stem->b.y
                << "],\"branch\":\"" << st.branch << "\",\"status\":\"" << st.status << "\"";
            if (st.freeEnd >= 0) {
                out << ",\"free_end\":\"" << names[st.freeEnd] << "\",\"notehead_end\":\"" << names[st.headEnd]
                    << "\",\"direction\":\"" << st.direction << "\"";
            } else {
                out << ",\"free_end\":null,\"notehead_end\":null,\"direction\":null";
            }
            out << ",\"flags\":{\"A\":" << st.flagsAt[0] << ",\"B\":" << st.flagsAt[1] << "},\"beams\":{\"A\":"
                << st.beamsAt[0] << ",\"B\":" << st.beamsAt[1] << "},\"hypotheses\":{\"A\":" << idsJson(st.nearEnd[0])
                << ",\"B\":" << idsJson(st.nearEnd[1]) << "},\"noteheads\":" << idsJson(st.chord);
            if (st.anchor >= 0) out << ",\"anchor_notehead\":\"" << jsonEscape(r_.noteheads[static_cast<size_t>(st.anchor)].id) << '"';
            out << ",\"noteheads_at_free_end\":" << idsJson(st.atFreeEnd) << '}';
        }
        out << "],\"events\":[";
        for (size_t i = 0; i < events_.size(); ++i) {
            const Event& e = events_[i];
            out << (i ? "," : "") << "{\"id\":\"" << jsonEscape(e.id) << "\",\"kind\":\"" << e.kind
                << "\",\"source\":\"" << e.source << "\",\"order\":" << i << ",\"x\":" << e.x
                << ",\"stem_id\":" << (e.stemId.empty() ? "null" : "\"" + jsonEscape(e.stemId) + "\"")
                << ",\"noteheads\":" << idsJson(e.heads)
                << ",\"direction\":" << (e.direction.empty() ? "null" : "\"" + e.direction + "\"")
                << ",\"dots\":" << e.dots << ",\"tuplets\":" << strList(e.tupletIds)
                << ",\"confirmed\":" << (e.confirmed ? "true" : "false")
                << ",\"basis\":\"" << jsonEscape(e.basis) << "\",\"candidates\":[";
            for (size_t c = 0; c < e.candidates.size(); ++c) {
                out << (c ? "," : "") << "{\"duration\":\"" << e.candidates[c].duration.str()
                    << "\",\"confidence\":" << e.candidates[c].confidence
                    << ",\"reading\":\"" << jsonEscape(e.candidates[c].reading)
                    << "\",\"fill\":\"" << e.candidates[c].fill << "\",\"dots\":" << e.candidates[c].dots << '}';
            }
            out << "]}";
        }
        out << "],\"visual_guidance\":[";
        for (size_t i = 0; i < guidance_.size(); ++i) {
            out << (i ? "," : "") << "{\"target\":\"" << jsonEscape(guidance_[i].target) << "\",\"reason\":\""
                << jsonEscape(guidance_[i].reason) << "\",\"requested_checks\":" << strList(guidance_[i].checks) << '}';
        }
        out << "],\"scope_note\":\"One voice per request. Stem length is never used as a rule; tolerances scale with staff spacing. Pitch comes from notehead position (pitch_context_engine); measure arithmetic from omr_constraint_solver.\"}";
        return out.str();
    }
};

void emitError(const std::string& message) {
    std::cout << "{\"type\":\"error\",\"message\":\"" << jsonEscape(message) << "\"}\n" << std::flush;
}

} // namespace

int main() {
    std::cout << "{\"type\":\"ready\",\"engine\":\"stem_first_engine\",\"version\":1}\n" << std::flush;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line.empty()) continue;
        std::istringstream input(line);
        std::string command;
        input >> command;
        for (char& c : command) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
        if (command == "EXIT" || command == "QUIT") break;
        if (command != "ANALYZE" && command != "PLAN") {
            emitError("unknown command; use PLAN, ANALYZE or EXIT");
            continue;
        }
        try {
            const Request request = parseRequest(input);
            Analyzer analyzer(request);
            std::cout << (command == "PLAN" ? analyzer.plan() : analyzer.run()) << '\n' << std::flush;
        } catch (const std::exception& error) {
            emitError(error.what());
        }
    }
    return 0;
}
