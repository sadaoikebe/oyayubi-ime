#pragma once

#include <cerrno>
#include <cstdlib>
#include <map>
#include <string>
#include <vector>
#include <utility>

// Small JSON value/parser shared by endpoint, config and protocol replies.
// Strings are UTF-8, including decoded JSON escapes. No substring field lookup.
namespace tip_json {
struct Value {
    enum Kind { Null, Boolean, Number, String, Array, Object } kind = Null;
    bool boolean = false;
    std::string text;
    std::vector<Value> array;
    std::map<std::string, Value> object;

    const Value& Get(const char* key) const {
        static const Value missing;
        auto it = object.find(key);
        return it == object.end() ? missing : it->second;
    }
    bool Int(long long& n) const {
        if (kind != Number || text.find_first_of(".eE") != std::string::npos) return false;
        char* end = nullptr;
        errno = 0;
        n = std::strtoll(text.c_str(), &end, 10);
        return errno != ERANGE && end != text.c_str() && *end == 0;
    }
};

class Parser {
    const std::string& s_;
    size_t p_ = 0;
    void Space() { while (p_ < s_.size() && (s_[p_] == ' ' || s_[p_] == '\r' ||
                       s_[p_] == '\n' || s_[p_] == '\t')) ++p_; }
    bool Take(char c) { if (p_ < s_.size() && s_[p_] == c) { ++p_; return true; } return false; }
    bool Hex(unsigned& n) {
        n = 0;
        for (int i = 0; i < 4; ++i) {
            if (p_ == s_.size()) return false;
            char c = s_[p_++];
            unsigned d;
            if (c >= '0' && c <= '9') d = c - '0';
            else if (c >= 'a' && c <= 'f') d = c - 'a' + 10;
            else if (c >= 'A' && c <= 'F') d = c - 'A' + 10;
            else return false;
            n = n * 16 + d;
        }
        return true;
    }
    static void Utf8(std::string& out, unsigned cp) {
        if (cp < 0x80) out += (char)cp;
        else if (cp < 0x800) {
            out += (char)(0xc0 | (cp >> 6)); out += (char)(0x80 | (cp & 63));
        } else if (cp < 0x10000) {
            out += (char)(0xe0 | (cp >> 12)); out += (char)(0x80 | ((cp >> 6) & 63));
            out += (char)(0x80 | (cp & 63));
        } else {
            out += (char)(0xf0 | (cp >> 18)); out += (char)(0x80 | ((cp >> 12) & 63));
            out += (char)(0x80 | ((cp >> 6) & 63)); out += (char)(0x80 | (cp & 63));
        }
    }
    bool String(std::string& out) {
        if (!Take('"')) return false;
        out.clear();
        while (p_ < s_.size()) {
            unsigned char c = (unsigned char)s_[p_++];
            if (c == '"') return true;
            if (c < 0x20) return false;
            if (c != '\\') { out += (char)c; continue; }
            if (p_ == s_.size()) return false;
            switch (s_[p_++]) {
                case '"': out += '"'; break;
                case '\\': out += '\\'; break;
                case '/': out += '/'; break;
                case 'b': out += '\b'; break;
                case 'f': out += '\f'; break;
                case 'n': out += '\n'; break;
                case 'r': out += '\r'; break;
                case 't': out += '\t'; break;
                case 'u': {
                    unsigned cp, low;
                    if (!Hex(cp)) return false;
                    if (cp >= 0xd800 && cp <= 0xdbff) {
                        if (!Take('\\') || !Take('u') || !Hex(low) ||
                            low < 0xdc00 || low > 0xdfff) return false;
                        cp = 0x10000 + ((cp - 0xd800) << 10) + low - 0xdc00;
                    } else if (cp >= 0xdc00 && cp <= 0xdfff) return false;
                    Utf8(out, cp);
                    break;
                }
                default: return false;
            }
        }
        return false;
    }
    bool Read(Value& v, unsigned depth) {
        if (depth > 64) return false;
        Space();
        if (p_ == s_.size()) return false;
        if (s_[p_] == '"') { v.kind = Value::String; return String(v.text); }
        if (Take('{')) {
            v.kind = Value::Object;
            Space();
            if (Take('}')) return true;
            do {
                Space(); std::string key; Value item;
                if (!String(key)) return false;
                Space();
                if (!Take(':') || !Read(item, depth + 1) || v.object.count(key)) return false;
                v.object.emplace(key, std::move(item));
                Space();
                if (Take('}')) return true;
            } while (Take(','));
            return false;
        }
        if (Take('[')) {
            v.kind = Value::Array;
            Space();
            if (Take(']')) return true;
            do {
                Value item;
                if (!Read(item, depth + 1)) return false;
                v.array.push_back(std::move(item));
                Space();
                if (Take(']')) return true;
            } while (Take(','));
            return false;
        }
        if (s_.compare(p_, 4, "null") == 0) { p_ += 4; return true; }
        if (s_.compare(p_, 4, "true") == 0) {
            p_ += 4; v.kind = Value::Boolean; v.boolean = true; return true;
        }
        if (s_.compare(p_, 5, "false") == 0) {
            p_ += 5; v.kind = Value::Boolean; return true;
        }
        size_t start = p_;
        Take('-');
        if (!Take('0')) {
            if (p_ == s_.size() || s_[p_] < '1' || s_[p_] > '9') return false;
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_;
        }
        if (Take('.')) {
            size_t digits = p_;
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_;
            if (digits == p_) return false;
        }
        if (Take('e') || Take('E')) {
            if (!Take('+')) Take('-');
            size_t digits = p_;
            while (p_ < s_.size() && s_[p_] >= '0' && s_[p_] <= '9') ++p_;
            if (digits == p_) return false;
        }
        v.kind = Value::Number; v.text = s_.substr(start, p_ - start);
        return true;
    }
public:
    explicit Parser(const std::string& s) : s_(s) {}
    bool Parse(Value& v) {
        v = Value();
        if (!Read(v, 0)) return false;
        Space(); return p_ == s_.size();
    }
};
// An optional boolean member: absent leaves b unchanged, any other type fails.
inline bool OptionalBool(const Value& v, const char* key, bool& b) {
    if (v.object.find(key) == v.object.end()) return true;
    const auto& x = v.Get(key);
    if (x.kind != Value::Boolean) return false;
    b = x.boolean;
    return true;
}
inline std::string Quote(const std::string& s) {
    const char* hex = "0123456789abcdef";
    std::string out = "\"";
    for (unsigned char c : s) {
        if (c == '"' || c == '\\') { out += '\\'; out += (char)c; }
        else if (c < 0x20) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += (char)c;
    }
    return out + '"';
}
}  // namespace tip_json
