#pragma once

/*
 * Rhythm Space — the complete rhythmic notation space of one measure.
 *
 * Grammar (every item is notated; durations are exact rationals):
 *
 *   measure  := item+                          filling the measure exactly
 *   item     := note | rest | measure_rest | tuplet
 *   note     := base value (longa .. 1/max_denominator) with 0..max_dots dots
 *   rest     := same values as notes
 *   tuplet   := a:n bracket whose content is ANY item sequence (including
 *               further tuplets) of nominal length a*u, sounding as n*u,
 *               where the unit u is a plain or dotted note value
 *
 * So a septuplet may contain a triplet, a triplet may contain a quarter and an
 * eighth, a quintuplet may contain rests and dotted values, and so on, up to a
 * caller-selected nesting depth. Nothing is sampled or approximated: counts
 * are exact big integers, every pattern has an index, and any index can be
 * decoded directly into its pattern.
 *
 * Counting works on the NOMINAL grid (notated values). Inside a tuplet the
 * content is an ordinary item sequence of nominal length a*u; only the
 * sounding durations are scaled by n/a. This makes the count a recursive
 * dynamic program per nesting level instead of an enumeration.
 *
 * Notation used for input and output:
 *   n4      quarter note        r8.     dotted eighth rest
 *   n2..    double-dotted half  nB rB   breve, nL rL longa
 *   M       full-measure rest (meters whose bar is not one whole note)
 *   [3:2 n8 n8 n8]                      eighth-note triplet
 *   [7:4 n16 n16 [3:2 n16 n16 n16] n16 n16 n16]   triplet inside a septuplet
 * The tuplet unit is not written: it is the content length divided by a.
 *
 * Templates (for searching with partial knowledge) mix concrete items with:
 *   ?        exactly one item       *       any items (possibly none)
 *   +        one or more items      *=3/16  any items lasting exactly 3/16
 */

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <map>
#include <memory>
#include <numeric>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace rhythm_space {

__extension__ typedef unsigned __int128 u128;

// ---------------------------------------------------------------------------
// Arbitrary-precision unsigned integer (base 2^32).
// ---------------------------------------------------------------------------
class BigUInt {
public:
    BigUInt() = default;
    explicit BigUInt(uint64_t value) {
        while (value) {
            limbs_.push_back(static_cast<uint32_t>(value));
            value >>= 32;
        }
    }

    bool isZero() const { return limbs_.empty(); }

    static int compare(const BigUInt& a, const BigUInt& b) {
        if (a.limbs_.size() != b.limbs_.size()) return a.limbs_.size() < b.limbs_.size() ? -1 : 1;
        for (size_t i = a.limbs_.size(); i-- > 0;) {
            if (a.limbs_[i] != b.limbs_[i]) return a.limbs_[i] < b.limbs_[i] ? -1 : 1;
        }
        return 0;
    }
    friend bool operator<(const BigUInt& a, const BigUInt& b) { return compare(a, b) < 0; }
    friend bool operator<=(const BigUInt& a, const BigUInt& b) { return compare(a, b) <= 0; }
    friend bool operator==(const BigUInt& a, const BigUInt& b) { return compare(a, b) == 0; }
    friend bool operator!=(const BigUInt& a, const BigUInt& b) { return compare(a, b) != 0; }

    BigUInt& operator+=(const BigUInt& other) {
        if (limbs_.size() < other.limbs_.size()) limbs_.resize(other.limbs_.size(), 0);
        uint64_t carry = 0;
        for (size_t i = 0; i < limbs_.size(); ++i) {
            uint64_t sum = static_cast<uint64_t>(limbs_[i]) + carry;
            if (i < other.limbs_.size()) sum += other.limbs_[i];
            limbs_[i] = static_cast<uint32_t>(sum);
            carry = sum >> 32;
            if (!carry && i >= other.limbs_.size()) break;
        }
        if (carry) limbs_.push_back(static_cast<uint32_t>(carry));
        return *this;
    }

    // Requires *this >= other.
    BigUInt& operator-=(const BigUInt& other) {
        if (*this < other) throw std::logic_error("BigUInt subtraction underflow");
        int64_t borrow = 0;
        for (size_t i = 0; i < limbs_.size(); ++i) {
            int64_t value = static_cast<int64_t>(limbs_[i]) - borrow;
            if (i < other.limbs_.size()) value -= other.limbs_[i];
            borrow = value < 0 ? 1 : 0;
            if (value < 0) value += (int64_t(1) << 32);
            limbs_[i] = static_cast<uint32_t>(value);
            if (!borrow && i + 1 >= other.limbs_.size()) break;
        }
        trim();
        return *this;
    }

    friend BigUInt operator+(BigUInt a, const BigUInt& b) { a += b; return a; }
    friend BigUInt operator-(BigUInt a, const BigUInt& b) { a -= b; return a; }

    friend BigUInt operator*(const BigUInt& a, const BigUInt& b) {
        BigUInt out;
        if (a.isZero() || b.isZero()) return out;
        out.limbs_.assign(a.limbs_.size() + b.limbs_.size(), 0);
        for (size_t i = 0; i < a.limbs_.size(); ++i) {
            uint64_t carry = 0;
            const uint64_t ai = a.limbs_[i];
            for (size_t j = 0; j < b.limbs_.size(); ++j) {
                const uint64_t cur = out.limbs_[i + j] + ai * b.limbs_[j] + carry;
                out.limbs_[i + j] = static_cast<uint32_t>(cur);
                carry = cur >> 32;
            }
            size_t k = i + b.limbs_.size();
            while (carry) {
                const uint64_t cur = out.limbs_[k] + carry;
                out.limbs_[k] = static_cast<uint32_t>(cur);
                carry = cur >> 32;
                ++k;
            }
        }
        out.trim();
        return out;
    }

    // this += value * small
    void addMultiple(const BigUInt& value, uint32_t small) {
        if (small == 0 || value.isZero()) return;
        if (limbs_.size() < value.limbs_.size()) limbs_.resize(value.limbs_.size(), 0);
        uint64_t carry = 0;
        size_t i = 0;
        for (; i < value.limbs_.size(); ++i) {
            const uint64_t cur = static_cast<uint64_t>(limbs_[i]) +
                                 static_cast<uint64_t>(value.limbs_[i]) * small + carry;
            limbs_[i] = static_cast<uint32_t>(cur);
            carry = cur >> 32;
        }
        for (; carry; ++i) {
            if (i == limbs_.size()) limbs_.push_back(0);
            const uint64_t cur = static_cast<uint64_t>(limbs_[i]) + carry;
            limbs_[i] = static_cast<uint32_t>(cur);
            carry = cur >> 32;
        }
    }

