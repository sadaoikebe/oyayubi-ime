// REDESIGN §7.8: the TIP's decisions without TSF.
#include "../tip_logic.h"

#include <cassert>
#include <cstdio>

using namespace tip_logic;

static Message Parse(const std::string& line) {
    Message m;
    assert(ParseMessage(line, m));
    return m;
}

static void ReadingReplies() {
    // An engine reply as the broker forwards it (with the sid it added).
    Message r = Parse(R"({"sid":4,"seq":17,"direct":false,"pass_through":false,
        "composition":"か","commit":"","converted":true,"fsm":"S1","n_tokens":1,
        "timer":1234,"candidates":["花","x"],"cand_index":1,"segments":[
        {"start":0,"end":1,"display_start":0,"display_end":1}],"focus":0,
        "candidate_page":2,"candidate_kinds":["dictionary"],"has_next_page":true,
        "predicting":false,"pending":true,"full":true})");
    assert(r.has_seq && r.seq == 17 && r.engine && !r.hello);
    assert(r.composition == "\xE3\x81\x8B" && r.converted && r.pending && r.full);
    assert(r.has_timer && r.timer == 1234 && r.n_tokens == 1 && r.page == 2);
    assert(r.candidates.size() == 2 && r.cand_index == 1 && r.has_next_page);
    assert(r.segments.size() == 1 && r.segments[0].display_end == 1);

    // §5.5: missing fields default, unknown ones are ignored, wrong types default.
    Message sparse = Parse(R"({"seq":3,"composition":7,"new_field":{"x":[1]},"timer":null})");
    assert(sparse.engine && sparse.composition.empty() && !sparse.has_timer);
    assert(sparse.focus == -1 && sparse.candidates.empty() && !sparse.error);
    Message bad_segments = Parse(R"({"seq":1,"segments":[{"display_start":2,"display_end":1},
        {"display_start":"x"},3]})");
    assert(bad_segments.segments.size() == 1 && bad_segments.segments[0].display_end == 2);

    Message notjson;
    assert(!ParseMessage("not json", notjson) && !ParseMessage("[1]", notjson));
}

static void ReadingBrokerMessages() {
    Message hello = Parse(R"({"proto":1,"state":"starting","warnings":["config","lm"]})");
    assert(hello.hello && hello.proto == 1 && hello.state == "starting");
    assert(hello.warnings.size() == 2 && !hello.engine);
    Message down = Parse(R"({"seq":9,"state":"down"})");
    assert(down.has_seq && !down.engine && down.state == "down");
    Message event = Parse(R"({"event":"state","state":"ready"})");
    assert(event.event == "state" && event.state == "ready" && !event.has_seq);
    Message incompatible = Parse(R"({"proto":1,"state":"incompatible"})");
    assert(incompatible.hello && incompatible.state == "incompatible");
}

static void CommittingVisibleText() {
    assert(CommitsVisibleText(Parse(R"({"event":"reset"})")));
    assert(CommitsVisibleText(Parse(R"({"seq":1,"error":true,"composition":""})")));
    for (const char* s : {"down", "maintenance", "incompatible", "not-installed"}) {
        assert(CommitsVisibleText(Parse(std::string("{\"seq\":2,\"state\":\"") + s + "\"}")));
        assert(CommitsVisibleText(Parse(std::string("{\"event\":\"state\",\"state\":\"") + s + "\"}")));
    }
    assert(!CommitsVisibleText(Parse(R"({"event":"state","state":"starting"})")));
    assert(!CommitsVisibleText(Parse(R"({"event":"state","state":"ready"})")));
    assert(!CommitsVisibleText(Parse(R"({"seq":3,"composition":"x"})")));
}

static void StatesAndWhatTheUserSees() {
    assert(ModeFor(Link::Connected, "ready") == Mode::Normal);
    assert(ModeFor(Link::Connected, "starting") == Mode::Preparing);
    assert(ModeFor(Link::Connected, "") == Mode::Preparing);  // before the hello reply
    assert(ModeFor(Link::Connected, "down") == Mode::Stopped);
    for (const char* s : {"maintenance", "not-installed", "incompatible"})
        assert(ModeFor(Link::Connected, s) == Mode::Excluded);
    assert(ModeFor(Link::Connecting, "") == Mode::Preparing);
    assert(ModeFor(Link::Unreachable, "ready") == Mode::Stopped);
    assert(ModeFor(Link::NotAllowed, "") == Mode::Excluded);

    assert(SendsKeys(Mode::Normal) && SendsKeys(Mode::Preparing));
    assert(!SendsKeys(Mode::Stopped) && !SendsKeys(Mode::Excluded));
    assert(std::string(Glyph(Mode::Normal, false)) == "\xE3\x81\x82");
    assert(std::string(Glyph(Mode::Normal, true)) == "A");
    assert(std::string(Glyph(Mode::Stopped, false)) == "!");
    assert(std::string(Glyph(Mode::Excluded, false)) == "A");
    assert(Faint(Mode::Preparing) && !Faint(Mode::Normal));

    assert((MenuFor(Mode::Normal) == std::vector<int>{MENU_TOGGLE, MENU_ADMIN}));
    assert((MenuFor(Mode::Preparing) == std::vector<int>{MENU_PREPARING, MENU_ADMIN}));
    assert((MenuFor(Mode::Stopped) == std::vector<int>{MENU_RETRY, MENU_REASON, MENU_ADMIN}));
}

