#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
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

struct Chosen {
    size_t candidateIndex = 0;
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

InputEvent parseEvent(const std::string& token, uint64_t ticksPerWhole) {
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

        if (ticksPerWhole % duration.d != 0)
            throw std::invalid_argument("duration denominator is not representable at selected resolution: " + duration.str());

        if (duration.n > UINT64_MAX / (ticksPerWhole / duration.d))
            throw std::invalid_argument("duration is too large");

        const uint64_t ticks = duration.n * (ticksPerWhole / duration.d);
        event.candidates.push_back(Candidate{duration, ticks, confidence});
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

class Solver {
public:
    Solver(int numerator, int denominator, int maxDenominator,
           SearchConfig config, std::vector<InputEvent> events)
        : numerator_(numerator), denominator_(denominator),
          maxDenominator_(maxDenominator), config_(config), events_(std::move(events)),
          ticksPerWhole_(static_cast<uint64_t>(6) * maxDenominator_) {
        if (numerator_ <= 0 || numerator_ > 10000) throw std::invalid_argument("meter numerator must be 1..10000");
        if (!isPowerOfTwo(static_cast<uint64_t>(denominator_)) ||
            static_cast<uint64_t>(denominator_) > static_cast<uint64_t>(maxDenominator_))
            throw std::invalid_argument("meter denominator must be a power of two <= max denominator");
        if (!isPowerOfTwo(static_cast<uint64_t>(maxDenominator_)) ||
            maxDenominator_ < 2 || maxDenominator_ > 256)
            throw std::invalid_argument("max denominator must be a power of two from 2 to 256");
        if (events_.empty()) throw std::invalid_argument("at least one event is required");
        if (events_.size() > 2048) throw std::invalid_argument("at most 2048 ordered events are supported");

        const uint64_t scaled = static_cast<uint64_t>(numerator_) * ticksPerWhole_;
        if (scaled % static_cast<uint64_t>(denominator_) != 0)
            throw std::invalid_argument("meter cannot be represented at this resolution");
        targetTicks_ = scaled / static_cast<uint64_t>(denominator_);
        if (targetTicks_ > 2000000) throw std::invalid_argument("measure exceeds solver grid limit");

        for (const auto& event : events_) {
            if (!ids_.insert(event.id).second) throw std::invalid_argument("duplicate event id: " + event.id);
            for (const auto& candidate : event.candidates) {
                if (candidate.ticks > targetTicks_ * 16)
                    throw std::invalid_argument("candidate duration is implausibly larger than measure");
            }
        }

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

        choices_.resize(events_.size());
        support_.resize(events_.size());
    }

    std::string readyJson() const {
        std::ostringstream out;
        out << "{\"type\":\"ready\",\"engine\":\"omr_constraint_solver\",\"meter\":\""
            << numerator_ << '/' << denominator_ << "\",\"max_note_denominator\":"
            << maxDenominator_ << ",\"ticks_per_whole\":" << ticksPerWhole_
            << ",\"protocol\":\"RESOLVE meter_num meter_den max_denominator options...\"}";
        return out.str();
    }

    std::string solve() {
        dfs(0, 0, 0.0);

        std::stable_sort(solutions_.begin(), solutions_.end(),
            [](const Solution& a, const Solution& b) { return a.logScore > b.logScore; });

        std::ostringstream out;
        const char* status = truncated_ ? "incomplete" :
            (validCount_ == 0 ? "inconsistent" : (validCount_ == 1 ? "resolved_unique" : "ambiguous"));

        out << "{\"type\":\"resolution\",\"status\":\"" << status
            << "\",\"meter\":\"" << numerator_ << '/' << denominator_
            << "\",\"measure_duration_whole\":\"" << Rational(numerator_, denominator_).str()
            << "\",\"nodes_visited\":" << nodesVisited_
            << ",\"valid_assignments\":" << validCount_
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
            std::vector<Rational> supported;
            for (size_t c = 0; c < support_[i].size(); ++c) {
                if (!support_[i][c]) continue;
                if (!supported.empty()) out << ',';
                out << '"' << events_[i].candidates[c].duration.str() << '"';
                supported.push_back(events_[i].candidates[c].duration);
            }
            out << "],\"eliminated_candidates\":[";
            bool first = true;
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
            out << "],\"support_is_complete\":" << (truncated_ ? "false" : "true");
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
    std::vector<size_t> choices_;
    std::vector<std::vector<bool>> support_;
    std::vector<Solution> solutions_;
    uint64_t nodesVisited_ = 0;
    uint64_t validCount_ = 0;
    bool truncated_ = false;

    void dfs(size_t eventIndex, uint64_t elapsedTicks, double logScore) {
        if (nodesVisited_ >= config_.maxNodes) {
            truncated_ = true;
            return;
        }
        ++nodesVisited_;

        if (elapsedTicks > targetTicks_) return;
        const uint64_t remaining = targetTicks_ - elapsedTicks;
        if (eventIndex == events_.size()) {
            if (remaining == 0) recordSolution(logScore);
            return;
        }

        if (suffixMin_[eventIndex] > remaining || suffixMax_[eventIndex] < remaining) return;

        const InputEvent& event = events_[eventIndex];
        for (size_t c = 0; c < event.candidates.size(); ++c) {
            const Candidate& candidate = event.candidates[c];
            if (candidate.ticks > remaining) continue;
            if (suffixMin_[eventIndex + 1] > remaining - candidate.ticks ||
                suffixMax_[eventIndex + 1] < remaining - candidate.ticks) continue;

            choices_[eventIndex] = c;
            dfs(eventIndex + 1, elapsedTicks + candidate.ticks,
                logScore + std::log(candidate.confidence));
            if (truncated_ && nodesVisited_ >= config_.maxNodes) return;
        }
    }

    void recordSolution(double logScore) {
        ++validCount_;
        for (size_t i = 0; i < choices_.size(); ++i) support_[i][choices_[i]] = true;

        Solution candidate{choices_, logScore};
        if (solutions_.size() < config_.limit) {
            solutions_.push_back(std::move(candidate));
            return;
        }

        auto worst = std::min_element(solutions_.begin(), solutions_.end(),
            [](const Solution& a, const Solution& b) { return a.logScore < b.logScore; });
        if (worst != solutions_.end() && candidate.logScore > worst->logScore)
            *worst = std::move(candidate);
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
                        events.push_back(parseEvent(token, static_cast<uint64_t>(6) * maxDenominator));
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
