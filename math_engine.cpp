#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

/*
  MATH ENGINE — C++17

  Does not store all rhythm patterns. It builds a compact dynamic-programming
  index in memory and supports:
    COUNT NOTES|RESTS
    GET NOTES|RESTS <index>
    PAGE NOTES|RESTS <start> <limit>

  JSON Lines (JSONL) protocol over stdin/stdout. The process stays alive so a
  bot can communicate with it without restarting the engine for each query.

  Build:
    clang++ -std=c++17 -O3 -DNDEBUG math_engine.cpp -o math_engine

  Run:
    ./math_engine 4 4 256
*/

namespace {

class BigUInt {
public:
    static constexpr uint32_t BASE = 1'000'000'000u;

    BigUInt(uint64_t value = 0) {
        do {
            digits_.push_back(static_cast<uint32_t>(value % BASE));
            value /= BASE;
        } while (value != 0);
    }

    static BigUInt parse(const std::string& text) {
        if (text.empty()) throw std::invalid_argument("index gol");
        BigUInt result(0);
        result.digits_.clear();

        for (char ch : text) {
            if (!std::isdigit(static_cast<unsigned char>(ch))) {
                throw std::invalid_argument("indexul trebuie sa fie un intreg >= 0");
            }

            uint64_t carry = static_cast<unsigned>(ch - '0');
            for (size_t i = 0; i < result.digits_.size(); ++i) {
                const uint64_t value =
                    static_cast<uint64_t>(result.digits_[i]) * 10u + carry;
                result.digits_[i] = static_cast<uint32_t>(value % BASE);
                carry = value / BASE;
            }
            if (carry) result.digits_.push_back(static_cast<uint32_t>(carry));
            if (result.digits_.empty()) result.digits_.push_back(0);
        }

        result.normalize();
        return result;
    }

    bool isZero() const {
        return digits_.size() == 1 && digits_[0] == 0;
    }

    std::string str() const {
        std::ostringstream out;
        out << digits_.back();
        for (size_t i = digits_.size() - 1; i > 0; --i) {
            out.width(9);
            out.fill('0');
            out << digits_[i - 1];
        }
        return out.str();
    }

    void add(const BigUInt& other) {
        const size_t n = std::max(digits_.size(), other.digits_.size());
        digits_.resize(n, 0);
        uint64_t carry = 0;

        for (size_t i = 0; i < n; ++i) {
            uint64_t sum = static_cast<uint64_t>(digits_[i]) + carry;
            if (i < other.digits_.size()) sum += other.digits_[i];
            digits_[i] = static_cast<uint32_t>(sum % BASE);
            carry = sum / BASE;
        }

        if (carry) digits_.push_back(static_cast<uint32_t>(carry));
    }

    void subtract(const BigUInt& other) {
        if (*this < other) throw std::logic_error("scadere BigUInt negativa");

        int64_t borrow = 0;
        for (size_t i = 0; i < digits_.size(); ++i) {
            int64_t value = static_cast<int64_t>(digits_[i]) - borrow;
            if (i < other.digits_.size()) value -= other.digits_[i];

            if (value < 0) {
                value += BASE;
                borrow = 1;
            } else {
                borrow = 0;
            }
            digits_[i] = static_cast<uint32_t>(value);
        }
        normalize();
    }

    void increment() { add(BigUInt(1)); }

    friend bool operator<(const BigUInt& a, const BigUInt& b) {
        if (a.digits_.size() != b.digits_.size()) {
            return a.digits_.size() < b.digits_.size();
        }
        for (size_t i = a.digits_.size(); i > 0; --i) {
            if (a.digits_[i - 1] != b.digits_[i - 1]) {
                return a.digits_[i - 1] < b.digits_[i - 1];
            }
        }
        return false;
    }

private:
    std::vector<uint32_t> digits_; // little-endian, base 10^9

