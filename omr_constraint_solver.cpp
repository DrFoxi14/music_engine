#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <unordered_map>
#include <numeric>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

/*
 * OMR Constraint Solver — exact rhythmic duration resolution.
 *
 * Input is a candidate list produced by the Visual Engine. This program does
 * not inspect pixels. It tests which visually plausible candidate durations
 * can exactly fill a measure, preserves ambiguity, ranks valid solutions by
 * supplied visual confidence, and reports regions/features worth rechecking.
 *
 * Build:
 *   clang++ -std=c++17 -O3 -DNDEBUG omr_constraint_solver.cpp -o omr_constraint_solver
 *
 * Protocol (one request per line):
 *   RESOLVE 4 4 256 limit=20 max_nodes=100000 \
 *     noteA=1/2:0.99|1/4:0.52 noteB=1/4:0.98 noteC=1/2:0.52|1/4:0.48
 *   EXIT
 *
 * Durations are fractions of a whole note. Confidence is a soft ranking
 * signal, never a substitute for hard visual or musical constraints.
 */
namespace {

struct Rational {
    uint64_t n = 0;
    uint64_t d = 1;

    Rational(uint64_t numerator = 0, uint64_t denominator = 1) : n(numerator), d(denominator) {
        if (d == 0) throw std::invalid_argument("fraction denominator cannot be zero");
        const uint64_t g = std::gcd(n, d);
        n /= g;
        d /= g;
    }

