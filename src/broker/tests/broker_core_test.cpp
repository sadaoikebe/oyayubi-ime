// The broker's decisions with a fake platform and a fake clock (REDESIGN §5.4-§5.7).
#include "../broker_core.h"

#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

using namespace broker;

namespace {

struct Fake : Platform {
    int64_t now = 1000;
    int starts = 0;
    bool running = false;
    bool start_ok = true;
    bool maintenance = false;
    bool quit = false;
    int kills = 0;
    int spawn_resets = 0;
    int cleared = 0;
    std::vector<std::string> to_engine;
    std::vector<std::pair<ClientId, std::string>> to_client;
    std::vector<Notice> notices;
    std::vector<std::string> log;

    int64_t Now() override { return now; }
    bool StartEngine(std::string& error) override {
        if (!start_ok) {
            error = "python.exe missing";
            return false;
        }
        ++starts;
        running = true;
        return true;
    }
    void WriteEngine(const std::string& line) override { to_engine.push_back(line); }
    void KillEngine() override { ++kills; }
    void WriteClient(ClientId c, const std::string& line) override { to_client.push_back({c, line}); }
    void Notify(const Notice& n) override { notices.push_back(n); }
    void ClearStopped() override { ++cleared; }
    void OpenAdmin() override {}
    void ResetSpawnCount() override { ++spawn_resets; }
    bool MaintenanceActive() override { return maintenance; }
    void Log(const std::string& line) override { log.push_back(line); }
    void Quit() override { quit = true; }

    std::vector<std::string> Client(ClientId c) {
        std::vector<std::string> out;
        for (auto& [id, line] : to_client)
            if (id == c) out.push_back(line);
        return out;
    }
};

// Run the broker's timers up to the fake clock's time t.
void Advance(Broker& b, Fake& f, int64_t ms) {
    int64_t end = f.now + ms;
    for (;;) {
        int64_t next = b.NextDeadline();
        if (next > end) break;
        f.now = std::max(f.now, next);
        b.OnTimer();
    }
    f.now = end;
    b.OnTimer();
}

void Ready(Broker& b) { b.OnEngineLine(R"({"ready":true,"proto":1,"warnings":[]})"); }

void Hello(Broker& b, ClientId c) {
    b.OnClientConnected(c);
    b.OnClientLine(c, R"({"op":"hello","proto":1,"tag":"t","bits":64})");
}

bool Contains(const std::vector<std::string>& lines, const std::string& text) {
    for (auto& l : lines)
        if (l == text) return true;
    return false;
}

void ImmediateCrashStartsExactlyTwice() {
    Fake f;
    Broker b(f, {});
    b.Start();
    b.OnEngineExit(1);  // before ready
    assert(b.State() == EngineState::Backoff);
    Advance(b, f, 0);  // first restart: no wait
    assert(f.starts == 2);
    b.OnEngineExit(1);
    assert(b.State() == EngineState::Failed);
    assert(f.notices.size() == 1 && f.notices[0].stopped && f.notices[0].reason == Stop::Repeated);
    Advance(b, f, 10 * 60 * 1000);  // ten minutes: nothing starts by itself
    assert(f.starts == 2);
    // A key while stopped is answered "down".
    Hello(b, 1);
    b.OnClientLine(1, R"({"seq":5,"op":"down","vk":74,"t":1})");
    assert(f.Client(1).back() == R"({"seq":5,"state":"down"})");
}

void FatalCodesNameTheReason() {
    Fake f;
    Broker b(f, {});
    b.Start();
    b.OnEngineStderrLine("fatal 21 dictionary-missing");
    b.OnEngineExit(21);
    Advance(b, f, 0);
    b.OnEngineExit(21);
    assert(b.State() == EngineState::Failed && b.LastStop() == Stop::Dictionary);
    assert(f.starts == 2);
}

void HeldKeysReachTheNextEngineInOrder() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Hello(b, 7);
    assert(f.Client(7).back() == R"({"proto":1,"state":"starting","warnings":[]})");
    b.OnClientLine(7, R"({"seq":1,"op":"down","vk":74,"t":1})");  // held while starting
    Ready(b);
    assert(f.to_engine[0] == R"({"op":"open","sid":7})");
    assert(f.to_engine[1] == R"({"sid":7,"seq":1,"op":"down","vk":74,"t":1})");
    b.OnEngineLine(R"({"sid":7,"seq":1,"composition":"x","direct":true})");
    assert(f.Client(7).back() == R"({"sid":7,"seq":1,"composition":"x","direct":true})");
    // The engine falls over while seq 2 is being processed; 3 and 4 arrive meanwhile.
    b.OnClientLine(7, R"({"seq":2,"op":"down","vk":75,"t":2})");
    f.to_engine.clear();
    b.OnEngineExit(-9);
    assert(Contains(f.Client(7), R"({"seq":2,"state":"down"})"));  // lost with the engine
    assert(f.Client(7).back() == R"({"event":"state","state":"starting"})");
    b.OnClientLine(7, R"({"seq":3,"op":"down","vk":76,"t":3})");
    b.OnClientLine(7, R"({"seq":4,"op":"up","vk":76,"t":4})");
    Advance(b, f, 0);
    assert(f.starts == 2 && b.State() == EngineState::Starting);
    f.to_client.clear();
    Ready(b);
    auto lines = f.Client(7);
    assert(lines.size() == 2 && lines[0] == R"({"event":"reset"})" &&
           lines[1] == R"({"event":"state","state":"ready"})");
    // New session, the input mode restored, then the held keys in order.
    assert(f.to_engine.size() == 4);
    assert(f.to_engine[0] == R"({"op":"open","sid":7})");
    assert(f.to_engine[1] == R"({"sid":7,"seq":-1,"op":"set_direct","direct":true})");
    assert(f.to_engine[2] == R"({"sid":7,"seq":3,"op":"down","vk":76,"t":3})");
    assert(f.to_engine[3] == R"({"sid":7,"seq":4,"op":"up","vk":76,"t":4})");
    // The broker's own request is answered to nobody.
    f.to_client.clear();
    b.OnEngineLine(R"({"sid":7,"seq":-1,"direct":true})");
    assert(f.to_client.empty());
}