    void normalize() {
        while (digits_.size() > 1 && digits_.back() == 0) digits_.pop_back();
        if (digits_.empty()) digits_.push_back(0);
    }
};

enum class EventKind {
    Note,
    DottedNote,
    Rest,
    DottedRest,
    Triplet,
    MeasureRest
};

struct Event {
    uint64_t ticks = 0;
    EventKind kind = EventKind::Note;
    int denominator = 1;
    int tripletMask = 7; // bit set = note; bit clear = rest
    bool containsRest = false;
    std::string label;
};

std::string kindName(EventKind kind) {
    switch (kind) {
        case EventKind::Note: return "note";
        case EventKind::DottedNote: return "dotted_note";
        case EventKind::Rest: return "rest";
        case EventKind::DottedRest: return "dotted_rest";
        case EventKind::Triplet: return "triplet";
        case EventKind::MeasureRest: return "measure_rest";
    }
    return "unknown";
}

std::string noteName(int denominator) {
    switch (denominator) {
        case 1: return "nota intreaga";
        case 2: return "doime";
        case 4: return "patrime";
        case 8: return "optime";
        case 16: return "saisprezecime";
        case 32: return "treizecidoime";
        case 64: return "nota 1/64";
        case 128: return "nota 1/128";
        case 256: return "nota 1/256";
        default: return "nota 1/" + std::to_string(denominator);
    }
}

std::string jsonEscape(const std::string& input) {
    std::string out;
    out.reserve(input.size() + 8);

    for (unsigned char ch : input) {
        switch (ch) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (ch < 0x20) {
                    const char* hex = "0123456789abcdef";
                    out += "\\u00";
                    out += hex[(ch >> 4) & 0xF];
                    out += hex[ch & 0xF];
                } else {
                    out += static_cast<char>(ch);
                }
        }
    }
    return out;
}

bool isPowerOfTwo(int value) {
    return value > 0 && (value & (value - 1)) == 0;
}

std::vector<int> denominatorsUpTo(int maximum) {
    std::vector<int> result;
    for (int d = 1; d <= maximum; d *= 2) {
        result.push_back(d);
        if (d > maximum / 2) break;
    }
    return result;
}

std::string makeTripletLabel(int denominator, int mask) {
    const std::string name = noteName(denominator);
    std::ostringstream out;
    out << "TRIOLET [";

    for (int slot = 0; slot < 3; ++slot) {
        if (slot) out << ", ";
        out << ((mask & (1 << slot)) ? "nota de " : "pauza de ") << name;
    }

    out << "] (3 in timpul a 2 x " << name << ")";
    return out.str();
}

class RhythmEngine {
public:
    RhythmEngine(int numerator, int denominator, int maxNoteDenominator)
        : numerator_(numerator),
          denominator_(denominator),
          maxDenominator_(maxNoteDenominator),
          ticksPerWhole_(static_cast<uint64_t>(6) * maxNoteDenominator) {
        if (numerator_ <= 0 || numerator_ > 10000) {
            throw std::invalid_argument("numaratorul trebuie sa fie intre 1 si 10000");
        }
        if (!isPowerOfTwo(denominator_) || denominator_ > maxDenominator_) {
            throw std::invalid_argument(
                "numitorul masurii trebuie sa fie o putere a lui 2 si <= subdiviziunea maxima");
        }
        if (!isPowerOfTwo(maxDenominator_) ||
            maxDenominator_ < 2 || maxDenominator_ > 256) {
            throw std::invalid_argument(
                "subdiviziunea maxima trebuie sa fie o putere a lui 2 intre 2 si 256");
        }

        const uint64_t numeratorTicks =
            static_cast<uint64_t>(numerator_) * ticksPerWhole_;

        if (numeratorTicks % static_cast<uint64_t>(denominator_) != 0) {
            throw std::invalid_argument(
                "masura nu poate fi reprezentata exact la rezolutia selectata");
        }

        targetTicks_ = numeratorTicks / static_cast<uint64_t>(denominator_);
        if (targetTicks_ > 2'000'000) {
            throw std::invalid_argument(
                "masura este prea mare pentru indexul DP (limita 2.000.000 ticks)");
        }

        noteEvents_ = buildEvents(false);
        restEvents_ = buildEvents(true);
        buildNoteIndex();
        buildRestIndex();
    }