    std::string str() const {
        return std::to_string(n) + "/" + std::to_string(d);
    }
};

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

std::vector<std::string> split(const std::string& text, char delimiter) {
    std::vector<std::string> result;
    std::stringstream ss(text);
    std::string item;
    while (std::getline(ss, item, delimiter)) if (!item.empty()) result.push_back(item);
    return result;
}

uint64_t parseUInt(const std::string& text, const std::string& label) {
    if (text.empty() || text.front() == '-') throw std::invalid_argument(label + " must be non-negative");
    size_t used = 0;
    const unsigned long long value = std::stoull(text, &used);
    if (used != text.size()) throw std::invalid_argument("invalid " + label);
    return static_cast<uint64_t>(value);
}

bool isPowerOfTwo(uint64_t x) { return x && !(x & (x - 1)); }

Rational parseFraction(const std::string& text) {
    const size_t slash = text.find('/');
    if (slash == std::string::npos) throw std::invalid_argument("duration must be a fraction such as 1/4");
    const uint64_t n = parseUInt(text.substr(0, slash), "duration numerator");
    const uint64_t d = parseUInt(text.substr(slash + 1), "duration denominator");
    if (n == 0 || d == 0 || n > 1000000 || d > 1000000)
        throw std::invalid_argument("duration fraction is out of supported range");
    return Rational(n, d);
}

struct Candidate {
    Rational duration;
    uint64_t ticks = 0;
    double confidence = 1.0;
};

struct InputEvent {
    std::string id;
    std::vector<Candidate> candidates;
};

struct Solution {
    std::vector<size_t> choices;
    double logScore = 0.0;
};

struct SearchConfig {
    uint64_t maxNodes = 200000;
    size_t limit = 20;
};

double parseConfidence(const std::string& text) {
    size_t used = 0;
    const double value = std::stod(text, &used);
    if (used != text.size() || !std::isfinite(value) || value <= 0.0 || value > 1.0)
        throw std::invalid_argument("candidate confidence must be in (0, 1]");
    return value;
}

InputEvent parseEvent(const std::string& token) {
    const size_t equals = token.find('=');
    if (equals == std::string::npos || equals == 0 || equals + 1 >= token.size())
        throw std::invalid_argument("event must look like id=1/2:0.9|1/4:0.7");

    InputEvent event;
    event.id = token.substr(0, equals);
    for (unsigned char c : event.id) {
        if (!(std::isalnum(c) || c == '_' || c == '-'))
            throw std::invalid_argument("event ids may contain only letters, digits, _ and -");
    }

    for (const std::string& rawCandidate : split(token.substr(equals + 1), '|')) {
        const size_t colon = rawCandidate.find(':');
        const Rational duration = parseFraction(rawCandidate.substr(0, colon));
        const double confidence =
            colon == std::string::npos ? 1.0 : parseConfidence(rawCandidate.substr(colon + 1));

        // Ticks are assigned once the request's exact grid is known.
        event.candidates.push_back(Candidate{duration, 0, confidence});
    }

    if (event.candidates.empty()) throw std::invalid_argument("event must have at least one candidate");
    std::stable_sort(event.candidates.begin(), event.candidates.end(),
        [](const Candidate& a, const Candidate& b) { return a.confidence > b.confidence; });
    return event;
}

std::vector<std::string> suggestedChecks(const std::vector<Rational>& durations) {
    std::set<std::string> values;
    for (const auto& duration : durations) values.insert(duration.str());

    std::vector<std::string> checks;
    if (values.count("1/2") && values.count("1/4"))
        checks.push_back("notehead_fill");
    if ((values.count("1/4") && values.count("1/8")) ||
        (values.count("1/8") && values.count("1/16")) ||
        (values.count("1/16") && values.count("1/32")))
        checks.push_back("flag_or_beam_count");

    bool dottedPair = false;
    for (size_t i = 0; i < durations.size(); ++i) {
        for (size_t j = i + 1; j < durations.size(); ++j) {
            const uint64_t left = durations[i].n * durations[j].d;
            const uint64_t right = durations[j].n * durations[i].d;
            const uint64_t lo = std::min(left, right);
            const uint64_t hi = std::max(left, right);
            if (hi * 2 == lo * 3) dottedPair = true;
        }
    }
    if (dottedPair) checks.push_back("augmentation_dot");
    if (checks.empty()) checks.push_back("reinspect_symbol_and_local_context");
    return checks;
}

// Base ticks per whole note: lcm(1..12) * max_note_denominator, the same grid
// as Search Engine. Each request extends it with the lcm of every candidate
// denominator, so any exact rational duration is representable: a 13-tuplet,
// or nested tuplets such as 9 inside 9 (slot 1/81 of the unit).
constexpr uint64_t kTupletGridFactor = 27720;
// Keeps measure * 16 * 2048 events inside uint64 for the DP sums.
constexpr uint64_t kMaxTargetTicks = uint64_t(1) << 47;

uint64_t ticksPerWholeFor(int maxDenominator) {
    return kTupletGridFactor * static_cast<uint64_t>(maxDenominator);
}

uint64_t checkedLcm(uint64_t a, uint64_t b) {
    const uint64_t g = std::gcd(a, b);
    const uint64_t step = b / g;
    if (a > kMaxTargetTicks / step)
        throw std::invalid_argument("durations are too fine to share one exact grid (lcm of denominators too large)");
    return a * step;
}

uint64_t saturatingMul(uint64_t a, uint64_t b, bool& saturated) {
    if (a != 0 && b > UINT64_MAX / a) {
        saturated = true;
        return UINT64_MAX;
    }
    return a * b;
}

uint64_t saturatingAdd(uint64_t a, uint64_t b, bool& saturated) {
    if (a > UINT64_MAX - b) {
        saturated = true;
        return UINT64_MAX;
    }
    return a + b;
}

class Solver {
public:
    Solver(int numerator, int denominator, int maxDenominator,
           SearchConfig config, std::vector<InputEvent> events)
        : numerator_(numerator), denominator_(denominator),
          maxDenominator_(maxDenominator), config_(config), events_(std::move(events)),
          ticksPerWhole_(0) {
        if (numerator_ <= 0 || numerator_ > 10000) throw std::invalid_argument("meter numerator must be 1..10000");
        if (!isPowerOfTwo(static_cast<uint64_t>(denominator_)) ||
            static_cast<uint64_t>(denominator_) > static_cast<uint64_t>(maxDenominator_))
            throw std::invalid_argument("meter denominator must be a power of two <= max denominator");
        if (!isPowerOfTwo(static_cast<uint64_t>(maxDenominator_)) ||
            maxDenominator_ < 2 || maxDenominator_ > 256)
            throw std::invalid_argument("max denominator must be a power of two from 2 to 256");
        if (events_.empty()) throw std::invalid_argument("at least one event is required");
        if (events_.size() > 2048) throw std::invalid_argument("at most 2048 ordered events are supported");

        ticksPerWhole_ = ticksPerWholeFor(maxDenominator_);
        for (const auto& event : events_)
            for (const auto& candidate : event.candidates) ticksPerWhole_ = checkedLcm(ticksPerWhole_, candidate.duration.d);
        if (static_cast<uint64_t>(numerator_) > kMaxTargetTicks / ticksPerWhole_)
            throw std::invalid_argument("measure is too long for this grid");
        for (auto& event : events_) {
            for (auto& candidate : event.candidates) {
                const uint64_t per = ticksPerWhole_ / candidate.duration.d;
                if (candidate.duration.n > kMaxTargetTicks / per)
                    throw std::invalid_argument("duration is too large: " + candidate.duration.str());
                candidate.ticks = candidate.duration.n * per;
            }
        }

        const uint64_t scaled = static_cast<uint64_t>(numerator_) * ticksPerWhole_;
        if (scaled % static_cast<uint64_t>(denominator_) != 0)
            throw std::invalid_argument("meter cannot be represented at this resolution");
        targetTicks_ = scaled / static_cast<uint64_t>(denominator_);

        for (const auto& event : events_) {
            if (!ids_.insert(event.id).second) throw std::invalid_argument("duplicate event id: " + event.id);
            for (const auto& candidate : event.candidates) {
                if (candidate.ticks > targetTicks_ * 16)
                    throw std::invalid_argument("candidate duration is implausibly larger than measure");
            }
        }

        // Bounded by 2048 events * 16 measures each, well inside uint64.
        suffixMin_.assign(events_.size() + 1, 0);
        suffixMax_.assign(events_.size() + 1, 0);
        for (size_t i = events_.size(); i-- > 0;) {
            uint64_t minimum = UINT64_MAX;
            uint64_t maximum = 0;
            for (const auto& c : events_[i].candidates) {
                minimum = std::min(minimum, c.ticks);
                maximum = std::max(maximum, c.ticks);
            }
            suffixMin_[i] = suffixMin_[i + 1] + minimum;
            suffixMax_[i] = suffixMax_[i + 1] + maximum;
        }

        support_.resize(events_.size());
        candidateCounts_.resize(events_.size());
        for (size_t i = 0; i < events_.size(); ++i) {
            support_[i].assign(events_[i].candidates.size(), false);
            candidateCounts_[i].assign(events_[i].candidates.size(), 0);
            candidateLog2_.push_back(std::vector<double>(events_[i].candidates.size(), kNoCount));
        }
    }

