#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <map>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <unordered_map>
#include <vector>

/*
 * Music Engine — filtered search and pattern generation (C++17).
 *
 * Search is bounded: it enumerates matching patterns lazily and never stores
 * the complete combinatorial result set. Every result is emitted as JSONL.
 *
 * Build: clang++ -std=c++17 -O3 -DNDEBUG rhythm_search_engine.cpp -o rhythm_search_engine
 * Run:   ./rhythm_search_engine 4 4 256
 *
 * Commands:
 *   HELP
 *   SEARCH NOTES limit=50 max_nodes=200000 exact=note@2:1,note@4:2
 *   SEARCH RESTS limit=50 kinds=note,dotted_note,rest,triplet denominators=2,4,8
 *   GENERATE NOTES limit=20 exact=note@2:1,note@4:2 prefix=note@2
 *   EXIT
 *
 * Category NOTES emits note-only patterns. RESTS emits patterns containing
 * at least one rest, including mixed triplet slots and a full-measure rest.
 */
namespace {

std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

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

std::vector<std::string> split(const std::string& s, char delimiter) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delimiter)) if (!item.empty()) out.push_back(item);
    return out;
}

bool isPowerOfTwo(int x) { return x > 0 && (x & (x - 1)) == 0; }

uint64_t parseU64(const std::string& s, const std::string& name) {
    if (s.empty() || s[0] == '-') throw std::invalid_argument(name + " must be >= 0");
    size_t used = 0;
    unsigned long long value = std::stoull(s, &used);
    if (used != s.size()) throw std::invalid_argument("invalid integer for " + name);
    return static_cast<uint64_t>(value);
}

struct Event {
    std::string kind;
    int denominator = 0;        // Base note denominator; 0 for full-measure rest.
    uint64_t ticks = 0;          // Exact duration on the engine's integer grid.
    int durationNum = 1;         // Duration measured in whole notes.
    int durationDen = 1;
    std::vector<bool> tupletSlots{true, true, true};
    int noteSlots = 1;
    int restSlots = 0;
    std::string label;
    int actualCount = 0;
    int normalCount = 0;

    std::string key() const {
        std::string result = kind + "@" + std::to_string(denominator);
        if (kind == "tuplet")
            result += ":" + std::to_string(actualCount) + ":" + std::to_string(normalCount);
        return result;
    }
};

std::string noteName(int d) {
    switch (d) {
        case 1: return "whole note";
        case 2: return "half note";
        case 4: return "quarter note";
        case 8: return "eighth note";
        case 16: return "sixteenth note";
        case 32: return "thirty-second note";
        case 64: return "sixty-fourth note";
        case 128: return "128th note";
        case 256: return "256th note";
        default: return "1/" + std::to_string(d) + " note";
    }
}

std::string reducedFraction(int n, int d) {
    const int g = std::gcd(n, d);
    return std::to_string(n / g) + "/" + std::to_string(d / g);
}

std::string descriptor(const std::string& raw) {
    const auto p = raw.find('@');
    if (p == std::string::npos || p == 0 || p + 1 >= raw.size())
        throw std::invalid_argument("descriptor must look like note@4, triplet@8, or tuplet@8:5:4");
    const std::string kind = lower(raw.substr(0, p));
    const std::string payload = raw.substr(p + 1);
    if (kind == "tuplet") {
        const auto fields = split(payload, ':');
        if (fields.size() != 3)
            throw std::invalid_argument("tuplet descriptor must look like tuplet@8:5:4");
        const uint64_t den = parseU64(fields[0], "tuplet denominator");
        const uint64_t actual = parseU64(fields[1], "tuplet actual count");
        const uint64_t normal = parseU64(fields[2], "tuplet normal count");
        if (den == 0 || den > 256 || !isPowerOfTwo(static_cast<int>(den)))
            throw std::invalid_argument("tuplet denominator must be a power of two <= 256");
        if (actual < 3 || actual > 9 || normal < 1 || normal > 16)
            throw std::invalid_argument("tuplet counts must satisfy 3 <= actual <= 9 and 1 <= normal <= 16");
        return "tuplet@" + std::to_string(den) + ":" + std::to_string(actual) + ":" + std::to_string(normal);
    }
    const uint64_t den = parseU64(payload, "descriptor denominator");
    static const std::set<std::string> valid{
        "note", "dotted_note", "rest", "dotted_rest", "triplet", "tuplet", "measure_rest"
    };
    if (!valid.count(kind)) throw std::invalid_argument("unknown event kind in descriptor: " + kind);
    if (kind == "measure_rest") {
        if (den != 0) throw std::invalid_argument("measure_rest descriptor must be measure_rest@0");
    } else if (den > 256 || !isPowerOfTwo(static_cast<int>(den))) {
        throw std::invalid_argument("descriptor denominator must be a power of two <= 256");
    }
    return kind + "@" + std::to_string(den);
}

struct ExactItem { std::string key; int count; };