    size_t bitLength() const {
        if (limbs_.empty()) return 0;
        size_t bits = (limbs_.size() - 1) * 32;
        uint32_t top = limbs_.back();
        while (top) { ++bits; top >>= 1; }
        return bits;
    }
    bool bit(size_t i) const {
        const size_t limb = i / 32;
        return limb < limbs_.size() && ((limbs_[limb] >> (i % 32)) & 1u);
    }

    // Binary long division: a = q*b + r.
    static void divmod(const BigUInt& a, const BigUInt& b, BigUInt& q, BigUInt& r) {
        if (b.isZero()) throw std::logic_error("BigUInt division by zero");
        q = BigUInt();
        r = BigUInt();
        if (a < b) { r = a; return; }
        q.limbs_.assign(a.limbs_.size(), 0);
        for (size_t i = a.bitLength(); i-- > 0;) {
            r.shiftLeftOne();
            if (a.bit(i)) {
                if (r.limbs_.empty()) r.limbs_.push_back(0);
                r.limbs_[0] |= 1u;
            }
            if (b <= r) {
                r -= b;
                q.limbs_[i / 32] |= (1u << (i % 32));
            }
        }
        q.trim();
    }

    std::string str() const {
        if (isZero()) return "0";
        BigUInt copy = *this;
        std::vector<uint32_t> chunks;
        while (!copy.isZero()) chunks.push_back(copy.divSmall(1000000000u));
        std::string out = std::to_string(chunks.back());
        for (size_t i = chunks.size() - 1; i-- > 0;) {
            std::string part = std::to_string(chunks[i]);
            out += std::string(9 - part.size(), '0') + part;
        }
        return out;
    }

    static BigUInt parse(const std::string& text) {
        if (text.empty()) throw std::invalid_argument("expected a non-negative integer");
        BigUInt out;
        for (char ch : text) {
            if (!std::isdigit(static_cast<unsigned char>(ch)))
                throw std::invalid_argument("expected a non-negative integer: " + text);
            out.mulSmallAdd(10, static_cast<uint32_t>(ch - '0'));
        }
        return out;
    }

    // Uniform integer in [0, bound).
    static BigUInt randomBelow(const BigUInt& bound, std::mt19937_64& rng) {
        if (bound.isZero()) throw std::invalid_argument("empty range");
        const size_t bits = bound.bitLength();
        for (;;) {
            BigUInt candidate;
            candidate.limbs_.assign((bits + 31) / 32, 0);
            for (auto& limb : candidate.limbs_) limb = static_cast<uint32_t>(rng());
            const size_t extra = candidate.limbs_.size() * 32 - bits;
            if (extra) candidate.limbs_.back() &= (0xFFFFFFFFu >> extra);
            candidate.trim();
            if (candidate < bound) return candidate;
        }
    }

private:
    std::vector<uint32_t> limbs_; // little-endian

    void trim() { while (!limbs_.empty() && limbs_.back() == 0) limbs_.pop_back(); }

    void shiftLeftOne() {
        uint32_t carry = 0;
        for (auto& limb : limbs_) {
            const uint32_t next = limb >> 31;
            limb = (limb << 1) | carry;
            carry = next;
        }
        if (carry) limbs_.push_back(carry);
    }

    uint32_t divSmall(uint32_t divisor) {
        uint64_t remainder = 0;
        for (size_t i = limbs_.size(); i-- > 0;) {
            const uint64_t cur = (remainder << 32) | limbs_[i];
            limbs_[i] = static_cast<uint32_t>(cur / divisor);
            remainder = cur % divisor;
        }
        trim();
        return static_cast<uint32_t>(remainder);
    }

    void mulSmallAdd(uint32_t factor, uint32_t add) {
        uint64_t carry = add;
        for (auto& limb : limbs_) {
            const uint64_t cur = static_cast<uint64_t>(limb) * factor + carry;
            limb = static_cast<uint32_t>(cur);
            carry = cur >> 32;
        }
        if (carry) limbs_.push_back(static_cast<uint32_t>(carry));
    }
};

// Counts are carried in pairs: every pattern, and patterns without any rest.
// "At least one rest" is the difference.
struct Count {
    BigUInt all;
    BigUInt quiet; // no rest anywhere
};

enum class Category { All, Notes, Rests };

inline Category parseCategory(std::string text) {
    for (char& c : text) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    if (text == "ALL") return Category::All;
    if (text == "NOTES") return Category::Notes;
    if (text == "RESTS") return Category::Rests;
    throw std::invalid_argument("category must be ALL, NOTES or RESTS");
}

inline const char* categoryName(Category c) {
    return c == Category::All ? "ALL" : (c == Category::Notes ? "NOTES" : "RESTS");
}

inline BigUInt selectCount(const Count& c, Category category) {
    switch (category) {
        case Category::All: return c.all;
        case Category::Notes: return c.quiet;
        case Category::Rests: return c.all - c.quiet;
    }
    return BigUInt();
}

// ---------------------------------------------------------------------------
// Exact rationals for sounding durations and onsets.
// ---------------------------------------------------------------------------
struct Rational {
    uint64_t n = 0;
    uint64_t d = 1;

    static Rational make(u128 num, u128 den) {
        if (den == 0) throw std::logic_error("zero denominator");
        u128 a = num, b = den;
        while (b) { const u128 t = a % b; a = b; b = t; }
        if (a == 0) a = 1;
        num /= a;
        den /= a;
        if (num > UINT64_MAX || den > UINT64_MAX)
            throw std::invalid_argument("duration too fine to represent exactly; reduce nesting or tuplet sizes");
        return Rational{static_cast<uint64_t>(num), static_cast<uint64_t>(den)};
    }
    Rational operator+(const Rational& o) const {
        return make(static_cast<u128>(n) * o.d + static_cast<u128>(o.n) * d, static_cast<u128>(d) * o.d);
    }
    Rational operator*(const Rational& o) const {
        const Rational a = make(n, o.d);
        const Rational b = make(o.n, d);
        return make(static_cast<u128>(a.n) * b.n, static_cast<u128>(a.d) * b.d);
    }
    std::string str() const { return std::to_string(n) + "/" + std::to_string(d); }
};

// ---------------------------------------------------------------------------
// Items and notation trees.
// ---------------------------------------------------------------------------
enum class Kind { Note, Rest, MeasureRest, Tuplet };