void HeldKeysExpireAfterHoldMs() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Hello(b, 1);
    b.OnClientLine(1, R"({"seq":1,"op":"down","vk":74,"t":1})");
    b.OnClientLine(1, R"({"seq":2,"op":"up","vk":74,"t":2})");
    Advance(b, f, HOLD_MS - 1);
    assert(f.Client(1).size() == 1);  // only the hello reply
    Advance(b, f, 1);
    auto lines = f.Client(1);
    assert(lines.size() == 3 && lines[1] == R"({"seq":1,"state":"down"})" &&
           lines[2] == R"({"seq":2,"state":"down"})");
    // More than HOLD_MAX at once: all of them, at once.
    for (size_t i = 0; i <= HOLD_MAX; ++i)
        b.OnClientLine(1, "{\"seq\":" + std::to_string(100 + i) + ",\"op\":\"timeout\"}");
    assert(f.Client(1).size() == 3 + HOLD_MAX + 1);
}

void SilentEngineIsKilled() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Ready(b);
    Hello(b, 1);
    b.OnClientLine(1, R"({"seq":1,"op":"down","vk":74,"t":1})");
    Advance(b, f, ENGINE_HANG_MS - 1);
    assert(f.kills == 0);
    Advance(b, f, 1);
    assert(f.kills == 1);
    b.OnEngineExit(-9);
    assert(Contains(f.Client(1), R"({"seq":1,"state":"down"})"));
    Advance(b, f, 0);
    assert(f.starts == 2);  // started again
    // While starting, 30 s of silence ends it too.
    Advance(b, f, ENGINE_START_SILENCE_MS - 1);
    assert(f.kills == 1);
    b.OnEngineLine(R"({"progress":"cache"})");
    Advance(b, f, ENGINE_START_SILENCE_MS - 1);
    assert(f.kills == 1);  // progress counts as speech
    Advance(b, f, 1);
    assert(f.kills == 2);
    b.OnEngineExit(-9);  // the first exit before ready: once more, after 2 s
    assert(b.State() == EngineState::Backoff);
    Advance(b, f, 2000);
    assert(f.starts == 3);
    Advance(b, f, ENGINE_START_SILENCE_MS);
    b.OnEngineExit(-9);  // the second in a row before ready: stopped
    assert(b.State() == EngineState::Failed && b.LastStop() == Stop::Hang);
    assert(f.notices.size() == 1 && f.notices[0].reason == Stop::Hang);
}

void ThreeCrashesThenRetryStartsOnce() {
    Fake f;
    Broker b(f, {});
    b.Start();
    for (int i = 0; i < 3; ++i) {
        Ready(b);
        b.OnEngineExit(1);
        if (i < 2) {
            assert(b.State() == EngineState::Backoff);
            Advance(b, f, BACKOFF_MS[i]);
        }
    }
    assert(f.starts == 3 && b.State() == EngineState::Failed);
    assert(f.notices.size() == 1);
    Hello(b, 1);
    b.OnClientLine(1, R"({"op":"retry"})");
    assert(f.starts == 4 && f.cleared == 1 && b.State() == EngineState::Starting);
    b.OnEngineExit(1);  // the one attempt fails before ready: stopped again
    assert(b.State() == EngineState::Failed && f.starts == 4 && f.notices.size() == 2);
    Advance(b, f, 60 * 60 * 1000);
    assert(f.starts == 4);
}