struct Filter {
    uint64_t limit = 50;
    uint64_t offset = 0;
    uint64_t maxNodes = 200000;
    int minEvents = 1;
    int maxEvents = 2048;
    int minTriplets = 0;
    int maxTriplets = 2048;
    int minDotted = 0;
    int maxDotted = 2048;
    int minRestSlots = 0;
    int maxRestSlots = 2048;
    int minNoteSlots = 0;
    int maxNoteSlots = 2048;
    uint64_t minEventTicks = 0;
    uint64_t maxEventTicks = UINT64_MAX;
    std::set<std::string> durations;
    std::set<std::string> tripletPatterns;
    std::set<std::string> tupletPatterns;
    std::set<std::string> tupletRatios;
    std::set<std::string> kinds;
    std::set<int> denominators;
    std::map<std::string, int> exact;
    std::string first;
    std::string last;
    std::vector<std::string> prefix;
    std::vector<std::string> suffix;
    std::vector<std::string> contains;
};

void parseSequence(const std::string& value, std::vector<std::string>& dest) {
    for (const auto& part : split(value, '>')) dest.push_back(descriptor(part));
}

Filter parseFilter(const std::vector<std::string>& tokens, const std::string& category) {
    Filter f;
    f.minRestSlots = (category == "RESTS") ? 1 : 0;
    f.maxRestSlots = (category == "NOTES") ? 0 : 2048;
    std::map<std::string, std::string> args;

    for (const std::string& token : tokens) {
        const size_t pos = token.find('=');
        if (pos == std::string::npos || pos == 0 || pos + 1 >= token.size())
            throw std::invalid_argument("filters must use key=value syntax: " + token);
        const std::string key = lower(token.substr(0, pos));
        if (args.count(key)) throw std::invalid_argument("duplicate filter: " + key);
        args[key] = token.substr(pos + 1);
    }

    auto number = [&](const char* key, uint64_t fallback) {
        auto it = args.find(key);
        return it == args.end() ? fallback : parseU64(it->second, key);
    };
    auto signedNumber = [&](const char* key, int fallback) {
        const uint64_t value = number(key, static_cast<uint64_t>(fallback));
        if (value > 1000000) throw std::invalid_argument(std::string(key) + " is too large");
        return static_cast<int>(value);
    };

    f.limit = number("limit", f.limit);
    f.offset = number("offset", f.offset);
    f.maxNodes = number("max_nodes", f.maxNodes);
    f.minEvents = signedNumber("min_events", f.minEvents);
    f.maxEvents = signedNumber("max_events", f.maxEvents);
    f.minTriplets = signedNumber("min_triplets", f.minTriplets);
    f.maxTriplets = signedNumber("max_triplets", f.maxTriplets);
    f.minDotted = signedNumber("min_dotted", f.minDotted);
    f.maxDotted = signedNumber("max_dotted", f.maxDotted);
    f.minRestSlots = signedNumber("min_rest_slots", f.minRestSlots);
    f.maxRestSlots = signedNumber("max_rest_slots", f.maxRestSlots);
    f.minNoteSlots = signedNumber("min_note_slots", f.minNoteSlots);
    f.maxNoteSlots = signedNumber("max_note_slots", f.maxNoteSlots);
    f.minEventTicks = number("min_event_ticks", f.minEventTicks);
    f.maxEventTicks = number("max_event_ticks", f.maxEventTicks);

    if (f.limit == 0 || f.limit > 1000) throw std::invalid_argument("limit must be 1..1000");
    if (f.maxNodes == 0 || f.maxNodes > 10000000) throw std::invalid_argument("max_nodes must be 1..10000000");
    if (f.minEvents < 0 || f.maxEvents < f.minEvents || f.maxEvents > 2048)
        throw std::invalid_argument("event bounds must satisfy 0 <= min_events <= max_events <= 2048");
    if (f.minTriplets > f.maxTriplets || f.minDotted > f.maxDotted ||
        f.minRestSlots > f.maxRestSlots || f.minNoteSlots > f.maxNoteSlots ||
        f.minEventTicks > f.maxEventTicks)
        throw std::invalid_argument("minimum filter cannot exceed maximum filter");

    if (auto it = args.find("kinds"); it != args.end()) {
        for (auto k : split(it->second, ',')) f.kinds.insert(lower(k));
        static const std::set<std::string> valid{
            "note", "dotted_note", "rest", "dotted_rest", "triplet", "measure_rest"
        };
        for (const auto& k : f.kinds) if (!valid.count(k)) throw std::invalid_argument("unknown kind: " + k);
    }

    if (auto it = args.find("denominators"); it != args.end() && args.count("subdivisions"))
        throw std::invalid_argument("use only one of denominators or subdivisions");
    auto denominatorFilter = args.find("denominators");
    if (denominatorFilter == args.end()) denominatorFilter = args.find("subdivisions");
    if (denominatorFilter != args.end()) {
        for (const auto& d : split(denominatorFilter->second, ',')) {
            const uint64_t v = parseU64(d, "denominators");
            if (v > 256 || !isPowerOfTwo(static_cast<int>(v)))
                throw std::invalid_argument("denominators/subdivisions must be powers of two <= 256");
            f.denominators.insert(static_cast<int>(v));
        }
    }

    if (auto it = args.find("exact"); it != args.end()) {
        for (const auto& item : split(it->second, ',')) {
            const size_t colon = item.rfind(':');
            if (colon == std::string::npos) throw std::invalid_argument("exact items must look like note@4:2");
            const std::string key = descriptor(item.substr(0, colon));
            const uint64_t count = parseU64(item.substr(colon + 1), "exact count");
            if (count == 0 || count > 2048) throw std::invalid_argument("exact counts must be 1..2048");
            if (f.exact.count(key)) throw std::invalid_argument("duplicate exact descriptor: " + key);
            f.exact[key] = static_cast<int>(count);
        }
    }

    if (auto it = args.find("durations"); it != args.end()) {
        for (const auto& item : split(it->second, ',')) {
            const size_t slash = item.find('/');
            if (slash == std::string::npos) throw std::invalid_argument("durations must look like 1/4 or 3/8");
            const uint64_t n = parseU64(item.substr(0, slash), "duration numerator");
            const uint64_t d = parseU64(item.substr(slash + 1), "duration denominator");
            if (n == 0 || d == 0 || n > 100000 || d > 100000)
                throw std::invalid_argument("duration fraction out of range");
            const uint64_t g = std::gcd(n, d);
            f.durations.insert(std::to_string(n/g) + "/" + std::to_string(d/g));
        }
    }
    if (auto it = args.find("triplet_patterns"); it != args.end()) {
        for (auto pattern : split(upper(it->second), ',')) {
            if (pattern.size() != 3 || pattern.find_first_not_of("NR") != std::string::npos)
                throw std::invalid_argument("triplet_patterns must use three N/R symbols, e.g. NNR, NRN, RNN");
            f.tripletPatterns.insert(pattern);
        }
    }
    if (auto it = args.find("tuplet_ratios"); it != args.end()) {
        for (const auto& ratio : split(it->second, ',')) {
            const auto parts = split(ratio, ':');
            if (parts.size() != 2) throw std::invalid_argument("tuplet_ratios must look like 5:4,6:4,7:4");
            const uint64_t actual = parseU64(parts[0], "tuplet actual count");
            const uint64_t normal = parseU64(parts[1], "tuplet normal count");
            if (actual < 3 || actual > 9 || normal < 1 || normal > 16)
                throw std::invalid_argument("tuplet ratio must satisfy 3 <= actual <= 9 and 1 <= normal <= 16");
            f.tupletRatios.insert(std::to_string(actual) + ":" + std::to_string(normal));
        }
    }
    if (auto it = args.find("tuplet_patterns"); it != args.end()) {
        for (auto pattern : split(upper(it->second), ',')) {
            const auto parts = split(pattern, ':');
            if (parts.size() != 3 || parts[2].size() != parseU64(parts[0], "tuplet pattern actual count") ||
                parts[2].find_first_not_of("NR") != std::string::npos)
                throw std::invalid_argument("tuplet_patterns must look like 5:4:NNRNN or 7:4:NNRNNRN");
            const uint64_t actual = parseU64(parts[0], "tuplet pattern actual count");
            const uint64_t normal = parseU64(parts[1], "tuplet pattern normal count");
            if (actual < 3 || actual > 9 || normal < 1 || normal > 16)
                throw std::invalid_argument("tuplet pattern ratio out of range");
            const std::string ratio = std::to_string(actual) + ":" + std::to_string(normal);
            f.tupletRatios.insert(ratio);
            f.tupletPatterns.insert(ratio + ":" + parts[2]);
        }
    }
    if (auto it = args.find("first"); it != args.end()) f.first = descriptor(it->second);
    if (auto it = args.find("last"); it != args.end()) f.last = descriptor(it->second);
    if (auto it = args.find("prefix"); it != args.end()) parseSequence(it->second, f.prefix);
    if (auto it = args.find("suffix"); it != args.end()) parseSequence(it->second, f.suffix);
    if (auto it = args.find("contains"); it != args.end()) parseSequence(it->second, f.contains);

    static const std::set<std::string> supported{
        "limit", "offset", "max_nodes", "min_events", "max_events",
        "min_triplets", "max_triplets", "min_dotted", "max_dotted",
        "min_rest_slots", "max_rest_slots", "min_note_slots", "max_note_slots",
        "min_event_ticks", "max_event_ticks", "durations", "triplet_patterns",
        "tuplet_patterns", "tuplet_ratios",
        "kinds", "denominators", "subdivisions", "exact",
        "first", "last", "prefix", "suffix", "contains"
    };
    for (const auto& pair : args)
        if (!supported.count(pair.first)) throw std::invalid_argument("unknown filter: " + pair.first);
    return f;
}