inline int kindOrder(Kind k) {
    switch (k) {
        case Kind::Note: return 0;
        case Kind::Rest: return 1;
        case Kind::MeasureRest: return 2;
        case Kind::Tuplet: return 3;
    }
    return 4;
}

struct Item {
    Kind kind = Kind::Note;
    int exponent = 0;     // base value 2^-exponent of a whole note (tuplet: unit)
    int dots = 0;         // dots of the value (tuplet: unit dots)
    int actual = 0;       // tuplet a
    int normal = 0;       // tuplet n
    uint64_t span = 0;    // nominal ticks occupied where the item is placed
    uint64_t content = 0; // tuplet nominal content length (a * unit)
};

struct Node {
    Kind kind = Kind::Note;
    int exponent = 0;
    int dots = 0;
    int actual = 0;
    int normal = 0;
    uint64_t span = 0;
    std::vector<Node> children;
};

inline std::string valueName(int exponent) {
    if (exponent == -2) return "L";
    if (exponent == -1) return "B";
    return std::to_string(1u << exponent);
}

inline std::string notation(const Node& node) {
    switch (node.kind) {
        case Kind::Note: return "n" + valueName(node.exponent) + std::string(static_cast<size_t>(node.dots), '.');
        case Kind::Rest: return "r" + valueName(node.exponent) + std::string(static_cast<size_t>(node.dots), '.');
        case Kind::MeasureRest: return "M";
        case Kind::Tuplet: {
            std::string out = "[" + std::to_string(node.actual) + ":" + std::to_string(node.normal);
            for (const auto& child : node.children) out += " " + notation(child);
            return out + "]";
        }
    }
    return "?";
}

inline std::string notation(const std::vector<Node>& nodes) {
    std::string out;
    for (const auto& node : nodes) {
        if (!out.empty()) out += ' ';
        out += notation(node);
    }
    return out;
}

inline bool containsRest(const Node& node) {
    if (node.kind == Kind::Rest || node.kind == Kind::MeasureRest) return true;
    for (const auto& child : node.children) if (containsRest(child)) return true;
    return false;
}

// ---------------------------------------------------------------------------
// Configuration: the limits that make "every possibility" a finite, exact set.
// ---------------------------------------------------------------------------
struct Config {
    int numerator = 4;
    int denominator = 4;
    int maxDenominator = 16;  // shortest base value 1/maxDenominator
    int longest = 2;          // longest base value in whole notes: 1, 2 (breve) or 4 (longa)
    int maxDots = 2;
    int depth = 2;            // tuplet nesting depth (0 = no tuplets)
    int maxActual = 12;
    int maxNormal = 16;
    bool anyNormal = false;   // false: conventional normals; true: every 1..maxNormal
    std::set<std::pair<int, int>> ratios; // explicit ratio list (overrides the rule above)
    int unitDots = 1;         // tuplet units may be dotted up to this many dots
    int minTupletItems = 2;   // 1 or 2
    bool measureRest = true;
    std::set<int> values;     // allowed base exponents for notes/rests (empty = all)
    std::set<int> dotCounts;  // allowed dot counts for notes/rests (empty = all)

    int maxExponent() const {
        int e = 0;
        while ((1 << e) < maxDenominator) ++e;
        return e;
    }
    int minExponent() const { return longest == 4 ? -2 : (longest == 2 ? -1 : 0); }

    std::string signature() const {
        std::ostringstream out;
        out << numerator << '/' << denominator << '|' << maxDenominator << '|' << longest << '|'
            << maxDots << '|' << depth << '|' << maxActual << '|' << maxNormal << '|' << anyNormal
            << '|' << unitDots << '|' << minTupletItems << '|' << measureRest << "|r";
        for (const auto& r : ratios) out << r.first << ':' << r.second << ',';
        out << "|v";
        for (int v : values) out << v << ',';
        out << "|d";
        for (int d : dotCounts) out << d << ',';
        return out.str();
    }
};

inline bool isPowerOfTwo(uint64_t x) { return x && !(x & (x - 1)); }

inline int exponentForValue(const std::string& text) {
    if (text == "B" || text == "b") return -1;
    if (text == "L" || text == "l") return -2;
    if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos)
        throw std::invalid_argument("note value must be a power of two, B (breve) or L (longa): " + text);
    const unsigned long v = std::stoul(text);
    if (!isPowerOfTwo(v) || v > 4096) throw std::invalid_argument("note value must be a power of two: " + text);
    int e = 0;
    while ((1ul << e) < v) ++e;
    return e;
}

// Conventional normals for a tuplet of `actual` notes:
//   simple time:   the largest power of two below a  (3:2, 5:4, 6:4, 7:4, 9:8 ...)
//   compound time: duplets/quadruplets/octuplets 2:3, 4:3, 4:6, 8:6, 8:12
inline std::vector<int> conventionalNormals(int actual) {
    std::vector<int> out;
    if (!isPowerOfTwo(static_cast<uint64_t>(actual))) {
        int p = 1;
        while (p * 2 < actual) p *= 2;
        out.push_back(p);
    } else {
        if ((actual * 3) % 2 == 0) out.push_back(actual * 3 / 2);
        if (actual >= 4) out.push_back(actual * 3 / 4);
    }
    return out;
}

// ---------------------------------------------------------------------------
// The counting tables for one configuration.
// ---------------------------------------------------------------------------
class Space {
public:
    explicit Space(const Config& cfg) : cfg_(cfg) {
        validate();
        dotScale_ = uint64_t(1) << cfg_.maxDots;
        ticksPerWhole_ = static_cast<uint64_t>(cfg_.maxDenominator) * dotScale_;
        const uint64_t scaled = static_cast<uint64_t>(cfg_.numerator) * ticksPerWhole_;
        if (scaled % static_cast<uint64_t>(cfg_.denominator) != 0)
            throw std::invalid_argument("meter is not representable at this resolution");
        measureTicks_ = scaled / static_cast<uint64_t>(cfg_.denominator);
        build();
    }

    const Config& config() const { return cfg_; }
    uint64_t ticksPerWhole() const { return ticksPerWhole_; }
    uint64_t measureTicks() const { return measureTicks_; }
    int levels() const { return static_cast<int>(alphabet_.size()); }
    const std::vector<Item>& alphabet(int level) const { return alphabet_[static_cast<size_t>(level)]; }
    uint64_t bound(int level) const { return bounds_[static_cast<size_t>(level)]; }