    std::string solve() {
        runDynamicProgram();

        std::ostringstream out;
        const char* status = truncated_ ? "incomplete" :
            (validCount_ == 0 ? "inconsistent" : (validCount_ == 1 ? "resolved_unique" : "ambiguous"));

        out << "{\"type\":\"resolution\",\"status\":\"" << status
            << "\",\"meter\":\"" << numerator_ << '/' << denominator_
            << "\",\"measure_duration_whole\":\"" << Rational(numerator_, denominator_).str()
            << "\",\"nodes_visited\":" << nodesVisited_
            << ",\"valid_assignments\":" << validCount_
            << ",\"valid_assignments_is_lower_bound\":" << (countSaturated_ ? "true" : "false")
            << ",\"valid_assignments_log2\":" << (validLog2_ == kNoCount ? std::string("null") : std::to_string(validLog2_))
            << ",\"solutions_limited_by_memory\":" << (solutionsLimitedByMemory_ ? "true" : "false")
            << ",\"truncated\":" << (truncated_ ? "true" : "false")
            << ",\"solutions\":[";
        for (size_t s = 0; s < solutions_.size(); ++s) {
            if (s) out << ',';
            appendSolution(out, solutions_[s]);
        }
        out << "],\"event_support\":[";
        for (size_t i = 0; i < events_.size(); ++i) {
            if (i) out << ',';
            out << "{\"event_id\":\"" << jsonEscape(events_[i].id) << "\",\"input_candidates\":[";
            for (size_t c = 0; c < events_[i].candidates.size(); ++c) {
                if (c) out << ',';
                out << '"' << events_[i].candidates[c].duration.str() << '"';
            }
            out << "],\"supported_candidates\":[";
            bool first = true;
            for (size_t c = 0; c < support_[i].size(); ++c) {
                if (!support_[i][c]) continue;
                if (!first) out << ',';
                first = false;
                out << '"' << events_[i].candidates[c].duration.str() << '"';
            }
            out << "],\"eliminated_candidates\":[";
            first = true;
            for (size_t c = 0; c < support_[i].size(); ++c) {
                // On a truncated search, an unobserved candidate is unverified,
                // not disproven. Never expose partial search coverage as certainty.
                if (support_[i][c] || truncated_) continue;
                if (!first) out << ',';
                first = false;
                out << '"' << events_[i].candidates[c].duration.str() << '"';
            }
            out << "],\"unverified_candidates\":[";
            first = true;
            if (truncated_) {
                for (size_t c = 0; c < support_[i].size(); ++c) {
                    if (support_[i][c]) continue;
                    if (!first) out << ',';
                    first = false;
                    out << '"' << events_[i].candidates[c].duration.str() << '"';
                }
            }
            out << "],\"solution_counts\":[";
            for (size_t c = 0; c < candidateCounts_[i].size(); ++c) out << (c ? "," : "") << candidateCounts_[i][c];
            size_t supportedCount = 0;
            for (bool b : support_[i]) supportedCount += b;
            out << "],\"solution_counts_log2\":[";
            for (size_t c = 0; c < candidateLog2_[i].size(); ++c)
                out << (c ? "," : "") << (candidateLog2_[i][c] == kNoCount ? std::string("null") : std::to_string(candidateLog2_[i][c]));
            out << "],\"determined\":" << (!truncated_ && supportedCount == 1 ? "true" : "false");
            out << ",\"support_is_complete\":" << (truncated_ ? "false" : "true");
            out << ",\"suggested_visual_checks\":[";
            std::vector<Rational> inputDurations;
            for (const auto& candidate : events_[i].candidates) inputDurations.push_back(candidate.duration);
            const auto checks = inputDurations.size() > 1 ? suggestedChecks(inputDurations) : std::vector<std::string>{};
            for (size_t c = 0; c < checks.size(); ++c) {
                if (c) out << ',';
                out << '"' << checks[c] << '"';
            }
            out << "]}";
        }
        out << "],\"interpretation\":\"A unique result is mathematically unique only relative to the supplied candidates, meter, and single sequential voice/part assumptions.\"}";
        return out.str();
    }

private:
    // One ranked partial solution for the suffix starting at (event, elapsed):
    // the candidate chosen for this event and the rank of the continuation
    // in the next event's list.
    struct RankedEntry {
        double logScore = 0.0;
        uint32_t candidate = 0;
        uint32_t nextRank = 0;
    };