class SearchEngine {
public:
    SearchEngine(int numerator, int denominator, int maxDenominator)
        : numerator_(numerator), denominator_(denominator), maxDenominator_(maxDenominator),
          ticksPerWhole_(static_cast<uint64_t>(2520) * maxDenominator) {
        if (numerator_ < 1 || numerator_ > 10000) throw std::invalid_argument("numerator must be 1..10000");
        if (!isPowerOfTwo(denominator_) || denominator_ > maxDenominator_)
            throw std::invalid_argument("meter denominator must be a power of two <= max note denominator");
        if (!isPowerOfTwo(maxDenominator_) || maxDenominator_ < 2 || maxDenominator_ > 256)
            throw std::invalid_argument("max note denominator must be a power of two from 2 to 256");

        const uint64_t scaled = static_cast<uint64_t>(numerator_) * ticksPerWhole_;
        if (scaled % static_cast<uint64_t>(denominator_) != 0)
            throw std::invalid_argument("meter cannot be represented exactly on selected time grid");
        targetTicks_ = scaled / static_cast<uint64_t>(denominator_);
        if (targetTicks_ > 50000000) throw std::invalid_argument("meter too large for this search grid");

        buildEvents();
    }

    std::string readyJson() const {
        std::ostringstream out;
        out << "{\"type\":\"ready\",\"meter\":\"" << numerator_ << '/' << denominator_
            << "\",\"max_note_denominator\":" << maxDenominator_
            << ",\"target_ticks\":" << targetTicks_
            << ",\"events_notes_only\":" << noteEvents_.size()
            << ",\"events_with_rests\":" << restEvents_.size()
            << ",\"tuplet_ratio_range\":{\"actual_min\":3,\"actual_max\":9,\"normal_min\":1,\"normal_max\":16}"
            << ",\"protocol\":\"SEARCH|GENERATE category key=value ...\"}";
        return out.str();
    }