    // Sequences (>= 0 items) of nominal length n at a nesting level.
    const Count& sequences(int level, uint64_t n) const { return F_[static_cast<size_t>(level)].at(n); }
    // Valid tuplet contents (respecting min items) of nominal length n at a level >= 1.
    const Count& contents(int level, uint64_t n) const { return T_[static_cast<size_t>(level)].at(n); }

    // Weight of choosing an item: 1 for notes/rests; content count for tuplets.
    Count weight(int level, const Item& item) const {
        Count w;
        if (item.kind == Kind::Tuplet) return contents(level + 1, item.content);
        w.all = BigUInt(1);
        w.quiet = BigUInt(item.kind == Kind::Note ? 1 : 0);
        return w;
    }

    uint64_t valueTicks(int exponent, int dots) const {
        const uint64_t base = exponent >= 0 ? (ticksPerWhole_ >> exponent) : (ticksPerWhole_ << (-exponent));
        return base * ((uint64_t(2) << dots) - 1) / (uint64_t(1) << dots);
    }
    bool valueRepresentable(int exponent, int dots) const {
        if (exponent < -2 || exponent > cfg_.maxExponent() || dots < 0 || dots > cfg_.maxDots) return false;
        return true;
    }
    // Find the (exponent, dots) whose ticks equal t, if any.
    bool valueForTicks(uint64_t t, int& exponent, int& dots) const {
        for (int e = -2; e <= cfg_.maxExponent(); ++e)
            for (int k = 0; k <= cfg_.maxDots; ++k)
                if (valueTicks(e, k) == t) { exponent = e; dots = k; return true; }
        return false;
    }

    size_t simpleCount(int level) const {
        size_t n = 0;
        for (const auto& item : alphabet(level)) n += item.kind != Kind::Tuplet;
        return n;
    }
    size_t tupletCount(int level) const { return alphabet(level).size() - simpleCount(level); }

private:
    Config cfg_;
    uint64_t dotScale_ = 1;
    uint64_t ticksPerWhole_ = 1;
    uint64_t measureTicks_ = 0;
    std::vector<std::vector<Item>> alphabet_;
    std::vector<uint64_t> bounds_;
    std::vector<std::vector<Count>> F_;
    std::vector<std::vector<Count>> T_;

    void validate() const {
        if (cfg_.numerator < 1 || cfg_.numerator > 1000) throw std::invalid_argument("meter numerator must be 1..1000");
        if (!isPowerOfTwo(static_cast<uint64_t>(cfg_.denominator)) || cfg_.denominator > 1024)
            throw std::invalid_argument("meter denominator must be a power of two <= 1024");
        if (!isPowerOfTwo(static_cast<uint64_t>(cfg_.maxDenominator)) || cfg_.maxDenominator > 1024)
            throw std::invalid_argument("max_denominator must be a power of two from 1 to 1024");
        if (cfg_.longest != 1 && cfg_.longest != 2 && cfg_.longest != 4)
            throw std::invalid_argument("longest must be 1 (whole), 2 (breve) or 4 (longa)");
        if (cfg_.maxDots < 0 || cfg_.maxDots > 4) throw std::invalid_argument("max_dots must be 0..4");
        if (cfg_.depth < 0 || cfg_.depth > 4) throw std::invalid_argument("depth must be 0..4");
        if (cfg_.maxActual < 2 || cfg_.maxActual > 32) throw std::invalid_argument("max_actual must be 2..32");
        if (cfg_.maxNormal < 1 || cfg_.maxNormal > 32) throw std::invalid_argument("max_normal must be 1..32");
        if (cfg_.unitDots < 0 || cfg_.unitDots > cfg_.maxDots)
            throw std::invalid_argument("unit_dots must be 0..max_dots");
        if (cfg_.minTupletItems != 1 && cfg_.minTupletItems != 2)
            throw std::invalid_argument("min_tuplet_items must be 1 or 2");
        for (const auto& r : cfg_.ratios) {
            if (r.first < 2 || r.first > 32 || r.second < 1 || r.second > 32 || r.first == r.second)
                throw std::invalid_argument("tuplet ratios need 2 <= a <= 32, 1 <= n <= 32 and a != n");
        }
    }

    std::vector<std::pair<int, int>> tupletRatios() const {
        std::vector<std::pair<int, int>> out;
        if (!cfg_.ratios.empty()) return {cfg_.ratios.begin(), cfg_.ratios.end()};
        for (int a = 2; a <= cfg_.maxActual; ++a) {
            if (cfg_.anyNormal) {
                for (int n = 1; n <= cfg_.maxNormal; ++n) if (n != a) out.push_back({a, n});
            } else {
                for (int n : conventionalNormals(a)) if (n <= cfg_.maxNormal) out.push_back({a, n});
            }
        }
        return out;
    }