    const BigUInt& noteCount() const {
        return noteWays_[static_cast<size_t>(targetTicks_)];
    }

    const BigUInt& restCount() const {
        // State 0 means no rest has appeared yet; count continuations
        // that introduce at least one rest before the measure is complete.
        return restWays_[static_cast<size_t>(targetTicks_)][0];
    }

    std::string readyJson() const {
        std::ostringstream out;
        out << "{\"type\":\"ready\",\"meter\":\"" << numerator_ << '/' << denominator_
            << "\",\"max_note_denominator\":" << maxDenominator_
            << ",\"ticks_per_whole\":" << ticksPerWhole_
            << ",\"target_ticks\":" << targetTicks_
            << ",\"count_notes\":\"" << noteCount().str()
            << "\",\"count_with_at_least_one_rest\":\"" << restCount().str()
            << "\",\"protocol\":\"COUNT|GET|PAGE; category NOTES or RESTS; JSON Lines\"}";
        return out.str();
    }

    std::optional<std::string> getPattern(bool withRests, const BigUInt& index) const {
        const BigUInt& total = withRests ? restCount() : noteCount();
        if (!(index < total)) return std::nullopt;

        const std::vector<Event>& events = withRests ? restEvents_ : noteEvents_;
        BigUInt remainingIndex = index;
        uint64_t remainingTicks = targetTicks_;
        bool hasRest = false;
        std::vector<const Event*> chosen;

        while (remainingTicks > 0) {
            bool found = false;

            for (const Event& event : events) {
                if (event.ticks > remainingTicks) continue;

                const uint64_t nextTicks = remainingTicks - event.ticks;
                const bool nextHasRest = hasRest || event.containsRest;

                const BigUInt& block = withRests
                    ? restWays_[static_cast<size_t>(nextTicks)][nextHasRest ? 1 : 0]
                    : noteWays_[static_cast<size_t>(nextTicks)];

                if (remainingIndex < block) {
                    chosen.push_back(&event);
                    remainingTicks = nextTicks;
                    hasRest = nextHasRest;
                    found = true;
                    break;
                }

                remainingIndex.subtract(block);
            }

            if (!found) throw std::logic_error("indexul nu a putut fi decodat");
        }

        return patternJson(withRests, index, chosen);
    }

    std::string countJson(bool withRests) const {
        const BigUInt& total = withRests ? restCount() : noteCount();
        std::ostringstream out;
        out << "{\"type\":\"count\",\"category\":\"" << (withRests ? "RESTS" : "NOTES")
            << "\",\"meter\":\"" << numerator_ << '/' << denominator_
            << "\",\"count\":\"" << total.str() << "\"}";
        return out.str();
    }

private:
    int numerator_;
    int denominator_;
    int maxDenominator_;
    uint64_t ticksPerWhole_;
    uint64_t targetTicks_ = 0;
    std::vector<Event> noteEvents_;
    std::vector<Event> restEvents_;
    std::vector<BigUInt> noteWays_;

    // [remaining ticks][0 = no rest seen yet, 1 = a rest has already appeared]
    std::vector<std::array<BigUInt, 2>> restWays_;