    void search(const std::string& category, const Filter& filter) const {
        if (category != "NOTES" && category != "RESTS")
            throw std::invalid_argument("category must be NOTES or RESTS");
        const std::vector<Event>& baseEvents = category == "NOTES" ? noteEvents_ : restEvents_;
        std::vector<Event> queryEvents = baseEvents;

        // Ratios requested by a caller are generated on demand, so the engine
        // can support uncommon but valid tuplets without bloating every search.
        std::set<std::string> availableRatios;
        for (const auto& event : queryEvents) {
            if (event.kind == "tuplet" || event.kind == "triplet") {
                availableRatios.insert(std::to_string(event.actualCount) + ":" +
                                        std::to_string(event.normalCount));
            }
        }
        for (const auto& ratio : filter.tupletRatios) {
            if (availableRatios.count(ratio)) continue;
            const auto parts = split(ratio, ':');
            const int actual = static_cast<int>(parseU64(parts.at(0), "tuplet actual count"));
            const int normal = static_cast<int>(parseU64(parts.at(1), "tuplet normal count"));
            addTupletEvents(queryEvents, category == "RESTS", actual, normal, filter);
        }

        SearchRun run(*this, queryEvents, category, filter);
        run.execute();
    }

private:
    int numerator_;
    int denominator_;
    int maxDenominator_;
    uint64_t ticksPerWhole_;
    uint64_t targetTicks_ = 0;
    std::vector<Event> noteEvents_;
    std::vector<Event> restEvents_;

    void addTupletEvents(std::vector<Event>& out, bool includeRests, int actual, int normal,
                         const Filter& filter) const {
        const std::string ratio = std::to_string(actual) + ":" + std::to_string(normal);
        std::set<std::string> requestedMasks;
        for (const auto& pattern : filter.tupletPatterns) {
            const auto parts = split(pattern, ':');
            if (parts.size() == 3 && parts[0] + ":" + parts[1] == ratio)
                requestedMasks.insert(parts[2]);
        }

        for (int d = 1; d <= maxDenominator_; d *= 2) {
            const uint64_t groupTicks =
                (static_cast<uint64_t>(normal) * ticksPerWhole_) / static_cast<uint64_t>(d);
            const bool isTriplet = actual == 3 && normal == 2;
            const int lastMask = (1 << actual) - 1;
            for (int mask = 0; mask <= lastMask; ++mask) {
                std::string maskText;
                std::vector<bool> slots;
                slots.reserve(static_cast<size_t>(actual));
                int notes = 0;
                for (int slot = 0; slot < actual; ++slot) {
                    const bool isNote = (mask & (1 << slot)) != 0;
                    slots.push_back(isNote);
                    maskText += isNote ? 'N' : 'R';
                    notes += isNote ? 1 : 0;
                }
                const int rests = actual - notes;
                if (!includeRests && rests > 0) continue;
                if (includeRests && rests == 0) continue;
                if (!requestedMasks.empty() && !requestedMasks.count(maskText)) continue;

                std::ostringstream label;
                if (isTriplet) label << "triplet(";
                else label << "tuplet " << actual << ":" << normal << "(";
                for (int i = 0; i < actual; ++i) {
                    if (i) label << ',';
                    label << (slots[static_cast<size_t>(i)] ? "note" : "rest");
                }
                label << ") of " << noteName(d);
                Event event{
                    isTriplet ? "triplet" : "tuplet", d, groupTicks, normal, d,
                    slots, notes, rests, label.str(), actual, normal
                };
                out.push_back(std::move(event));
            }
            if (d > maxDenominator_ / 2) break;
        }
    }

