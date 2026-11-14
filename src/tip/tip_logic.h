// The TIP's decisions that do not need TSF (REDESIGN §7.8), tested on any OS by
// tests/tip_logic_test.cpp: reading the broker's messages, whether to eat a key,
// the order of replies, and what the user sees for each state.
#pragma once

#include <cstdint>
#include <deque>
#include <string>
#include <vector>

#include "tip_json.h"

namespace tip_logic {

// REDESIGN §11.
constexpr uint32_t KEY_BUDGET_MS = 200;
constexpr uint32_t PENDING_NOTICE_MS = 1000;
constexpr uint32_t CONNECT_MIN_INTERVAL_MS = 2000;
constexpr uint32_t BROKER_MAX_SPAWNS = 3;
constexpr uint32_t BROKER_WAIT_MS = 10000;
constexpr uint32_t BROKER_POLL_MS = 100;
constexpr uint32_t NOTICE_SHOW_MS = 4000;  // §9.2: the notices that go by themselves
constexpr int PROTO = 1;

// Win32 virtual keys used here (no windows.h in this header).
enum : uint32_t {
    VK_BACK_ = 0x08, VK_TAB_ = 0x09, VK_RETURN_ = 0x0D, VK_ESCAPE_ = 0x1B, VK_SPACE_ = 0x20,
    VK_PRIOR_ = 0x21, VK_NEXT_ = 0x22, VK_LEFT_ = 0x25, VK_UP_ = 0x26, VK_RIGHT_ = 0x27,
    VK_DOWN_ = 0x28, VK_F6_ = 0x75, VK_F7_ = 0x76, VK_F8_ = 0x77, VK_F9_ = 0x78, VK_F10_ = 0x79,
};

struct Segment {
    int display_start = 0, display_end = 0;
};

// One line from the broker. Every field has a default; a field that is absent
// or of another type takes it (§5.5: unknown fields are ignored, missing ones
// default, nothing is counted or rejected).
struct Message {
    // The broker's own fields.
    bool hello = false;  // the reply to hello: {"proto":1,"state":..,"warnings":[..]}
    int proto = 0;
    std::string event;  // "state", "reset", or empty
    std::string state;  // ready, starting, down, maintenance, not-installed, incompatible
    bool has_seq = false;
    long long seq = 0;
    std::vector<std::string> warnings;
    // The engine's reply (yuragi.ime.server.snapshot).
    bool engine = false;  // carries the engine's state (not just {"seq","state"})
    std::string composition, commit;  // UTF-8
    bool direct = false, pass_through = false, converted = false, pending = false;
    bool predicting = false, full = false, error = false, has_next_page = false;
    int n_tokens = 0, focus = -1, page = 0, cand_index = 0;
    bool has_timer = false;
    uint64_t timer = 0;
    std::vector<std::string> candidates;
    std::vector<Segment> segments;
};

inline bool ReadInt(const tip_json::Value& v, const char* key, int& out) {
    long long n;
    if (!v.Get(key).Int(n) || n < -2147483647LL || n > 2147483647LL) return false;
    out = (int)n;
    return true;
}
inline void ReadBool(const tip_json::Value& v, const char* key, bool& out) {
    const auto& x = v.Get(key);
    if (x.kind == tip_json::Value::Boolean) out = x.boolean;
}
inline void ReadText(const tip_json::Value& v, const char* key, std::string& out) {
    const auto& x = v.Get(key);
    if (x.kind == tip_json::Value::String) out = x.text;
}

// false only when the line is not a JSON object.
inline bool ParseMessage(const std::string& line, Message& m) {
    tip_json::Value v;
    if (!tip_json::Parser(line).Parse(v) || v.kind != tip_json::Value::Object) return false;
    m = Message();
    m.hello = v.Get("proto").kind == tip_json::Value::Number;
    ReadInt(v, "proto", m.proto);
    ReadText(v, "event", m.event);
    ReadText(v, "state", m.state);
    m.has_seq = v.Get("seq").Int(m.seq);
    for (const auto& w : v.Get("warnings").array)
        if (w.kind == tip_json::Value::String) m.warnings.push_back(w.text);
    m.engine = m.has_seq && m.state.empty();
    ReadText(v, "composition", m.composition);
    ReadText(v, "commit", m.commit);
    ReadBool(v, "direct", m.direct);
    ReadBool(v, "pass_through", m.pass_through);
    ReadBool(v, "converted", m.converted);
    ReadBool(v, "pending", m.pending);
    ReadBool(v, "predicting", m.predicting);
    ReadBool(v, "full", m.full);
    ReadBool(v, "error", m.error);
    ReadBool(v, "has_next_page", m.has_next_page);
    ReadInt(v, "n_tokens", m.n_tokens);
    ReadInt(v, "focus", m.focus);
    ReadInt(v, "candidate_page", m.page);
    ReadInt(v, "cand_index", m.cand_index);
    long long timer;
    if (v.Get("timer").Int(timer) && timer >= 0) {
        m.has_timer = true;
        m.timer = (uint64_t)timer;
    }
    for (const auto& c : v.Get("candidates").array)
        if (c.kind == tip_json::Value::String) m.candidates.push_back(c.text);
    for (const auto& s : v.Get("segments").array) {
        Segment seg;
        if (!ReadInt(s, "display_start", seg.display_start) ||
            !ReadInt(s, "display_end", seg.display_end))
            continue;
        if (seg.display_start < 0) seg.display_start = 0;
        if (seg.display_end < seg.display_start) seg.display_end = seg.display_start;
        m.segments.push_back(seg);
    }
    return true;
}

// §7.5: the text on screen stays and the conversion ends (nothing is erased).
inline bool CommitsVisibleText(const Message& m) {
    if (m.event == "reset" || m.error) return true;
    return m.state == "down" || m.state == "maintenance" || m.state == "incompatible" ||
           m.state == "not-installed";
}

// -- what the user sees (§9.1) ---------------------------------------------------

enum class Mode { Normal, Preparing, Stopped, Excluded };

// Why there is no connection to the broker.
enum class Link { Connected, Connecting, Unreachable, NotAllowed };
//   Connecting   waiting for the pipe (a broker was just started)
//   Unreachable  no broker, and this TIP may not start another (spent its spawns)
//   NotAllowed   this process must not start one (§7.3: another version, maintenance,
//                sandboxed or elevated): it only ever connects

inline Mode ModeFor(Link link, const std::string& state) {
    switch (link) {
        case Link::Connecting: return Mode::Preparing;
        case Link::Unreachable: return Mode::Stopped;
        case Link::NotAllowed: return Mode::Excluded;
        case Link::Connected: break;
    }
    if (state == "ready") return Mode::Normal;
    if (state == "starting" || state.empty()) return Mode::Preparing;
    if (state == "down") return Mode::Stopped;
    return Mode::Excluded;  // maintenance, not-installed, incompatible
}

// Keys go to the engine (ready) or to the broker's hold (starting); otherwise
// they pass through as in direct input.
inline bool SendsKeys(Mode mode) { return mode == Mode::Normal || mode == Mode::Preparing; }

// The taskbar glyph and whether it is drawn faint.
inline const char* Glyph(Mode mode, bool direct) {
    switch (mode) {
        // ゆ and Y (yuragi), not あ/A: told apart from Microsoft IME at a glance.
        case Mode::Normal: return direct ? "Y" : "\xE3\x82\x86";  // ゆ
        case Mode::Preparing: return "\xE3\x82\x86";
        case Mode::Stopped: return "!";
        case Mode::Excluded: return "Y";
    }
    return "Y";
}
inline bool Faint(Mode mode) { return mode == Mode::Preparing; }

// The right-click menu (§9.4).
enum MenuItem { MENU_TOGGLE = 1, MENU_ADMIN = 2, MENU_RETRY = 3, MENU_REASON = 4, MENU_PREPARING = 5 };
inline std::vector<int> MenuFor(Mode mode) {
    switch (mode) {
        case Mode::Normal: return {MENU_TOGGLE, MENU_ADMIN};
        case Mode::Preparing: return {MENU_PREPARING, MENU_ADMIN};
        case Mode::Stopped: return {MENU_RETRY, MENU_REASON, MENU_ADMIN};
        case Mode::Excluded: return {MENU_ADMIN};
    }
    return {};
}

// The one-line notice under the caret (§9.2).
enum class Notice { None, Preparing, Stopped, TooLong, Reopen };

// -- keys -------------------------------------------------------------------------

struct Mods {
    bool shift = false, ctrl = false, alt = false, win = false;
    bool Shortcut() const { return ctrl || alt || win; }
};

// What the TIP knows locally, from the last applied reply and the requests
// still in flight.
struct Local {
    bool composing = false, converted = false, candidates = false;
    bool awaiting = false;  // a request has no reply yet (§7.4.1)
};

// session.py and chord/keys.py: the keys the session acts on.
inline bool CharacterVk(uint32_t vk) {
    if ((vk >= 'A' && vk <= 'Z') || (vk >= '0' && vk <= '9')) return true;
    switch (vk) {
        case 0xBA: case 0xBB: case 0xBC: case 0xBD: case 0xBE: case 0xBF: case 0xC0:
        case 0xDB: case 0xDC: case 0xDD: case 0xDE: return true;
        default: return false;
    }
}

// OnTestKeyDown: whether this key is ours, from local state only (no I/O).
// A request still waiting for its reply counts as composing: once its
// character arrives the input is composing, so a following Enter or
// Backspace must not slip past it to the application.
inline bool WantKey(uint32_t vk, Mods mods, const Local& local) {
    if (mods.Shortcut()) return false;
    bool composing = local.composing || local.awaiting;
    bool converted = local.converted;
    if (CharacterVk(vk) || vk == VK_SPACE_) return true;
    if (vk == VK_RETURN_ || vk == VK_BACK_ || vk == VK_ESCAPE_) return composing;
    if (vk == VK_F7_) return true;
    if (vk == VK_F6_) return composing;
    if (vk == VK_F8_ || vk == VK_F9_ || vk == VK_F10_) return composing && !mods.shift;
    if (vk == VK_TAB_) return composing && !mods.shift;
    if (converted || (local.awaiting && local.candidates)) {
        if (vk == VK_LEFT_ || vk == VK_RIGHT_) return true;
        if (!mods.shift && (vk == VK_UP_ || vk == VK_DOWN_ || vk == VK_PRIOR_ || vk == VK_NEXT_))
            return true;
    }
    return false;
}

// After the reply came in time: whether the key was consumed (the rules of
// the TIP before the redesign, unchanged).
inline bool EatAfterReply(bool down, uint32_t vk, Mods mods, const Local& before,
                          bool before_timer, int before_tokens, const Message& r) {
    if (r.direct || r.pass_through) return false;
    bool state_mask = before.composing || before_tokens > 0 || before_timer || r.n_tokens > 0 ||
                      r.has_timer || r.converted || !r.candidates.empty() ||
                      !r.composition.empty() || !r.commit.empty();
    if (!down) return true;  // only consumed downs send their up
    if (CharacterVk(vk) || vk == VK_SPACE_) return state_mask;
    if (vk == VK_BACK_ || vk == VK_RETURN_ || vk == VK_ESCAPE_) return before.composing && state_mask;
    if (vk == VK_F7_) return true;
    if (vk == VK_F6_ || vk == VK_F8_ || vk == VK_F9_ || vk == VK_F10_)
        return before.composing || before.converted;
    if (vk == VK_TAB_) return r.predicting;
    Local wanted = before;
    wanted.awaiting = false;
    return before.converted && WantKey(vk, mods, wanted);
}

// -- order (§7.4.3) ---------------------------------------------------------------

// The requests in flight, oldest first. Replies come back in the order the
// requests went out (one reader, one broker, one engine thread).
class InFlight {
public:
    long long Next(uint64_t now) {
        long long seq = ++last_;
        sent_.push_back({seq, now});
        return seq;
    }
    // A reply for seq: true when it is the oldest one in flight (as it must be);
    // replies for requests that were given up are dropped by the caller.
    bool Answer(long long seq) {
        while (!sent_.empty() && sent_.front().seq < seq) sent_.pop_front();  // never answered
        if (sent_.empty() || sent_.front().seq != seq) return false;
        sent_.pop_front();
        return true;
    }
    bool Waiting() const { return !sent_.empty(); }
    bool Waiting(long long seq) const {
        for (const auto& s : sent_)
            if (s.seq == seq) return true;
        return false;
    }
    // When the "preparing" notice is due (§7.4.4); 0 when nothing waits.
    uint64_t NoticeAt() const { return sent_.empty() ? 0 : sent_.front().at + PENDING_NOTICE_MS; }
    void Clear() { sent_.clear(); }
    long long Last() const { return last_; }  // the newest number given out

private:
    struct Sent {
        long long seq;
        uint64_t at;
    };
    std::deque<Sent> sent_;
    long long last_ = 0;
};

// §7.2: connecting is tried on activation, on a kana key, and on "retry",
// never more often than every CONNECT_MIN_INTERVAL_MS.
class ConnectGate {
public:
    bool Allow(uint64_t now) {
        if (tried_ && now - last_ < CONNECT_MIN_INTERVAL_MS) return false;
        tried_ = true;
        last_ = now;
        return true;
    }

private:
    bool tried_ = false;
    uint64_t last_ = 0;
};

// §7.3: whether this TIP may start the broker. Every condition must hold.
struct SpawnFacts {
    bool tag_matches = false;  // HKCU\Software\yuragi-ime\Tag == this DLL's tag
    bool maintenance = false;  // Local\yuragi-ime-maintenance exists
    bool medium_integrity = false;  // not sandboxed (AppContainer/low), not elevated
    uint32_t spawns = 0;  // this sign-in, from the volatile key
};
enum class SpawnDecision { Spawn, NotAllowed, Spent };
inline SpawnDecision DecideSpawn(const SpawnFacts& f) {
    if (!f.tag_matches || f.maintenance || !f.medium_integrity) return SpawnDecision::NotAllowed;
    if (f.spawns >= BROKER_MAX_SPAWNS) return SpawnDecision::Spent;
    return SpawnDecision::Spawn;
}

// Requests (the TIP -> broker messages, §5.5).
inline std::string Hello(const std::string& tag) {
    return "{\"op\":\"hello\",\"proto\":1,\"tag\":" + tip_json::Quote(tag) + ",\"bits\":" +
           std::to_string(sizeof(void*) * 8) + "}";
}
inline std::string ModsJson(Mods m) {
    std::string out = "[";
    auto add = [&](const char* name) {
        if (out.size() > 1) out += ',';
        out += tip_json::Quote(name);
    };
    if (m.shift) add("shift");
    if (m.ctrl) add("ctrl");
    if (m.alt) add("alt");
    if (m.win) add("win");
    return out + ']';
}
inline std::string KeyRequest(long long seq, bool down, uint32_t vk, uint64_t t, Mods mods) {
    return "{\"seq\":" + std::to_string(seq) + ",\"op\":\"" + (down ? "down" : "up") +
           "\",\"vk\":" + std::to_string(vk) + ",\"t\":" + std::to_string(t) +
           ",\"mods\":" + ModsJson(mods) + "}";
}
inline std::string OpRequest(long long seq, const std::string& op, const std::string& extra = "") {
    return "{\"seq\":" + std::to_string(seq) + ",\"op\":\"" + op + "\"" +
           (extra.empty() ? "" : "," + extra) + "}";
}

}  // namespace tip_logic