    std::vector<Event> buildEvents(bool includeRests) const {
        std::vector<Event> events;

        for (int d : denominatorsUpTo(maxDenominator_)) {
            const uint64_t normal = ticksPerWhole_ / static_cast<uint64_t>(d);
            const uint64_t dotted =
                (3 * ticksPerWhole_) / (2 * static_cast<uint64_t>(d));
            const uint64_t tripletGroup =
                (2 * ticksPerWhole_) / static_cast<uint64_t>(d);
            const std::string name = noteName(d);

            events.push_back(Event{
                normal, EventKind::Note, d, 7, false,
                name + " (1/" + std::to_string(d) + ")"
            });
            events.push_back(Event{
                dotted, EventKind::DottedNote, d, 7, false,
                name + " cu punct (3/" + std::to_string(2 * d) + ")"
            });

            if (includeRests) {
                events.push_back(Event{
                    normal, EventKind::Rest, d, 0, true,
                    "pauza de " + name + " (1/" + std::to_string(d) + ")"
                });
                events.push_back(Event{
                    dotted, EventKind::DottedRest, d, 0, true,
                    "pauza de " + name + " cu punct"
                });
            }

            const int firstMask = includeRests ? 0 : 7;
            const int lastMask = 7;

            for (int mask = firstMask; mask <= lastMask; ++mask) {
                const bool hasRest = includeRests && mask != 7;
                events.push_back(Event{
                    tripletGroup, EventKind::Triplet, d, mask, hasRest,
                    makeTripletLabel(d, mask)
                });
            }
        }

        // A full-measure rest is represented as a special event when a regular
        // whole-note rest cannot exactly fill the selected measure.
        if (includeRests && targetTicks_ != ticksPerWhole_) {
            events.push_back(Event{
                targetTicks_, EventKind::MeasureRest, 0, 0, true,
                "PAUZA DE MASURA INTREAGA"
            });
        }

        std::stable_sort(
            events.begin(), events.end(),
            [](const Event& a, const Event& b) { return a.ticks > b.ticks; }
        );

        return events;
    }

    void buildNoteIndex() {
        noteWays_.assign(static_cast<size_t>(targetTicks_) + 1, BigUInt(0));
        noteWays_[0] = BigUInt(1);

        for (uint64_t remaining = 1; remaining <= targetTicks_; ++remaining) {
            BigUInt total(0);

            for (const Event& event : noteEvents_) {
                if (event.ticks > remaining) continue;
                total.add(noteWays_[static_cast<size_t>(remaining - event.ticks)]);
            }

            noteWays_[static_cast<size_t>(remaining)] = std::move(total);
        }
    }

    void buildRestIndex() {
        restWays_.resize(static_cast<size_t>(targetTicks_) + 1);
        restWays_[0][0] = BigUInt(0);
        restWays_[0][1] = BigUInt(1);

        for (uint64_t remaining = 1; remaining <= targetTicks_; ++remaining) {
            BigUInt noRestYet(0);
            BigUInt restAlreadySeen(0);

            for (const Event& event : restEvents_) {
                if (event.ticks > remaining) continue;

                const uint64_t next =
                    remaining - event.ticks;

                // If a rest has already appeared, any valid suffix is allowed.
                // Otherwise, the suffix must supply a rest unless this event does.
                restAlreadySeen.add(
                    restWays_[static_cast<size_t>(next)][1]
                );
                noRestYet.add(
                    restWays_[static_cast<size_t>(next)][event.containsRest ? 1 : 0]
                );
            }

            restWays_[static_cast<size_t>(remaining)][0] = std::move(noRestYet);
            restWays_[static_cast<size_t>(remaining)][1] = std::move(restAlreadySeen);
        }
    }

    std::string patternJson(
        bool withRests,
        const BigUInt& index,
        const std::vector<const Event*>& chosen
    ) const {
        std::ostringstream out;
        out << "{\"type\":\"pattern\",\"category\":\"" << (withRests ? "RESTS" : "NOTES")
            << "\",\"meter\":\"" << numerator_ << '/' << denominator_
            << "\",\"index\":\"" << index.str() << "\",\"has_rest\":"
            << (withRests ? "true" : "false") << ",\"events\":[";

        for (size_t i = 0; i < chosen.size(); ++i) {
            if (i) out << ',';

            const Event& event = *chosen[i];
            out << "{\"kind\":\"" << kindName(event.kind)
                << "\",\"ticks\":" << event.ticks;

            if (event.denominator > 0) {
                out << ",\"denominator\":" << event.denominator;
            }

            if (event.kind == EventKind::Triplet) {
                out << ",\"slots\":[";
                for (int slot = 0; slot < 3; ++slot) {
                    if (slot) out << ',';
                    out << '"'
                        << ((event.tripletMask & (1 << slot)) ? "note" : "rest")
                        << '"';
                }
                out << ']';
            }

            out << ",\"label\":\"" << jsonEscape(event.label) << "\"}";
        }

        out << "]}";
        return out.str();
    }
};