    void addCommonEvents(std::vector<Event>& out, bool includeRests) const {
        for (int d = 1; d <= maxDenominator_; d *= 2) {
            const uint64_t normalTicks = ticksPerWhole_ / static_cast<uint64_t>(d);
            const uint64_t dottedTicks = (3 * ticksPerWhole_) / (2 * static_cast<uint64_t>(d));
            const std::string name = noteName(d);

            out.push_back(Event{"note", d, normalTicks, 1, d, {true}, 1, 0, name});
            out.push_back(Event{"dotted_note", d, dottedTicks, 3, 2*d, {true}, 1, 0,
                                name + " dotted"});

            if (includeRests) {
                out.push_back(Event{"rest", d, normalTicks, 1, d, {false}, 0, 1,
                                    "rest of " + name});
                out.push_back(Event{"dotted_rest", d, dottedTicks, 3, 2*d, {false}, 0, 1,
                                    "dotted rest of " + name});
            }

            const std::vector<std::pair<int,int>> ratios{{3,2},{4,3},{5,4},{6,4},{7,4},{9,8}};
            for (const auto& ratio : ratios) {
                const int actual = ratio.first;
                const int normal = ratio.second;
                const uint64_t groupTicks =
                    (static_cast<uint64_t>(normal) * ticksPerWhole_) / static_cast<uint64_t>(d);
                const int firstMask = includeRests ? 0 : ((1 << actual) - 1);
                const int lastMask = (1 << actual) - 1;
                for (int mask = firstMask; mask <= lastMask; ++mask) {
                    std::vector<bool> slots;
                    slots.reserve(static_cast<size_t>(actual));
                    int notes = 0;
                    for (int slot = 0; slot < actual; ++slot) {
                        const bool isNote = (mask & (1 << slot)) != 0;
                        slots.push_back(isNote);
                        notes += isNote ? 1 : 0;
                    }
                    const int rests = actual - notes;
                    if (includeRests && rests == 0) continue;
                    std::ostringstream label;
                    const bool isTriplet = actual == 3 && normal == 2;
                    if (isTriplet) label << "triplet(";
                    else label << "tuplet " << actual << ":" << normal << "(";
                    for (int i = 0; i < actual; ++i) {
                        if (i) label << ',';
                        label << (slots[static_cast<size_t>(i)] ? "note" : "rest");
                    }
                    label << ") of " << name;
                    Event event{
                        isTriplet ? "triplet" : "tuplet", d, groupTicks, normal, d,
                        slots, notes, rests, label.str(), actual, normal
                    };
                    out.push_back(std::move(event));
                }
            }
            if (d > maxDenominator_ / 2) break;
        }
    }

    void buildEvents() {
        addCommonEvents(noteEvents_, false);
        addCommonEvents(restEvents_, true);
        if (targetTicks_ != ticksPerWhole_) {
            restEvents_.push_back(Event{"measure_rest", 0, targetTicks_, numerator_, denominator_,
                                        {false,false,false}, 0, 1, "full-measure rest"});
        }
        auto order = [](const Event& a, const Event& b) {
            if (a.ticks != b.ticks) return a.ticks > b.ticks;
            if (a.kind != b.kind) return a.kind < b.kind;
            if (a.denominator != b.denominator) return a.denominator < b.denominator;
            return a.tupletSlots < b.tupletSlots;
        };
        std::stable_sort(noteEvents_.begin(), noteEvents_.end(), order);
        std::stable_sort(restEvents_.begin(), restEvents_.end(), order);
    }

    class SearchRun {
    public:
        SearchRun(const SearchEngine& engine, const std::vector<Event>& events,
                  std::string category, Filter filter)
            : e_(engine), events_(events), category_(std::move(category)), f_(std::move(filter)),
              remainingExact_(f_.exact) {
            for (const auto& event : events_) {
                if (event.ticks < f_.minEventTicks || event.ticks > f_.maxEventTicks) continue;
                if (!f_.kinds.empty() && !f_.kinds.count(event.kind)) continue;
                if (!f_.denominators.empty() && !f_.denominators.count(event.denominator)) continue;
                if (!f_.durations.empty() && !f_.durations.count(reducedFraction(event.durationNum,event.durationDen))) continue;
                allowed_.push_back(&event);
                minTicks_ = std::min(minTicks_, event.ticks);
                maxTicks_ = std::max(maxTicks_, event.ticks);
            }
            if (f_.minRestSlots == 0 && category_ == "RESTS") f_.minRestSlots = 1;
            if (!f_.exact.empty()) {
                exactTicks_ = 0;
                exactEvents_ = 0;
                for (const auto& [key,count] : f_.exact) {
                    const Event* found = nullptr;
                    for (const auto* event : allowed_) if (event->key() == key) { found = event; break; }
                    if (!found) { impossible_ = true; continue; }
                    exactTicks_ += found->ticks * static_cast<uint64_t>(count);
                    exactEvents_ += count;
                }
                if (exactTicks_ != e_.targetTicks_ || exactEvents_ > f_.maxEvents || exactEvents_ < f_.minEvents)
                    impossible_ = true;
            }
            if (allowed_.empty()) impossible_ = true;
            buildContainsAutomaton();
        }