    void build() {
        const int levels = cfg_.depth + 1;
        alphabet_.assign(static_cast<size_t>(levels), {});
        bounds_.assign(static_cast<size_t>(levels), 0);
        F_.assign(static_cast<size_t>(levels), {});
        T_.assign(static_cast<size_t>(levels), {});

        // Notes and rests (identical at every level).
        std::vector<Item> simple;
        for (int e = cfg_.minExponent(); e <= cfg_.maxExponent(); ++e) {
            if (!cfg_.values.empty() && !cfg_.values.count(e)) continue;
            for (int k = 0; k <= cfg_.maxDots; ++k) {
                if (!cfg_.dotCounts.empty() && !cfg_.dotCounts.count(k)) continue;
                const uint64_t t = valueTicks(e, k);
                simple.push_back(Item{Kind::Note, e, k, 0, 0, t, 0});
                simple.push_back(Item{Kind::Rest, e, k, 0, 0, t, 0});
            }
        }

        // Tuplet shapes; placed at levels < depth.
        std::vector<Item> tuplets;
        if (cfg_.depth > 0) {
            for (const auto& [a, n] : tupletRatios()) {
                for (int e = cfg_.minExponent(); e <= cfg_.maxExponent(); ++e) {
                    for (int k = 0; k <= cfg_.unitDots; ++k) {
                        const uint64_t unit = valueTicks(e, k);
                        tuplets.push_back(Item{Kind::Tuplet, e, k, a, n,
                                               unit * static_cast<uint64_t>(n),
                                               unit * static_cast<uint64_t>(a)});
                    }
                }
            }
        }

        // Nominal lengths needed per level.
        bounds_[0] = measureTicks_;
        for (int level = 0; level + 1 < levels; ++level) {
            uint64_t next = 0;
            for (const auto& t : tuplets)
                if (t.span <= bounds_[static_cast<size_t>(level)]) next = std::max(next, t.content);
            bounds_[static_cast<size_t>(level) + 1] = next;
        }
        uint64_t work = 0;
        for (uint64_t b : bounds_) {
            if (b > (uint64_t(1) << 20))
                throw std::invalid_argument("search space too large for these limits; lower max_denominator, max_dots, depth or tuplet sizes");
            work += b;
        }
        if (work * (simple.size() + tuplets.size()) > 400000000ull)
            throw std::invalid_argument("search space too large for these limits; lower max_denominator, max_dots, depth or tuplet sizes");

        // Deepest level first: its alphabet has no tuplets.
        for (int level = levels - 1; level >= 0; --level) {
            const size_t L = static_cast<size_t>(level);
            const uint64_t B = bounds_[L];
            std::vector<Item>& items = alphabet_[L];
            for (const auto& s : simple) if (s.span <= B) items.push_back(s);
            if (level == 0 && cfg_.measureRest && measureTicks_ != ticksPerWhole_)
                items.push_back(Item{Kind::MeasureRest, 0, 0, 0, 0, measureTicks_, 0});
            if (level + 1 < levels) {
                for (const auto& t : tuplets) {
                    if (t.span > B || t.content > bounds_[L + 1]) continue;
                    if (T_[L + 1][t.content].all.isZero()) continue;
                    items.push_back(t);
                }
            }
            std::stable_sort(items.begin(), items.end(), [](const Item& a, const Item& b) {
                if (a.span != b.span) return a.span > b.span;
                if (a.kind != b.kind) return kindOrder(a.kind) < kindOrder(b.kind);
                if (a.actual != b.actual) return a.actual < b.actual;
                if (a.normal != b.normal) return a.normal < b.normal;
                if (a.exponent != b.exponent) return a.exponent < b.exponent;
                return a.dots < b.dots;
            });

            // Group weights by span: sequences(N) = sum_span W(span) * sequences(N - span).
            std::map<uint64_t, Count> grouped;
            std::map<uint64_t, std::pair<uint32_t, uint32_t>> smallGroups; // span -> (all, quiet)
            for (const auto& item : items) {
                if (item.kind == Kind::Tuplet) {
                    const Count& w = T_[L + 1][item.content];
                    grouped[item.span].all += w.all;
                    grouped[item.span].quiet += w.quiet;
                } else {
                    auto& g = smallGroups[item.span];
                    g.first += 1;
                    g.second += item.kind == Kind::Note ? 1 : 0;
                }
            }
            std::vector<Count>& F = F_[L];
            F.assign(B + 1, Count{});
            F[0].all = BigUInt(1);
            F[0].quiet = BigUInt(1);
            for (uint64_t N = 1; N <= B; ++N) {
                Count& out = F[N];
                for (const auto& [span, g] : smallGroups) {
                    if (span > N) break;
                    out.all.addMultiple(F[N - span].all, g.first);
                    out.quiet.addMultiple(F[N - span].quiet, g.second);
                }
                for (const auto& [span, w] : grouped) {
                    if (span > N) break;
                    if (!F[N - span].all.isZero()) out.all += w.all * F[N - span].all;
                    if (!F[N - span].quiet.isZero() && !w.quiet.isZero()) out.quiet += w.quiet * F[N - span].quiet;
                }
            }

            // Tuplet contents at this level: sequences minus single-item fills.
            if (level >= 1) {
                std::vector<Count>& T = T_[L];
                T = F;
                T[0] = Count{};
                if (cfg_.minTupletItems >= 2) {
                    for (const auto& item : items) {
                        if (item.kind == Kind::MeasureRest) continue;
                        Count single;
                        if (item.kind == Kind::Tuplet) {
                            single = T_[L + 1][item.content];
                        } else {
                            single.all = BigUInt(1);
                            single.quiet = BigUInt(item.kind == Kind::Note ? 1 : 0);
                        }
                        T[item.span].all -= single.all;
                        T[item.span].quiet -= single.quiet;
                    }
                }
            }
        }
    }
};

// ---------------------------------------------------------------------------
// Notation parsing (concrete items and templates).
// ---------------------------------------------------------------------------
struct Token {
    enum Type { Concrete, One, Star, Gap } type = Concrete;
    Node node;
    uint64_t span = 0;   // concrete span / gap length
    bool hasRest = false;
};

class NotationParser {
public:
    NotationParser(const Space& space, const std::string& text) : space_(space) { tokenize(text); }

    std::vector<Token> parseTemplate() {
        std::vector<Token> out;
        while (pos_ < tokens_.size()) {
            const std::string& t = tokens_[pos_];
            if (t == "?") { ++pos_; out.push_back(Token{Token::One, {}, 0, false}); continue; }
            if (t == "*") { ++pos_; out.push_back(Token{Token::Star, {}, 0, false}); continue; }
            if (t == "+") {
                ++pos_;
                out.push_back(Token{Token::One, {}, 0, false});
                out.push_back(Token{Token::Star, {}, 0, false});
                continue;
            }
            if (t.rfind("*=", 0) == 0) {
                ++pos_;
                const uint64_t ticks = parseDurationTicks(t.substr(2));
                if (ticks == 0) throw std::invalid_argument("gap duration must be positive");
                out.push_back(Token{Token::Gap, {}, ticks, false});
                continue;
            }
            Node node = parseItem();
            Token tok;
            tok.type = Token::Concrete;
            tok.span = node.span;
            tok.hasRest = containsRest(node);
            tok.node = std::move(node);
            out.push_back(std::move(tok));
        }
        if (out.empty()) throw std::invalid_argument("empty template");
        return out;
    }

    std::vector<Node> parseSequence() {
        std::vector<Node> out;
        while (pos_ < tokens_.size()) out.push_back(parseItem());
        if (out.empty()) throw std::invalid_argument("empty notation");
        return out;
    }

private:
    const Space& space_;
    std::vector<std::string> tokens_;
    size_t pos_ = 0;

    void tokenize(const std::string& text) {
        std::string cur;
        auto flush = [&] { if (!cur.empty()) { tokens_.push_back(cur); cur.clear(); } };
        for (char c : text) {
            if (std::isspace(static_cast<unsigned char>(c))) { flush(); continue; }
            if (c == '[' || c == ']') { flush(); tokens_.push_back(std::string(1, c)); continue; }
            cur += c;
        }
        flush();
    }

    uint64_t parseDurationTicks(const std::string& text) const {
        const size_t slash = text.find('/');
        if (slash == std::string::npos) throw std::invalid_argument("durations must look like 3/16");
        const uint64_t n = std::stoull(text.substr(0, slash));
        const uint64_t d = std::stoull(text.substr(slash + 1));
        if (d == 0 || (n * space_.ticksPerWhole()) % d != 0)
            throw std::invalid_argument("duration not representable on the nominal grid: " + text);
        return n * space_.ticksPerWhole() / d;
    }