std::string upper(std::string value) {
    for (char& ch : value) {
        ch = static_cast<char>(
            std::toupper(static_cast<unsigned char>(ch))
        );
    }
    return value;
}

void emitError(const std::string& message) {
    std::cout << "{\"type\":\"error\",\"message\":\""
              << jsonEscape(message) << "\"}\n";
}

void printHelp() {
    std::cout
        << "{\"type\":\"help\",\"commands\":["
        << "\"COUNT NOTES\",\"COUNT RESTS\",\"GET NOTES <index>\","
        << "\"GET RESTS <index>\",\"PAGE NOTES <start_index> <limit>\","
        << "\"PAGE RESTS <start_index> <limit>\",\"EXIT\"]}\n";
}

} // namespace

int main(int argc, char** argv) {
    try {
        int numerator = 4;
        int denominator = 4;
        int maxDenominator = 256;

        if (argc == 4) {
            numerator = std::stoi(argv[1]);
            denominator = std::stoi(argv[2]);
            maxDenominator = std::stoi(argv[3]);
        } else if (argc != 1) {
            std::cerr
                << "Utilizare: " << argv[0]
                << " [numarator numitor subdiviziune_maxima]\n";
            return 2;
        }

        RhythmEngine engine(numerator, denominator, maxDenominator);
        std::cout << engine.readyJson() << '\n' << std::flush;

        std::string line;
        while (std::getline(std::cin, line)) {
            std::istringstream input(line);
            std::string command;
            input >> command;
            command = upper(command);

            if (command.empty()) continue;
            if (command == "EXIT" || command == "QUIT") break;

            if (command == "HELP") {
                printHelp();
                continue;
            }

            try {
                if (command == "COUNT") {
                    std::string category;
                    input >> category;
                    category = upper(category);

                    if (category == "NOTES") {
                        std::cout << engine.countJson(false) << '\n';
                    } else if (category == "RESTS") {
                        std::cout << engine.countJson(true) << '\n';
                    } else {
                        emitError("Categoria trebuie sa fie NOTES sau RESTS");
                    }
                } else if (command == "GET") {
                    std::string category;
                    std::string indexText;
                    input >> category >> indexText;
                    category = upper(category);

                    if (category != "NOTES" && category != "RESTS") {
                        emitError("Categoria trebuie sa fie NOTES sau RESTS");
                        continue;
                    }

                    BigUInt index = BigUInt::parse(indexText);
                    auto result = engine.getPattern(category == "RESTS", index);

                    if (result) {
                        std::cout << *result << '\n';
                    } else {
                        emitError(
                            "Index in afara intervalului; foloseste COUNT pentru numarul total"
                        );
                    }
                } else if (command == "PAGE") {
                    std::string category;
                    std::string startText;
                    size_t limit = 0;
                    input >> category >> startText >> limit;
                    category = upper(category);

                    if (category != "NOTES" && category != "RESTS") {
                        emitError("Categoria trebuie sa fie NOTES sau RESTS");
                        continue;
                    }
                    if (!input || limit == 0 || limit > 1000) {
                        emitError("PAGE necesita limit intre 1 si 1000");
                        continue;
                    }

                    BigUInt index = BigUInt::parse(startText);
                    const bool withRests = category == "RESTS";
                    size_t produced = 0;

                    for (; produced < limit; ++produced) {
                        auto result = engine.getPattern(withRests, index);
                        if (!result) break;

                        std::cout << *result << '\n';
                        index.increment();
                    }

                    std::cout
                        << "{\"type\":\"page_end\",\"category\":\"" << category
                        << "\",\"returned\":" << produced << "}\n";
                } else {
                    emitError("Comanda necunoscuta; trimite HELP pentru lista comenzilor");
                }
            } catch (const std::exception& error) {
                emitError(error.what());
            }

            std::cout << std::flush;
        }
    } catch (const std::exception& error) {
        std::cerr << "Eroare la pornire: " << error.what() << '\n';
        return 1;
    }

    return 0;
}