        void execute() {
            if (!impossible_) dfs(e_.targetTicks_, 0, 0, 0, 0, 0);
            std::cout << "{\"type\":\"search_summary\",\"category\":\"" << category_
                      << "\",\"meter\":\"" << e_.numerator_ << '/' << e_.denominator_
                      << "\",\"returned\":" << returned_
                      << ",\"matches_seen\":" << matchesSeen_
                      << ",\"nodes_visited\":" << nodes_
                      << ",\"truncated\":" << (truncated_ ? "true" : "false")
                      << ",\"has_more\":" << (hasMore_ ? "true" : "false")
                      << ",\"max_nodes\":" << f_.maxNodes << "}\n" << std::flush;
        }

    private:
        const SearchEngine& e_;
        const std::vector<Event>& events_;
        std::string category_;
        Filter f_;
        std::vector<const Event*> allowed_;
        std::map<std::string,int> remainingExact_;
        uint64_t exactTicks_ = 0;
        int exactEvents_ = 0;
        uint64_t nodes_ = 0;
        uint64_t matchesSeen_ = 0;
        uint64_t returned_ = 0;
        bool stopped_ = false;
        bool truncated_ = false;
        bool hasMore_ = false;
        bool impossible_ = false;
        uint64_t minTicks_ = UINT64_MAX;
        uint64_t maxTicks_ = 0;
        std::vector<const Event*> chosen_;

        // A precomputed KMP transition table makes each DFS push/pop transition
        // O(1), rather than rescanning the current prefix at every terminal node.
        std::unordered_map<std::string, size_t> containsSymbolIndex_;
        std::vector<std::vector<size_t>> containsNext_;
        std::vector<std::vector<unsigned char>> containsMatch_;
        std::vector<size_t> containsStateStack_{0};
        std::vector<unsigned char> containsMatchedStack_{0};

        void buildContainsAutomaton() {
            containsStateStack_.assign(1, 0);
            containsMatchedStack_.assign(1, 0);
            if (f_.contains.empty()) return;

            for (const Event* event : allowed_) {
                const std::string key = event->key();
                if (!containsSymbolIndex_.count(key)) {
                    containsSymbolIndex_[key] = containsSymbolIndex_.size();
                }
            }
            for (const std::string& key : f_.contains) {
                if (!containsSymbolIndex_.count(key)) {
                    impossible_ = true;
                    return;
                }
            }

            const size_t m = f_.contains.size();
            const size_t alphabetSize = containsSymbolIndex_.size();
            std::vector<size_t> prefix(m, 0);
            for (size_t i = 1, j = 0; i < m; ++i) {
                while (j > 0 && f_.contains[i] != f_.contains[j]) j = prefix[j - 1];
                if (f_.contains[i] == f_.contains[j]) ++j;
                prefix[i] = j;
            }

            containsNext_.assign(m, std::vector<size_t>(alphabetSize, 0));
            containsMatch_.assign(m, std::vector<unsigned char>(alphabetSize, 0));

            // Build the deterministic KMP automaton from shorter prefix states.
            for (size_t state = 0; state < m; ++state) {
                for (const auto& symbol : containsSymbolIndex_) {
                    const size_t symbolIndex = symbol.second;
                    size_t nextState = 0;
                    bool matched = false;

                    if (f_.contains[state] == symbol.first) {
                        nextState = state + 1;
                        if (nextState == m) {
                            matched = true;
                            nextState = prefix[m - 1];
                        }
                    } else if (state > 0) {
                        const size_t fallback = prefix[state - 1];
                        nextState = containsNext_[fallback][symbolIndex];
                        matched = containsMatch_[fallback][symbolIndex] != 0;
                    }

                    containsNext_[state][symbolIndex] = nextState;
                    containsMatch_[state][symbolIndex] = matched ? 1 : 0;
                }
            }
        }

        void pushContainsState(const Event& event) {
            if (f_.contains.empty()) {
                containsStateStack_.push_back(0);
                containsMatchedStack_.push_back(0);
                return;
            }

            const size_t state = containsStateStack_.back();
            const auto symbol = containsSymbolIndex_.find(event.key());
            if (symbol == containsSymbolIndex_.end()) {
                containsStateStack_.push_back(state);
                containsMatchedStack_.push_back(containsMatchedStack_.back());
                return;
            }

            const size_t index = symbol->second;
            containsStateStack_.push_back(containsNext_[state][index]);
            containsMatchedStack_.push_back(
                (containsMatchedStack_.back() || containsMatch_[state][index]) ? 1 : 0
            );
        }