    // Onset positions of one event index, ascending, with their counts.
    // Sorted vectors keep a state at 24 bytes (hash maps cost ~3x more).
    struct Layer {
        std::vector<uint64_t> keys;
        std::vector<uint64_t> counts;
        std::vector<double> log2s;

        long find(uint64_t key) const {
            auto it = std::lower_bound(keys.begin(), keys.end(), key);
            if (it == keys.end() || *it != key) return -1;
            return static_cast<long>(it - keys.begin());
        }
        void push(uint64_t key, uint64_t count, double log2) {
            keys.push_back(key);
            counts.push_back(count);
            log2s.push_back(log2);
        }
        size_t size() const { return keys.size(); }
        void release() {
            std::vector<uint64_t>().swap(keys);
            std::vector<uint64_t>().swap(counts);
            std::vector<double>().swap(log2s);
        }
    };

    static constexpr double kNoCount = -std::numeric_limits<double>::infinity();
    // Entries kept for ranked solutions across all states (16 bytes each).
    static constexpr size_t kRankedEntryBudget = 4000000;

    static double log2Add(double a, double b) {
        if (a == kNoCount) return b;
        if (b == kNoCount) return a;
        const double hi = std::max(a, b), lo = std::min(a, b);
        return hi + std::log2(1.0 + std::exp2(lo - hi));
    }