static void EatingKeys() {
    Mods none, shift, ctrl;
    shift.shift = true;
    ctrl.ctrl = true;
    Local idle;
    assert(WantKey('J', none, idle) && WantKey(VK_SPACE_, none, idle));
    assert(!WantKey('J', ctrl, idle));  // shortcuts belong to the application
    assert(!WantKey(VK_RETURN_, none, idle) && !WantKey(VK_BACK_, none, idle));
    // §7.4.1: a character whose reply has not come yet makes Enter ours.
    Local awaiting;
    awaiting.awaiting = true;
    assert(WantKey(VK_RETURN_, none, awaiting) && WantKey(VK_BACK_, none, awaiting));
    assert(!WantKey(VK_LEFT_, none, awaiting));
    Local converted;
    converted.composing = converted.converted = converted.candidates = true;
    assert(WantKey(VK_LEFT_, none, converted) && WantKey(VK_DOWN_, none, converted));
    assert(!WantKey(VK_DOWN_, shift, converted) && WantKey(VK_RIGHT_, shift, converted));
    assert(!WantKey(VK_F8_, shift, converted) && WantKey(VK_F8_, none, converted));

    Message reply = Parse(R"({"seq":1,"composition":"x","n_tokens":1})");
    assert(EatAfterReply(true, 'J', none, idle, false, 0, reply));
    Message passed = Parse(R"({"seq":1,"pass_through":true})");
    assert(!EatAfterReply(true, 'J', none, idle, false, 0, passed));
    Message empty = Parse(R"({"seq":1})");
    assert(!EatAfterReply(true, VK_RETURN_, none, idle, false, 0, empty));
    assert(EatAfterReply(false, 'J', none, idle, false, 0, empty));  // a consumed down's up
    Message tab = Parse(R"({"seq":1,"predicting":true})");
    Local composing;
    composing.composing = true;
    assert(EatAfterReply(true, VK_TAB_, none, composing, false, 1, tab));
}

static void OrderOfReplies() {
    InFlight f;
    assert(!f.Waiting() && f.NoticeAt() == 0);
    long long a = f.Next(1000), b = f.Next(1050), c = f.Next(1100);
    assert(a == 1 && b == 2 && c == 3 && f.Waiting(2));
    assert(f.NoticeAt() == 1000 + PENDING_NOTICE_MS);
    assert(f.Answer(1));
    assert(f.NoticeAt() == 1050 + PENDING_NOTICE_MS);
    assert(!f.Answer(1));  // already answered
    assert(f.Answer(3));  // 2 was never answered (the broker answers in order: dropped)
    assert(!f.Waiting() && f.Last() == 3);
}

static void ConnectingAndSpawning() {
    ConnectGate gate;
    assert(gate.Allow(5000));
    assert(!gate.Allow(6999));
    assert(gate.Allow(7000));

    SpawnFacts f;
    f.tag_matches = f.medium_integrity = true;
    assert(DecideSpawn(f) == SpawnDecision::Spawn);
    f.spawns = BROKER_MAX_SPAWNS;
    assert(DecideSpawn(f) == SpawnDecision::Spent);
    f.spawns = 0;
    SpawnFacts old_dll = f;
    old_dll.tag_matches = false;  // uninstalled or updated: never starts anything
    assert(DecideSpawn(old_dll) == SpawnDecision::NotAllowed);
    SpawnFacts installing = f;
    installing.maintenance = true;
    assert(DecideSpawn(installing) == SpawnDecision::NotAllowed);
    SpawnFacts sandboxed = f;
    sandboxed.medium_integrity = false;
    assert(DecideSpawn(sandboxed) == SpawnDecision::NotAllowed);
}

static void Requests() {
    Mods m;
    m.shift = m.alt = true;
    assert(KeyRequest(7, true, 74, 123, m) ==
           R"({"seq":7,"op":"down","vk":74,"t":123,"mods":["shift","alt"]})");
    assert(OpRequest(8, "set_direct", "\"direct\":true") ==
           R"({"seq":8,"op":"set_direct","direct":true})");
    std::string hello = Hello("v0.2.0");
    Message parsed;
    assert(ParseMessage(hello, parsed));
    tip_json::Value v;
    assert(tip_json::Parser(hello).Parse(v) && v.Get("op").text == "hello" &&
           v.Get("tag").text == "v0.2.0");
}

int main() {
    ReadingReplies();
    ReadingBrokerMessages();
    CommittingVisibleText();
    StatesAndWhatTheUserSees();
    EatingKeys();
    OrderOfReplies();
    ConnectingAndSpawning();
    Requests();
    std::puts("tip_logic_test: all passed");
    return 0;
}