    Node parseItem() {
        if (pos_ >= tokens_.size()) throw std::invalid_argument("unexpected end of notation");
        const std::string t = tokens_[pos_++];
        if (t == "[") return parseTuplet();
        if (t == "]") throw std::invalid_argument("unexpected ]");
        if (t == "M") {
            Node node;
            node.kind = Kind::MeasureRest;
            node.span = space_.measureTicks();
            return node;
        }
        if (t.size() < 2 || (t[0] != 'n' && t[0] != 'r'))
            throw std::invalid_argument("notation items look like n4, r8., nB, M or [3:2 ...]: " + t);
        size_t dotStart = t.find('.');
        if (dotStart == std::string::npos) dotStart = t.size();
        const std::string dots = t.substr(dotStart);
        if (dots.find_first_not_of('.') != std::string::npos) throw std::invalid_argument("bad dots in " + t);
        Node node;
        node.kind = t[0] == 'n' ? Kind::Note : Kind::Rest;
        node.exponent = exponentForValue(t.substr(1, dotStart - 1));
        node.dots = static_cast<int>(dots.size());
        if (!space_.valueRepresentable(node.exponent, node.dots))
            throw std::invalid_argument("value outside the configured grid (max_denominator / max_dots): " + t);
        node.span = space_.valueTicks(node.exponent, node.dots);
        return node;
    }

    Node parseTuplet() {
        if (pos_ >= tokens_.size()) throw std::invalid_argument("unterminated tuplet");
        const std::string ratio = tokens_[pos_++];
        const size_t colon = ratio.find(':');
        if (colon == std::string::npos) throw std::invalid_argument("tuplets look like [3:2 ...]");
        Node node;
        node.kind = Kind::Tuplet;
        node.actual = std::stoi(ratio.substr(0, colon));
        node.normal = std::stoi(ratio.substr(colon + 1));
        if (node.actual < 1 || node.normal < 1) throw std::invalid_argument("tuplet ratio must be positive");
        uint64_t content = 0;
        while (true) {
            if (pos_ >= tokens_.size()) throw std::invalid_argument("unterminated tuplet");
            if (tokens_[pos_] == "]") { ++pos_; break; }
            Node child = parseItem();
            if (child.kind == Kind::MeasureRest) throw std::invalid_argument("M cannot appear inside a tuplet");
            content += child.span;
            node.children.push_back(std::move(child));
        }
        if (node.children.empty()) throw std::invalid_argument("empty tuplet");
        if (content % static_cast<uint64_t>(node.actual) != 0)
            throw std::invalid_argument("tuplet content is not a whole number of units: " + notation(node));
        const uint64_t unit = content / static_cast<uint64_t>(node.actual);
        if (!space_.valueForTicks(unit, node.exponent, node.dots))
            throw std::invalid_argument("tuplet unit (content / actual) is not a note value: " + notation(node));
        node.span = unit * static_cast<uint64_t>(node.normal);
        return node;
    }
};

// ---------------------------------------------------------------------------
// Queries: counting with templates and decoding any index into its pattern.
// ---------------------------------------------------------------------------
class Query {
public:
    // tokens empty => the whole measure is free.
    Query(std::shared_ptr<const Space> space, std::vector<Token> tokens)
        : space_(std::move(space)), tokens_(std::move(tokens)) {
        if (!tokens_.empty()) buildTemplateTable();
    }

    Count total() const {
        if (tokens_.empty()) return space_->sequences(0, space_->measureTicks());
        return G_[0][space_->measureTicks()];
    }

    std::vector<Node> decode(Category category, BigUInt index) const {
        std::vector<Frame> stack;
        if (tokens_.empty()) stack.push_back(Frame::seq(0, space_->measureTicks(), 1, nullptr));
        else stack.push_back(Frame::tmpl(0, space_->measureTicks()));

        std::vector<Node> root;
        bool hasRest = false;
        while (!stack.empty()) {
            Frame& top = stack.back();
            if (top.isSeq && top.rem == 0) { stack.pop_back(); continue; }
            if (!top.isSeq && top.k == tokens_.size()) {
                if (top.N != 0) throw std::logic_error("template decoding ended early");
                stack.pop_back();
                continue;
            }
            if (!top.isSeq) {
                const Token& tok = tokens_[top.k];
                if (tok.type == Token::Concrete) {
                    root.push_back(tok.node);
                    hasRest = hasRest || tok.hasRest;
                    top.N -= tok.span;
                    ++top.k;
                    continue;
                }
                if (tok.type == Token::Gap) {
                    top.N -= tok.span;
                    ++top.k;
                    stack.push_back(Frame::seq(0, tok.span, 1, &root));
                    continue;
                }
            }

            // A choice point. Completions of every frame below the top.
            Count lower;
            lower.all = BigUInt(1);
            lower.quiet = BigUInt(1);
            for (size_t i = 0; i + 1 < stack.size(); ++i) {
                const Count c = completions(stack[i]);
                lower.all = lower.all * c.all;
                lower.quiet = lower.quiet * c.quiet;
            }

            std::vector<Choice> choices = choicesFor(top);
            const bool factorable = category != Category::Rests || hasRest;
            const BigUInt& L = category == Category::Notes ? lower.quiet : lower.all;
            BigUInt q, r;
            if (factorable) {
                if (L.isZero()) throw std::logic_error("index decoding reached an empty branch");
                BigUInt::divmod(index, L, q, r);
            }

            const Choice* picked = nullptr;
            for (const Choice& choice : choices) {
                if (factorable) {
                    const BigUInt& c = category == Category::Notes ? choice.count.quiet : choice.count.all;
                    if (q < c) { index = q * L + r; picked = &choice; break; }
                    q -= c;
                } else {
                    const BigUInt block = choice.count.all * lower.all - choice.count.quiet * lower.quiet;
                    if (index < block) { picked = &choice; break; }
                    index -= block;
                }
            }
            if (!picked) throw std::logic_error("index out of range during decoding");
            apply(stack, *picked, root, hasRest);
        }
        if (!index.isZero()) throw std::logic_error("index not fully consumed");
        return root;
    }

private:
    struct Frame {
        bool isSeq = true;
        int level = 0;
        uint64_t rem = 0;
        int items = 0;
        int minItems = 1;
        std::vector<Node>* target = nullptr; // nullptr = root
        size_t k = 0;
        uint64_t N = 0;