    int numerator_;
    int denominator_;
    int maxDenominator_;
    SearchConfig config_;
    std::vector<InputEvent> events_;
    uint64_t ticksPerWhole_;
    uint64_t targetTicks_ = 0;
    std::set<std::string> ids_;
    std::vector<uint64_t> suffixMin_;
    std::vector<uint64_t> suffixMax_;
    std::vector<std::vector<bool>> support_;
    std::vector<std::vector<uint64_t>> candidateCounts_;
    std::vector<std::vector<double>> candidateLog2_;   // log2 of the exact count; never saturates
    double validLog2_ = -std::numeric_limits<double>::infinity();
    bool solutionsLimitedByMemory_ = false;
    std::vector<Solution> solutions_;
    uint64_t nodesVisited_ = 0;
    uint64_t validCount_ = 0;
    bool countSaturated_ = false;
    bool truncated_ = false;

    bool remainingFeasible(size_t eventIndex, uint64_t elapsed) const {
        if (elapsed > targetTicks_) return false;
        const uint64_t remaining = targetTicks_ - elapsed;
        return suffixMin_[eventIndex] <= remaining && remaining <= suffixMax_[eventIndex];
    }

    // Exact dynamic program over (event index, elapsed ticks). The number of
    // states is the number of distinct onset positions, not the number of
    // candidate combinations, so dense ambiguous measures are solved exactly.
    // max_nodes bounds the number of states. Counts are kept exactly in
    // 64 bits (saturating) and as log2, which never saturates.
    void runDynamicProgram() {
        const size_t n = events_.size();

        // Forward: reachable onsets and how many prefixes reach them.
        std::vector<Layer> forward(n + 1);
        forward[0].push(0, 1, 0.0);
        nodesVisited_ = 1;
        for (size_t i = 0; i < n; ++i) {
            std::unordered_map<uint64_t, std::pair<uint64_t, double>> next;
            const Layer& cur = forward[i];
            for (size_t j = 0; j < cur.size(); ++j) {
                for (const Candidate& candidate : events_[i].candidates) {
                    const uint64_t at = cur.keys[j] + candidate.ticks;
                    if (!remainingFeasible(i + 1, at)) continue;
                    auto [it, inserted] = next.try_emplace(at, 0, kNoCount);
                    if (inserted && ++nodesVisited_ > config_.maxNodes) {
                        truncated_ = true;
                        return;
                    }
                    it->second.first = saturatingAdd(it->second.first, cur.counts[j], countSaturated_);
                    it->second.second = log2Add(it->second.second, cur.log2s[j]);
                }
            }
            std::vector<uint64_t> keys;
            keys.reserve(next.size());
            for (const auto& entry : next) keys.push_back(entry.first);
            std::sort(keys.begin(), keys.end());
            Layer& out = forward[i + 1];
            out.keys.reserve(keys.size());
            out.counts.reserve(keys.size());
            out.log2s.reserve(keys.size());
            for (uint64_t key : keys) out.push(key, next[key].first, next[key].second);
        }

        // Backward: number of completions from each co-reachable onset. A
        // forward layer is released as soon as it is no longer needed.
        std::vector<Layer> backward(n + 1);
        if (forward[n].find(targetTicks_) >= 0) backward[n].push(targetTicks_, 1, 0.0);
        forward[n].release();
        for (size_t i = n; i-- > 0;) {
            const Layer& fwd = forward[i];
            const Layer& after = backward[i + 1];
            for (size_t j = 0; j < fwd.size(); ++j) {
                uint64_t completions = 0;
                double completionsLog2 = kNoCount;
                for (size_t c = 0; c < events_[i].candidates.size(); ++c) {
                    const long idx = after.find(fwd.keys[j] + events_[i].candidates[c].ticks);
                    if (idx < 0) continue;
                    const size_t k = static_cast<size_t>(idx);
                    // A candidate is supported iff some valid full assignment uses it.
                    support_[i][c] = true;
                    completions = saturatingAdd(completions, after.counts[k], countSaturated_);
                    completionsLog2 = log2Add(completionsLog2, after.log2s[k]);
                    // Number of valid assignments that use this candidate.
                    candidateCounts_[i][c] = saturatingAdd(
                        candidateCounts_[i][c],
                        saturatingMul(fwd.counts[j], after.counts[k], countSaturated_), countSaturated_);
                    candidateLog2_[i][c] = log2Add(candidateLog2_[i][c], fwd.log2s[j] + after.log2s[k]);
                }
                if (completionsLog2 != kNoCount) backward[i].push(fwd.keys[j], completions, completionsLog2);
            }
            forward[i].release();
        }

        const long start = backward[0].find(0);
        if (start < 0) return;
        validCount_ = backward[0].counts[static_cast<size_t>(start)];
        validLog2_ = backward[0].log2s[static_cast<size_t>(start)];

        collectBestSolutions(backward);
    }