        bool eventAllowed(const Event& event) const {
            if (!f_.kinds.empty() && !f_.kinds.count(event.kind)) return false;
            if (!f_.denominators.empty() && !f_.denominators.count(event.denominator)) return false;
            if (event.ticks < f_.minEventTicks || event.ticks > f_.maxEventTicks) return false;
            if (!f_.durations.empty() && !f_.durations.count(reducedFraction(event.durationNum,event.durationDen))) return false;
            if (event.kind == "triplet" && !f_.tripletPatterns.empty()) {
                std::string mask;
                for (bool slot : event.tupletSlots) mask += slot ? 'N' : 'R';
                if (!f_.tripletPatterns.count(mask)) return false;
            }
            if (event.kind == "tuplet" || event.kind == "triplet") {
                const std::string ratio = std::to_string(event.actualCount) + ":" +
                                          std::to_string(event.normalCount);
                if (!f_.tupletRatios.empty() && !f_.tupletRatios.count(ratio)) return false;
                if (!f_.tupletPatterns.empty()) {
                    std::string mask;
                    for (bool slot : event.tupletSlots) mask += slot ? 'N' : 'R';
                    if (!f_.tupletPatterns.count(ratio + ":" + mask)) return false;
                }
            }
            if (!f_.exact.empty()) {
                auto it = remainingExact_.find(event.key());
                if (it == remainingExact_.end() || it->second <= 0) return false;
            }
            if (chosen_.empty() && !f_.first.empty() && event.key() != f_.first) return false;
            return true;
        }

        bool sequenceAtEnd(const std::vector<std::string>& seq, bool suffix) const {
            if (seq.empty()) return true;
            if (seq.size() > chosen_.size()) return false;
            const size_t start = suffix ? chosen_.size() - seq.size() : 0;
            for (size_t i=0;i<seq.size();++i)
                if (chosen_[start+i]->key() != seq[i]) return false;
            return true;
        }

        void emitMatch() {
            if (matchesSeen_ < f_.offset) { ++matchesSeen_; return; }
            if (returned_ >= f_.limit) { hasMore_ = true; stopped_ = true; return; }
            std::cout << patternJson() << '\n';
            ++matchesSeen_;
            ++returned_;
        }

        std::string patternJson() const {
            std::ostringstream out;
            out << "{\"type\":\"pattern\",\"category\":\"" << category_
                << "\",\"meter\":\"" << e_.numerator_ << '/' << e_.denominator_
                << "\",\"total_ticks\":" << e_.targetTicks_ << ",\"event_count\":" << chosen_.size()
                << ",\"events\":[";
            int notes=0, rests=0, triplets=0, tuplets=0, dotted=0;
            for (size_t i=0;i<chosen_.size();++i) {
                const Event& x = *chosen_[i];
                if (i) out << ',';
                notes += x.noteSlots; rests += x.restSlots;
                triplets += x.kind == "triplet";
                tuplets += x.kind == "triplet" || x.kind == "tuplet";
                dotted += x.kind == "dotted_note" || x.kind == "dotted_rest";
                out << "{\"kind\":\"" << x.kind << "\",\"descriptor\":\"" << x.key()
                    << "\",\"base_denominator\":" << x.denominator
                    << ",\"duration_ticks\":" << x.ticks
                    << ",\"duration_whole\":\"" << reducedFraction(x.durationNum,x.durationDen)
                    << "\",\"note_slots\":" << x.noteSlots << ",\"rest_slots\":" << x.restSlots;
                if (x.kind == "triplet" || x.kind == "tuplet") {
                    out << ",\"tuplet_ratio\":\"" << x.actualCount << ":" << x.normalCount << "\"";
                    out << ",\"tuplet_slots\":[";
                    for (size_t j=0;j<x.tupletSlots.size();++j) {
                        if (j) out << ',';
                        out << '"' << (x.tupletSlots[j] ? "note" : "rest") << '"';
                    }
                    out << "],\"tuplet_slot_duration_whole\":\""
                        << reducedFraction(x.normalCount, x.actualCount*x.denominator) << "\"";
                    if (x.kind == "triplet") {
                        out << ",\"triplet_slots\":[";
                        for (size_t j=0;j<x.tupletSlots.size();++j) {
                            if (j) out << ',';
                            out << '"' << (x.tupletSlots[j] ? "note" : "rest") << '"';
                        }
                        out << "],\"triplet_slot_duration_whole\":\""
                            << reducedFraction(x.normalCount, x.actualCount*x.denominator) << "\"";
                    }
                }
                out << ",\"label\":\"" << jsonEscape(x.label) << "\"}";
            }
            out << "],\"summary\":{\"note_slots\":" << notes << ",\"rest_slots\":" << rests
                << ",\"triplet_groups\":" << triplets << ",\"tuplet_groups\":" << tuplets
                << ",\"dotted_events\":" << dotted << "}}";
            return out.str();
        }