        static Frame seq(int level, uint64_t rem, int minItems, std::vector<Node>* target) {
            Frame f;
            f.isSeq = true; f.level = level; f.rem = rem; f.minItems = minItems; f.target = target;
            return f;
        }
        static Frame tmpl(size_t k, uint64_t N) {
            Frame f;
            f.isSeq = false; f.k = k; f.N = N;
            return f;
        }
    };

    struct Choice {
        const Item* item = nullptr; // nullptr = end of '*'
        Count count;
    };

    std::shared_ptr<const Space> space_;
    std::vector<Token> tokens_;
    std::vector<std::vector<Count>> G_; // [token index][remaining ticks]

    static Count one() { Count c; c.all = BigUInt(1); c.quiet = BigUInt(1); return c; }

    static Count times(const Count& a, const Count& b) {
        Count c;
        c.all = a.all * b.all;
        c.quiet = a.quiet * b.quiet;
        return c;
    }

    Count completions(const Frame& f) const {
        if (f.isSeq) {
            if (f.rem == 0) return one();
            if (f.items == 0 && f.minItems >= 2) return space_->contents(f.level, f.rem);
            return space_->sequences(f.level, f.rem);
        }
        return G_[f.k][f.N];
    }

    std::vector<Choice> choicesFor(const Frame& f) const {
        std::vector<Choice> out;
        if (f.isSeq) {
            for (const Item& item : space_->alphabet(f.level)) {
                if (item.span > f.rem) continue;
                if (f.items == 0 && f.minItems >= 2 && item.span == f.rem) continue;
                const uint64_t rest = f.rem - item.span;
                const Count after = rest == 0 ? one() : space_->sequences(f.level, rest);
                const Count c = times(space_->weight(f.level, item), after);
                if (!c.all.isZero()) out.push_back(Choice{&item, c});
            }
            return out;
        }
        const Token& tok = tokens_[f.k];
        if (tok.type == Token::Star) out.push_back(Choice{nullptr, G_[f.k + 1][f.N]});
        for (const Item& item : space_->alphabet(0)) {
            if (item.span > f.N) continue;
            const Count& after = tok.type == Token::Star ? G_[f.k][f.N - item.span] : G_[f.k + 1][f.N - item.span];
            const Count c = times(space_->weight(0, item), after);
            if (!c.all.isZero()) out.push_back(Choice{&item, c});
        }
        return out;
    }

    void apply(std::vector<Frame>& stack, const Choice& choice, std::vector<Node>& root, bool& hasRest) const {
        Frame& top = stack.back();
        if (!choice.item) { ++top.k; return; } // end of '*'
        const Item& item = *choice.item;
        Node node;
        node.kind = item.kind;
        node.exponent = item.exponent;
        node.dots = item.dots;
        node.actual = item.actual;
        node.normal = item.normal;
        node.span = item.span;
        hasRest = hasRest || item.kind == Kind::Rest || item.kind == Kind::MeasureRest;

        std::vector<Node>& target = (top.isSeq && top.target) ? *top.target : root;
        const int level = top.isSeq ? top.level : 0;
        if (top.isSeq) {
            top.rem -= item.span;
            ++top.items;
        } else {
            top.N -= item.span;
            if (tokens_[top.k].type == Token::One) ++top.k;
        }
        target.push_back(std::move(node));
        if (item.kind == Kind::Tuplet) {
            Node& tupletNode = target.back();
            stack.push_back(Frame::seq(level + 1, item.content, space_->config().minTupletItems, &tupletNode.children));
        }
    }

    void buildTemplateTable() {
        const uint64_t M = space_->measureTicks();
        const size_t K = tokens_.size();
        G_.assign(K + 1, std::vector<Count>(M + 1));
        G_[K][0] = one();
        const auto& items = space_->alphabet(0);
        for (size_t k = K; k-- > 0;) {
            const Token& tok = tokens_[k];
            for (uint64_t N = 0; N <= M; ++N) {
                Count& out = G_[k][N];
                switch (tok.type) {
                    case Token::Concrete:
                        if (tok.span <= N) {
                            out.all = G_[k + 1][N - tok.span].all;
                            if (!tok.hasRest) out.quiet = G_[k + 1][N - tok.span].quiet;
                        }
                        break;
                    case Token::Gap:
                        if (tok.span <= N && tok.span <= space_->bound(0))
                            out = times(space_->sequences(0, tok.span), G_[k + 1][N - tok.span]);
                        break;
                    case Token::One:
                    case Token::Star: {
                        if (tok.type == Token::Star) out = G_[k + 1][N];
                        for (const Item& item : items) {
                            if (item.span > N) continue;
                            const Count& after = tok.type == Token::Star ? G_[k][N - item.span]
                                                                          : G_[k + 1][N - item.span];
                            if (after.all.isZero()) continue;
                            const Count w = space_->weight(0, item);
                            out.all += w.all * after.all;
                            if (!w.quiet.isZero() && !after.quiet.isZero()) out.quiet += w.quiet * after.quiet;
                        }
                        break;
                    }
                }
            }
        }
    }
};

// ---------------------------------------------------------------------------
// JSON rendering of a pattern: notation, leaf events with exact sounding
// durations and onsets, and a summary.
// ---------------------------------------------------------------------------
struct Flattened {
    std::ostringstream events;
    size_t count = 0;
    int notes = 0, rests = 0, tuplets = 0, maxDepth = 0;
    Rational onset{0, 1};
};

inline void flatten(const Space& space, const Node& node, Rational scale,
                    std::vector<std::string>& path, Flattened& out) {
    if (node.kind == Kind::Tuplet) {
        ++out.tuplets;
        path.push_back(std::to_string(node.actual) + ":" + std::to_string(node.normal));
        out.maxDepth = std::max(out.maxDepth, static_cast<int>(path.size()));
        const Rational inner = scale * Rational::make(static_cast<u128>(node.normal), static_cast<u128>(node.actual));
        for (const auto& child : node.children) flatten(space, child, inner, path, out);
        path.pop_back();
        return;
    }
    const Rational notated = Rational::make(node.span, space.ticksPerWhole());
    const Rational sounding = notated * scale;
    if (out.count++) out.events << ',';
    const bool isRest = node.kind != Kind::Note;
    (isRest ? out.rests : out.notes) += 1;
    out.events << "{\"kind\":\"" << (node.kind == Kind::Note ? "note" : (node.kind == Kind::Rest ? "rest" : "measure_rest"))
               << "\",\"notation\":\"" << notation(node) << '"';
    if (node.kind != Kind::MeasureRest)
        out.events << ",\"value\":\"" << valueName(node.exponent) << "\",\"dots\":" << node.dots;
    out.events << ",\"notated\":\"" << notated.str() << "\",\"duration\":\"" << sounding.str()
               << "\",\"onset\":\"" << out.onset.str() << "\",\"tuplets\":[";
    for (size_t i = 0; i < path.size(); ++i) out.events << (i ? "," : "") << '"' << path[i] << '"';
    out.events << "]}";
    out.onset = out.onset + sounding;
}