    // k-best by visual log-score over the co-reachable states only. Ties are
    // broken by candidate order (confidence-sorted), event by event. Memory
    // is bounded: with too many states, fewer ranked solutions are kept.
    void collectBestSolutions(const std::vector<Layer>& backward) {
        const size_t n = events_.size();
        size_t states = 0;
        for (const auto& layer : backward) states += layer.size();
        const size_t k = std::max<size_t>(1, std::min(config_.limit, kRankedEntryBudget / std::max<size_t>(states, 1)));
        solutionsLimitedByMemory_ = k < config_.limit;

        // best[i]: k slots per state of backward[i]; counts[i]: slots used.
        std::vector<std::vector<RankedEntry>> best(n + 1);
        std::vector<std::vector<uint32_t>> used(n + 1);
        best[n].assign(backward[n].size() * k, RankedEntry{});
        used[n].assign(backward[n].size(), 1);

        // Scores are compared after quantizing to 1e-9 so products that are
        // equal in exact arithmetic (0.9*0.7 vs 0.7*0.9) tie deterministically
        // instead of by floating-point summation order.
        auto quantized = [](double score) { return std::llround(score * 1e9); };
        auto better = [&](const RankedEntry& a, const RankedEntry& b) {
            const long long qa = quantized(a.logScore);
            const long long qb = quantized(b.logScore);
            if (qa != qb) return qa > qb;
            if (a.candidate != b.candidate) return a.candidate < b.candidate;
            return a.nextRank < b.nextRank;
        };

        std::vector<RankedEntry> merged;
        for (size_t i = n; i-- > 0;) {
            const Layer& layer = backward[i];
            const Layer& after = backward[i + 1];
            best[i].assign(layer.size() * k, RankedEntry{});
            used[i].assign(layer.size(), 0);
            for (size_t j = 0; j < layer.size(); ++j) {
                merged.clear();
                for (size_t c = 0; c < events_[i].candidates.size(); ++c) {
                    const Candidate& candidate = events_[i].candidates[c];
                    const long idx = after.find(layer.keys[j] + candidate.ticks);
                    if (idx < 0) continue;
                    const size_t base = static_cast<size_t>(idx) * k;
                    const double own = std::log(candidate.confidence);
                    for (uint32_t r = 0; r < used[i + 1][static_cast<size_t>(idx)]; ++r)
                        merged.push_back(RankedEntry{own + best[i + 1][base + r].logScore,
                                                     static_cast<uint32_t>(c), r});
                }
                const size_t keep = std::min(k, merged.size());
                std::partial_sort(merged.begin(), merged.begin() + static_cast<std::ptrdiff_t>(keep),
                                  merged.end(), better);
                std::copy(merged.begin(), merged.begin() + static_cast<std::ptrdiff_t>(keep), best[i].begin() + static_cast<std::ptrdiff_t>(j * k));
                used[i][j] = static_cast<uint32_t>(keep);
            }
            // best[i + 1] is still needed for reconstruction; keep it.
        }

        const long root = backward[0].find(0);
        for (size_t rank = 0; rank < used[0][static_cast<size_t>(root)]; ++rank) {
            Solution solution;
            solution.logScore = best[0][static_cast<size_t>(root) * k + rank].logScore;
            uint64_t elapsed = 0;
            size_t r = rank;
            for (size_t i = 0; i < n; ++i) {
                const size_t idx = static_cast<size_t>(backward[i].find(elapsed));
                const RankedEntry& step = best[i][idx * k + r];
                solution.choices.push_back(step.candidate);
                elapsed += events_[i].candidates[step.candidate].ticks;
                r = step.nextRank;
            }
            solutions_.push_back(std::move(solution));
        }
    }