        void dfs(uint64_t remainingTicks, int triplets, int dotted, int restSlots, int noteSlots, int depth) {
            if (stopped_) return;
            if (nodes_ >= f_.maxNodes) { truncated_ = true; stopped_ = true; return; }
            ++nodes_;

            if (depth > f_.maxEvents || triplets > f_.maxTriplets || dotted > f_.maxDotted ||
                restSlots > f_.maxRestSlots || noteSlots > f_.maxNoteSlots) return;
            if (f_.exact.empty()) {
                if (depth == f_.maxEvents && remainingTicks != 0) return;
                if (minTicks_ != UINT64_MAX && remainingTicks > 0) {
                    const uint64_t maximumFutureEvents = remainingTicks / minTicks_;
                    if (depth + static_cast<int>(maximumFutureEvents) < f_.minEvents) return;
                }
                if (maxTicks_ > 0) {
                    const uint64_t minimumFutureEvents = (remainingTicks + maxTicks_ - 1) / maxTicks_;
                    if (depth + static_cast<int>(minimumFutureEvents) > f_.maxEvents) return;
                }
            } else {
                if (remainingTicks != exactTicks_) return;
                if (depth + countRemainingExact() > f_.maxEvents) return;
            }

            if (remainingTicks == 0) {
                if (depth < f_.minEvents || depth > f_.maxEvents ||
                    triplets < f_.minTriplets || triplets > f_.maxTriplets ||
                    dotted < f_.minDotted || dotted > f_.maxDotted ||
                    restSlots < f_.minRestSlots || restSlots > f_.maxRestSlots ||
                    noteSlots < f_.minNoteSlots || noteSlots > f_.maxNoteSlots) return;
                if (!f_.last.empty() && (chosen_.empty() || chosen_.back()->key() != f_.last)) return;
                if (!sequenceAtEnd(f_.prefix, false) || !sequenceAtEnd(f_.suffix, true)) return;
                if (!f_.contains.empty() && !containsMatchedStack_.back()) return;
                if (!f_.exact.empty()) {
                    for (const auto& pair : remainingExact_) if (pair.second != 0) return;
                }
                emitMatch();
                return;
            }

            for (const Event* event : allowed_) {
                if (stopped_) break;
                if (event->ticks > remainingTicks || !eventAllowed(*event)) continue;
                if (event->kind == "triplet" && triplets >= f_.maxTriplets) continue;
                if ((event->kind == "dotted_note" || event->kind == "dotted_rest") && dotted >= f_.maxDotted) continue;
                if (restSlots + event->restSlots > f_.maxRestSlots) continue;
                if (noteSlots + event->noteSlots > f_.maxNoteSlots) continue;

                if (!f_.exact.empty()) {
                    auto it = remainingExact_.find(event->key());
                    if (it == remainingExact_.end() || it->second <= 0) continue;
                    --it->second;
                    exactTicks_ -= event->ticks;
                }

                chosen_.push_back(event);
                pushContainsState(*event);
                dfs(remainingTicks - event->ticks,
                    triplets + (event->kind == "triplet"),
                    dotted + (event->kind == "dotted_note" || event->kind == "dotted_rest"),
                    restSlots + event->restSlots,
                    noteSlots + event->noteSlots,
                    depth + 1);
                containsStateStack_.pop_back();
                containsMatchedStack_.pop_back();
                chosen_.pop_back();

                if (!f_.exact.empty()) {
                    ++remainingExact_[event->key()];
                    exactTicks_ += event->ticks;
                }
            }
        }

        int countRemainingExact() const {
            int total = 0;
            for (const auto& p : remainingExact_) total += p.second;
            return total;
        }
    };
};

void emitError(const std::string& message) {
    std::cout << "{\"type\":\"error\",\"message\":\"" << jsonEscape(message) << "\"}\n" << std::flush;
}

} // namespace

int main(int argc, char** argv) {
    try {
        int numerator = 4, denominator = 4, maxDenominator = 256;
        if (argc == 4) {
            numerator = std::stoi(argv[1]);
            denominator = std::stoi(argv[2]);
            maxDenominator = std::stoi(argv[3]);
        } else if (argc != 1) {
            std::cerr << "Usage: " << argv[0] << " [numerator denominator max_note_denominator]\n";
            return 2;
        }

        SearchEngine engine(numerator, denominator, maxDenominator);
        std::cout << engine.readyJson() << '\n' << std::flush;
        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.empty()) continue;
            std::istringstream input(line);
            std::string command, category;
            input >> command;
            command = upper(command);
            if (command == "EXIT" || command == "QUIT") break;
            if (command == "HELP") {
                std::cout << "{\"type\":\"help\",\"syntax\":\"SEARCH|GENERATE NOTES|RESTS key=value ...\","
                          << "\"filters\":[\"limit\",\"offset\",\"max_nodes\",\"min_events\",\"max_events\","
                          << "\"min_triplets\",\"max_triplets\",\"min_dotted\",\"max_dotted\","
                          << "\"min_rest_slots\",\"max_rest_slots\",\"min_note_slots\",\"max_note_slots\","
                          << "\"min_event_ticks\",\"max_event_ticks\",\"durations\",\"triplet_patterns\",\"tuplet_patterns\",\"tuplet_ratios\",\"kinds\",\"denominators\",\"subdivisions\",\"exact\","
                          << "\"first\",\"last\",\"prefix\",\"suffix\",\"contains\"]}\n" << std::flush;
                continue;
            }
            if (command != "SEARCH" && command != "GENERATE") {
                emitError("unknown command; use HELP");
                continue;
            }
            input >> category;
            category = upper(category);
            std::vector<std::string> tokens;
            std::string token;
            while (input >> token) tokens.push_back(token);
            try {
                Filter filter = parseFilter(tokens, category);
                engine.search(category, filter);
            } catch (const std::exception& error) {
                emitError(error.what());
            }
        }
    } catch (const std::exception& error) {
        std::cerr << "Startup error: " << error.what() << '\n';
        return 1;
    }
    return 0;
}