inline std::string patternJson(const Space& space, const std::vector<Node>& nodes,
                               const std::string& head) {
    Flattened flat;
    std::vector<std::string> path;
    for (const auto& node : nodes) flatten(space, node, Rational{1, 1}, path, flat);
    std::ostringstream out;
    out << "{\"type\":\"pattern\"" << head << ",\"notation\":\"" << notation(nodes)
        << "\",\"event_count\":" << flat.count << ",\"events\":[" << flat.events.str()
        << "],\"summary\":{\"notes\":" << flat.notes << ",\"rests\":" << flat.rests
        << ",\"tuplet_groups\":" << flat.tuplets << ",\"max_tuplet_depth\":" << flat.maxDepth
        << ",\"total_duration\":\"" << flat.onset.str() << "\"}}";
    return out.str();
}

// ---------------------------------------------------------------------------
// Option parsing and a cache of counting tables.
// ---------------------------------------------------------------------------
struct Request {
    Config config;
    std::string templateText;
    uint64_t seed = 1;
    std::map<std::string, std::string> raw;
};

inline std::vector<std::string> splitList(const std::string& s, char sep) {
    std::vector<std::string> out;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, sep)) if (!item.empty()) out.push_back(item);
    return out;
}

inline int parseInt(const std::string& s, const std::string& name) {
    size_t used = 0;
    const int v = std::stoi(s, &used);
    if (used != s.size()) throw std::invalid_argument("invalid integer for " + name);
    return v;
}

// `tokens` are key=value options; `template=` consumes the rest of the line.
inline Request parseRequest(const std::vector<std::string>& tokens, int numerator, int denominator,
                            int defaultMaxDenominator) {
    Request req;
    req.config.numerator = numerator;
    req.config.denominator = denominator;
    req.config.maxDenominator = defaultMaxDenominator;
    for (size_t i = 0; i < tokens.size(); ++i) {
        const std::string& tok = tokens[i];
        const size_t eq = tok.find('=');
        if (eq == std::string::npos || eq == 0) throw std::invalid_argument("options must use key=value: " + tok);
        const std::string key = tok.substr(0, eq);
        std::string value = tok.substr(eq + 1);
        if (key == "template") {
            for (size_t j = i + 1; j < tokens.size(); ++j) value += " " + tokens[j];
            req.templateText = value;
            break;
        }
        if (req.raw.count(key)) throw std::invalid_argument("duplicate option: " + key);
        req.raw[key] = value;
        Config& c = req.config;
        if (key == "max_denominator") c.maxDenominator = parseInt(value, key);
        else if (key == "longest") c.longest = parseInt(value, key);
        else if (key == "max_dots") c.maxDots = parseInt(value, key);
        else if (key == "depth") c.depth = parseInt(value, key);
        else if (key == "max_actual") c.maxActual = parseInt(value, key);
        else if (key == "max_normal") c.maxNormal = parseInt(value, key);
        else if (key == "unit_dots") c.unitDots = parseInt(value, key);
        else if (key == "min_tuplet_items") c.minTupletItems = parseInt(value, key);
        else if (key == "measure_rest") c.measureRest = parseInt(value, key) != 0;
        else if (key == "seed") req.seed = static_cast<uint64_t>(std::stoull(value));
        else if (key == "tuplet_normals") {
            if (value == "any") c.anyNormal = true;
            else if (value == "standard") c.anyNormal = false;
            else throw std::invalid_argument("tuplet_normals must be standard or any");
        } else if (key == "tuplet_ratios") {
            if (value == "none") { c.depth = 0; continue; }
            for (const auto& r : splitList(value, ',')) {
                const auto parts = splitList(r, ':');
                if (parts.size() != 2) throw std::invalid_argument("tuplet_ratios look like 3:2,5:4");
                c.ratios.insert({parseInt(parts[0], key), parseInt(parts[1], key)});
            }
        } else if (key == "values") {
            for (const auto& v : splitList(value, ',')) c.values.insert(exponentForValue(v));
        } else if (key == "dots") {
            for (const auto& v : splitList(value, ',')) c.dotCounts.insert(parseInt(v, key));
        } else {
            throw std::invalid_argument("unknown option: " + key);
        }
    }
    if (req.config.unitDots > req.config.maxDots) req.config.unitDots = req.config.maxDots;
    return req;
}

class SpaceCache {
public:
    std::shared_ptr<const Space> get(const Config& cfg) {
        const std::string key = cfg.signature();
        auto it = cache_.find(key);
        if (it != cache_.end()) return it->second;
        auto space = std::make_shared<const Space>(cfg);
        if (cache_.size() >= 16) cache_.clear();
        cache_[key] = space;
        return space;
    }

private:
    std::map<std::string, std::shared_ptr<const Space>> cache_;
};

inline std::string configJson(const Space& space) {
    const Config& c = space.config();
    std::ostringstream out;
    out << "{\"meter\":\"" << c.numerator << '/' << c.denominator << "\",\"max_denominator\":" << c.maxDenominator
        << ",\"longest\":\"" << valueName(c.minExponent()) << "\",\"max_dots\":" << c.maxDots
        << ",\"depth\":" << c.depth << ",\"max_actual\":" << c.maxActual << ",\"max_normal\":" << c.maxNormal
        << ",\"tuplet_normals\":\"" << (c.ratios.empty() ? (c.anyNormal ? "any" : "standard") : "explicit")
        << "\",\"unit_dots\":" << c.unitDots << ",\"min_tuplet_items\":" << c.minTupletItems
        << ",\"measure_rest\":" << (c.measureRest ? "true" : "false")
        << ",\"nominal_ticks_per_whole\":" << space.ticksPerWhole()
        << ",\"measure_ticks\":" << space.measureTicks()
        << ",\"top_level_note_rest_shapes\":" << space.simpleCount(0)
        << ",\"top_level_tuplet_shapes\":" << space.tupletCount(0) << "}";
    return out.str();
}

} // namespace rhythm_space