    void appendSolution(std::ostringstream& out, const Solution& solution) const {
        out << "{\"log_visual_score\":" << solution.logScore << ",\"events\":[";
        uint64_t onset = 0;
        for (size_t i = 0; i < events_.size(); ++i) {
            if (i) out << ',';
            const Candidate& candidate = events_[i].candidates[solution.choices[i]];
            out << "{\"event_id\":\"" << jsonEscape(events_[i].id)
                << "\",\"duration_whole\":\"" << candidate.duration.str()
                << "\",\"onset_whole\":\"" << Rational(onset, ticksPerWhole_).str()
                << "\",\"confidence\":" << candidate.confidence << '}';
            onset += candidate.ticks;
        }
        out << "]}";
    }
};

void emitError(const std::string& message) {
    std::cout << "{\"type\":\"error\",\"message\":\"" << jsonEscape(message) << "\"}\n" << std::flush;
}

} // namespace

int main(int argc, char** argv) {
    try {
        int defaultNumerator = 4, defaultDenominator = 4, defaultMaxDenominator = 256;
        if (argc == 4) {
            defaultNumerator = std::stoi(argv[1]);
            defaultDenominator = std::stoi(argv[2]);
            defaultMaxDenominator = std::stoi(argv[3]);
        } else if (argc != 1) {
            std::cerr << "Usage: " << argv[0] << " [numerator denominator max_note_denominator]\n";
            return 2;
        }

        // The defaults are used if RESOLVE omits the meter prefix only in a future
        // protocol version; for now every request carries meter and resolution.
        std::cout << "{\"type\":\"ready\",\"engine\":\"omr_constraint_solver\",\"default_meter\":\""
                  << defaultNumerator << '/' << defaultDenominator
                  << "\",\"default_max_note_denominator\":" << defaultMaxDenominator << "}\n" << std::flush;

        std::string line;
        while (std::getline(std::cin, line)) {
            if (line.empty()) continue;
            std::istringstream input(line);
            std::string command;
            input >> command;
            for (char& c : command) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
            if (command == "EXIT" || command == "QUIT") break;
            if (command != "RESOLVE") {
                emitError("unknown command; use RESOLVE or EXIT");
                continue;
            }

            try {
                std::string numText, denText, maxText;
                input >> numText >> denText >> maxText;
                if (numText.empty() || denText.empty() || maxText.empty())
                    throw std::invalid_argument("RESOLVE requires numerator denominator max_note_denominator");

                const int numerator = static_cast<int>(parseUInt(numText, "meter numerator"));
                const int denominator = static_cast<int>(parseUInt(denText, "meter denominator"));
                const int maxDenominator = static_cast<int>(parseUInt(maxText, "max note denominator"));

                SearchConfig config;
                std::vector<InputEvent> events;
                std::string token;
                while (input >> token) {
                    if (token.rfind("limit=", 0) == 0) {
                        config.limit = static_cast<size_t>(parseUInt(token.substr(6), "limit"));
                    } else if (token.rfind("max_nodes=", 0) == 0) {
                        config.maxNodes = parseUInt(token.substr(10), "max_nodes");
                    } else {
                        if (maxDenominator < 2 || maxDenominator > 256 || !isPowerOfTwo(static_cast<uint64_t>(maxDenominator)))
                            throw std::invalid_argument("max denominator must be a power of two from 2 to 256");
                        events.push_back(parseEvent(token));
                    }
                }

                if (config.limit == 0 || config.limit > 100) throw std::invalid_argument("limit must be 1..100");
                if (config.maxNodes == 0 || config.maxNodes > 10000000)
                    throw std::invalid_argument("max_nodes must be 1..10000000");

                Solver solver(numerator, denominator, maxDenominator, config, std::move(events));
                std::cout << solver.solve() << '\n' << std::flush;
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