void BackoffWaits() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Ready(b);
    b.OnEngineExit(1);
    Advance(b, f, 0);
    assert(f.starts == 2);  // first restart at once
    Ready(b);
    b.OnEngineExit(1);
    Advance(b, f, 1999);
    assert(f.starts == 2);
    Advance(b, f, 1);
    assert(f.starts == 3);  // second after 2 s
}

void StableEngineForgetsFailures() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Ready(b);
    b.OnEngineExit(1);
    Advance(b, f, 0);
    Ready(b);
    Advance(b, f, ENGINE_STABLE_MS);
    assert(f.spawn_resets == 1);
    // Ten minutes later two more crashes are not "three in ten minutes".
    Advance(b, f, ENGINE_START_WINDOW_MS);
    b.OnEngineExit(1);
    Advance(b, f, 0);  // failures were reset: no wait
    assert(f.starts == 3);
    Ready(b);
    b.OnEngineExit(1);
    assert(b.State() == EngineState::Backoff);
}

void IncompatibleAndNotInstalled() {
    Fake f;
    Broker b(f, {});
    b.Start();
    b.OnClientConnected(1);
    b.OnClientLine(1, R"({"op":"hello","proto":2,"tag":"v9"})");
    assert(f.Client(1).back() == R"({"proto":1,"state":"incompatible"})");
    b.OnClientLine(1, R"({"seq":3,"op":"down","vk":1,"t":1})");
    assert(f.Client(1).back() == R"({"seq":3,"state":"incompatible"})");
    Ready(b);
    assert(f.to_engine.empty());  // never given a session

    Fake g;
    Broker::Options options;
    options.installed = false;
    Broker n(g, options);
    n.Start();
    assert(g.starts == 0);
    Hello(n, 1);
    assert(g.Client(1).back() == R"({"proto":1,"state":"not-installed","warnings":[]})");
    n.OnClientLine(1, R"({"seq":1,"op":"down","vk":1,"t":1})");
    assert(g.Client(1).back() == R"({"seq":1,"state":"not-installed"})");
}

void MaintenanceStopsEverything() {
    Fake f;
    f.maintenance = true;
    Broker b(f, {});
    b.Start();
    assert(f.starts == 0 && f.quit);
}

void WarningsOncePerCause() {
    Fake f;
    Broker b(f, {});
    b.Start();
    b.OnEngineLine(R"({"ready":true,"proto":1,"warnings":["config"]})");
    b.OnEngineLine(R"({"event":"warning","warning":"lm"})");
    b.OnEngineLine(R"({"event":"warning","warning":"lm"})");
    assert(f.notices.size() == 2 && f.notices[0].warning == "config" &&
           f.notices[1].warning == "lm");
    Hello(b, 2);
    assert(f.Client(2).back() == R"({"proto":1,"state":"ready","warnings":["config","lm"]})");
}

void RetryWhileRunningRestartsWithoutCounting() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Ready(b);
    Hello(b, 1);
    b.OnClientLine(1, R"({"op":"retry"})");  // settings changed (§9.4)
    assert(f.kills == 1);
    b.OnEngineExit(-9);
    assert(f.starts == 2 && b.State() == EngineState::Starting && f.notices.empty());
}

void PythonMissingIsAProgramFileStop() {
    Fake f;
    f.start_ok = false;
    Broker b(f, {});
    b.Start();
    Advance(b, f, 0);
    assert(b.State() == EngineState::Failed && b.LastStop() == Stop::ProgramFiles);
}

void ClosedClientClosesItsSession() {
    Fake f;
    Broker b(f, {});
    b.Start();
    Ready(b);
    Hello(b, 4);
    b.OnClientClosed(4);
    assert(f.to_engine.back() == R"({"op":"close","sid":4})");
}

void TestClockHook() {
    Fake f;
    Broker::Options options;
    options.test_hooks = true;
    Broker b(f, options);
    b.Start();
    b.OnEngineExit(1);
    Advance(b, f, 0);
    b.OnEngineExit(1);
    Hello(b, 1);
    int64_t before = b.Now();
    b.OnClientLine(1, R"({"op":"test_advance","ms":600000})");
    assert(b.Now() == before + 600000);
    b.OnTimer();
    assert(f.starts == 2);
}

}  // namespace

int main() {
    ImmediateCrashStartsExactlyTwice();
    FatalCodesNameTheReason();
    HeldKeysReachTheNextEngineInOrder();
    HeldKeysExpireAfterHoldMs();
    SilentEngineIsKilled();
    ThreeCrashesThenRetryStartsOnce();
    BackoffWaits();
    StableEngineForgetsFailures();
    IncompatibleAndNotInstalled();
    MaintenanceStopsEverything();
    WarningsOncePerCause();
    RetryWhileRunningRestartsWithoutCounting();
    PythonMissingIsAProgramFileStop();
    ClosedClientClosesItsSession();
    TestClockHook();
    std::puts("broker_core_test: all passed");
    return 0;
}
