#include "broker_core.h"

#include <algorithm>

#include "../tip/tip_json.h"

namespace broker {

using tip_json::Value;

const char* StopName(Stop stop) {
    switch (stop) {
        case Stop::None: return "none";
        case Stop::ProgramFiles: return "program-files";
        case Stop::Dictionary: return "dictionary";
        case Stop::Cache: return "cache";
        case Stop::Repeated: return "repeated";
        case Stop::Hang: return "hang";
    }
    return "?";
}

namespace {

bool ParseObject(const std::string& line, Value& value) {
    return tip_json::Parser(line).Parse(value) && value.kind == Value::Object;
}

// {"sid":N, ...the rest of the TIP's object...}
std::string WithSid(ClientId sid, const std::string& object) {
    size_t open = object.find('{');
    size_t body = object.find_first_not_of(" \t\r\n", open + 1);
    std::string head = "{\"sid\":" + std::to_string(sid);
    if (body == std::string::npos || object[body] == '}') return head + "}";
    return head + "," + object.substr(open + 1);
}

std::string SeqState(long long seq, const char* state) {
    return "{\"seq\":" + std::to_string(seq) + ",\"state\":\"" + state + "\"}";
}

Stop StopForExit(int code) {
    switch (code) {
        case 20: return Stop::ProgramFiles;
        case 21: return Stop::Dictionary;
        case 22: return Stop::Cache;
        default: return Stop::None;
    }
}

}  // namespace

Broker::Broker(Platform& platform, Options options) : platform_(platform), options_(options) {
    if (!options_.installed) state_ = EngineState::NotInstalled;
}

int64_t Broker::Now() const { return platform_.Now() + clock_offset_; }

const char* Broker::ClientState() const {
    switch (state_) {
        case EngineState::NotInstalled: return "not-installed";
        case EngineState::Ready: return "ready";
        case EngineState::Failed: return "down";
        default: return "starting";
    }
}

void Broker::Start() {
    if (state_ == EngineState::NotInstalled) {
        Log("no engine.json for this user: not installed; nothing is started");
        return;
    }
    StartEngine();
}

void Broker::SetState(EngineState state) {
    const char* before = ClientState();
    state_ = state;
    const char* after = ClientState();
    if (std::string(before) != after)
        Broadcast(std::string("{\"event\":\"state\",\"state\":\"") + after + "\"}");
}

void Broker::Broadcast(const std::string& line) {
    for (auto& [id, client] : clients_)
        if (client.hello && !client.incompatible) platform_.WriteClient(id, line);
}

void Broker::StartEngine() {
    if (platform_.MaintenanceActive()) {
        // §5.2/§8.1: the installer is working; it asks us to quit, or we leave.
        Log("maintenance in progress: not starting the engine; exiting");
        Broadcast("{\"event\":\"state\",\"state\":\"maintenance\"}");
        platform_.Quit();
        return;
    }
    int64_t now = Now();
    start_times_.push_back(now);
    ++starts_total_;
    killing_ = false;
    kill_reason_ = Stop::None;
    last_output_ = now;
    backoff_until_ = NEVER;
    for (auto& [id, client] : clients_) client.session = false;
    SetState(EngineState::Starting);
    Log("starting the engine (start " + std::to_string(starts_total_) + ", " +
        std::to_string(start_times_.size()) + " in the last 10 minutes)");
    std::string error;
    if (!platform_.StartEngine(error)) {
        Log("the engine could not be started: " + error);
        killing_ = true;
        kill_reason_ = Stop::ProgramFiles;  // python.exe or engine.json unusable
        EngineGone(-1);
    }
}

void Broker::OnClientConnected(ClientId id) { clients_[id] = Client(); }

void Broker::OnClientClosed(ClientId id) {
    auto it = clients_.find(id);
    if (it == clients_.end()) return;
    if (it->second.session && state_ == EngineState::Ready)
        platform_.WriteEngine("{\"op\":\"close\",\"sid\":" + std::to_string(id) + "}");
    clients_.erase(it);
}

void Broker::OnClientLine(ClientId id, const std::string& line) {
    auto it = clients_.find(id);
    if (it == clients_.end()) return;
    Client& client = it->second;
    Value msg;
    if (!ParseObject(line, msg)) {
        Log("unreadable message from a TIP; ignored");
        return;
    }
    const std::string op = msg.Get("op").text;
    long long seq = 0;
    bool has_seq = msg.Get("seq").Int(seq);
    if (!client.hello) {
        long long proto = 0;
        if (op != "hello" || !msg.Get("proto").Int(proto) || proto != PROTO) {
            // §5.5: a TIP of another protocol only ever hears "incompatible".
            client.hello = client.incompatible = true;
            platform_.WriteClient(id, "{\"proto\":1,\"state\":\"incompatible\"}");
            Log("a TIP of another protocol connected (" + msg.Get("tag").text + ")");
            return;
        }
        client.hello = true;
        std::string reply = std::string("{\"proto\":1,\"state\":\"") + ClientState() +
                            "\",\"warnings\":[";
        for (size_t i = 0; i < warnings_.size(); ++i)
            reply += (i ? "," : "") + tip_json::Quote(warnings_[i]);
        platform_.WriteClient(id, reply + "]}");
        if (state_ == EngineState::Ready) OpenSession(id, client, false);
        return;
    }
    if (client.incompatible) {
        if (has_seq) platform_.WriteClient(id, SeqState(seq, "incompatible"));
        return;
    }
    if (op == "hello") return;
    if (op == "retry") {
        Retry();
        return;
    }
    if (op == "open_admin") {
        Log("opening the management window");
        platform_.OpenAdmin();
        return;
    }
    if (op == "quit") {
        Log("asked to quit (installer)");
        platform_.Quit();
        return;
    }
    if (op == "test_advance" && options_.test_hooks) {
        long long ms = 0;
        if (msg.Get("ms").Int(ms)) clock_offset_ += ms;
        return;
    }
    if (!has_seq) {
        Log("a request without seq; ignored");
        return;
    }
    switch (state_) {
        case EngineState::Ready: Forward(id, client, seq, line); break;
        case EngineState::Starting:
        case EngineState::Backoff:
        case EngineState::Stopped: Hold(id, client, seq, line); break;
        case EngineState::Failed: platform_.WriteClient(id, SeqState(seq, "down")); break;
        case EngineState::NotInstalled:
            platform_.WriteClient(id, SeqState(seq, "not-installed"));
            break;
    }
}

void Broker::OpenSession(ClientId id, Client& client, bool restore) {
    platform_.WriteEngine("{\"op\":\"open\",\"sid\":" + std::to_string(id) + "}");
    client.session = true;
    if (restore && client.direct) {
        // §5.6: the new session starts in the input mode the old one had.
        long long seq = --internal_seq_;
        in_flight_.push_back({id, seq, Now()});
        platform_.WriteEngine("{\"sid\":" + std::to_string(id) + ",\"seq\":" +
                              std::to_string(seq) + ",\"op\":\"set_direct\",\"direct\":true}");
    }
}

void Broker::Forward(ClientId id, Client&, long long seq, const std::string& line) {
    in_flight_.push_back({id, seq, Now()});
    platform_.WriteEngine(WithSid(id, line));
}

void Broker::Hold(ClientId id, Client& client, long long seq, const std::string& line) {
    client.held.push_back({seq, line, Now()});
    if (client.held.size() > HOLD_MAX) {
        Log("more than " + std::to_string(HOLD_MAX) + " keys waiting for the engine; "
            "they were not processed");
        DropHeld(id, client, "down");
    }
}

void Broker::DropHeld(ClientId id, Client& client, const char* state) {
    for (const Held& held : client.held) platform_.WriteClient(id, SeqState(held.seq, state));
    client.held.clear();
}

void Broker::OnEngineLine(const std::string& line) {
    last_output_ = Now();
    Value msg;
    if (!ParseObject(line, msg)) {
        Log("unreadable line from the engine; ignored");
        return;
    }
    if (msg.Get("progress").kind != Value::Null) return;  // still starting, not silent
    if (msg.Get("ready").kind == Value::Boolean && msg.Get("ready").boolean) {
        if (state_ != EngineState::Starting) return;
        bool replaced = ever_ready_;
        ever_ready_ = true;
        ready_since_ = Now();
        stable_ = false;
        early_exits_ = 0;
        retry_attempt_ = false;
        Log("engine ready");
        for (const Value& w : msg.Get("warnings").array) Warn(w.text);
        if (replaced) Broadcast("{\"event\":\"reset\"}");  // §5.6: composition was lost
        state_ = EngineState::Ready;
        for (auto& [id, client] : clients_) {
            if (!client.hello || client.incompatible) continue;
            OpenSession(id, client, replaced);
            for (const Held& held : client.held) Forward(id, client, held.seq, held.line);
            client.held.clear();
        }
        Broadcast("{\"event\":\"state\",\"state\":\"ready\"}");
        return;
    }
    if (msg.Get("event").text == "warning") {
        Warn(msg.Get("warning").text);
        return;
    }
    long long sid = 0, seq = 0;
    if (!msg.Get("sid").Int(sid) || !msg.Get("seq").Int(seq)) {
        Log("a line from the engine without sid/seq; ignored");
        return;
    }
    // The engine answers in the order it was asked (one request at a time).
    if (!in_flight_.empty() && in_flight_.front().client == (ClientId)sid &&
        in_flight_.front().seq == seq) {
        in_flight_.pop_front();
    } else {
        Log("a reply the broker was not waiting for (sid " + std::to_string(sid) + ")");
        auto it = std::find_if(in_flight_.begin(), in_flight_.end(), [&](const InFlight& f) {
            return f.client == (ClientId)sid && f.seq == seq;
        });
        if (it != in_flight_.end()) in_flight_.erase(it);
    }
    auto client = clients_.find((ClientId)sid);
    if (client == clients_.end()) return;  // the TIP went away meanwhile
    if (msg.Get("direct").kind == Value::Boolean) client->second.direct = msg.Get("direct").boolean;
    if (seq >= 0) platform_.WriteClient((ClientId)sid, line);
}

void Broker::OnEngineStderrLine(const std::string& line) {
    last_output_ = Now();
    if (line.compare(0, 6, "fatal ") == 0) last_fatal_ = line;
}

void Broker::Warn(const std::string& code) {
    if (code.empty() || std::find(warnings_.begin(), warnings_.end(), code) != warnings_.end())
        return;
    warnings_.push_back(code);
    Log("warning: " + code);
    Notice notice;
    notice.warning = code;
    platform_.Notify(notice);  // §9.3: once per cause
}

void Broker::Kill(Stop reason) {
    if (killing_) return;
    killing_ = true;
    kill_reason_ = reason;
    platform_.KillEngine();
}

void Broker::OnEngineExit(int code) { EngineGone(code); }

void Broker::EngineGone(int code) {
    bool was_ready = state_ == EngineState::Ready;
    Log("engine exited with code " + std::to_string(code) + (was_ready ? "" : " before ready"));
    for (const InFlight& f : in_flight_) {
        // The key that was being processed is lost with the engine (§5.6).
        if (f.seq >= 0 && clients_.count(f.client))
            platform_.WriteClient(f.client, SeqState(f.seq, "down"));
    }
    in_flight_.clear();
    for (auto& [id, client] : clients_) client.session = false;
    if (restarting_) {
        restarting_ = false;
        StartEngine();
        return;
    }
    Stop reason = StopForExit(code);
    if (reason == Stop::None) reason = killing_ ? kill_reason_ : Stop::Repeated;
    if (reason == Stop::None) reason = Stop::Repeated;
    last_stop_ = reason;
    ++failures_;
    if (!was_ready) ++early_exits_;
    int64_t now = Now();
    while (!start_times_.empty() && now - start_times_.front() >= ENGINE_START_WINDOW_MS)
        start_times_.pop_front();
    if (retry_attempt_ && !was_ready) {
        Fail(reason);  // the user's one attempt did not get the engine running
    } else if (early_exits_ >= 2) {
        Fail(reason);  // §5.4: failing at startup fails the same way every time
    } else if ((int)start_times_.size() >= ENGINE_MAX_STARTS) {
        Fail(reason);
    } else {
        int64_t wait = BACKOFF_MS[std::min(failures_, 3) - 1];
        backoff_until_ = now + wait;
        Log("starting again in " + std::to_string(wait) + " ms");
        SetState(EngineState::Backoff);
    }
}

void Broker::Fail(Stop reason) {
    last_stop_ = reason;
    retry_attempt_ = false;
    backoff_until_ = NEVER;
    Log(std::string("engine given up: ") + StopName(reason) +
        (last_fatal_.empty() ? "" : " (" + last_fatal_ + ")"));
    for (auto& [id, client] : clients_) DropHeld(id, client, "down");
    SetState(EngineState::Failed);
    Notice notice;
    notice.stopped = true;
    notice.reason = reason;
    platform_.Notify(notice);  // §9.3: once; nothing starts by itself after this
}

void Broker::Retry() {
    if (state_ == EngineState::NotInstalled) return;
    Log("retry requested");
    start_times_.clear();
    failures_ = 0;
    early_exits_ = 0;
    last_fatal_.clear();
    switch (state_) {
        case EngineState::Failed:
            retry_attempt_ = true;
            platform_.ClearStopped();
            StartEngine();
            break;
        case EngineState::Backoff:
        case EngineState::Stopped:
            StartEngine();
            break;
        case EngineState::Ready:
        case EngineState::Starting:
            restarting_ = true;  // e.g. new settings: a fresh engine reads them
            Kill(Stop::None);
            break;
        case EngineState::NotInstalled:
            break;
    }
}

void Broker::OnTimer() {
    int64_t now = Now();
    if (state_ == EngineState::Backoff && now >= backoff_until_) {
        StartEngine();
        now = Now();
    }
    if (state_ == EngineState::Starting && !killing_ &&
        now - last_output_ >= ENGINE_START_SILENCE_MS) {
        Log("the engine said nothing for " + std::to_string(ENGINE_START_SILENCE_MS) +
            " ms while starting; ending it");
        Kill(Stop::Hang);
    }
    if (state_ == EngineState::Ready && !killing_ && !in_flight_.empty() &&
        now - in_flight_.front().sent >= ENGINE_HANG_MS) {
        Log("no reply from the engine for " + std::to_string(ENGINE_HANG_MS) + " ms; ending it");
        Kill(Stop::Hang);
    }
    if (state_ == EngineState::Ready && !stable_ && now - ready_since_ >= ENGINE_STABLE_MS) {
        stable_ = true;
        failures_ = 0;
        platform_.ResetSpawnCount();
    }
    for (auto& [id, client] : clients_) {
        if (!client.held.empty() && now - client.held.front().at >= HOLD_MS) {
            Log("keys waited " + std::to_string(HOLD_MS) + " ms for the engine; "
                "they were not processed");
            DropHeld(id, client, "down");
        }
    }
}

int64_t Broker::NextDeadline() const {
    int64_t next = NEVER;
    if (state_ == EngineState::Backoff) next = std::min(next, backoff_until_);
    if (state_ == EngineState::Starting && !killing_)
        next = std::min(next, last_output_ + ENGINE_START_SILENCE_MS);
    if (state_ == EngineState::Ready && !killing_ && !in_flight_.empty())
        next = std::min(next, in_flight_.front().sent + ENGINE_HANG_MS);
    if (state_ == EngineState::Ready && !stable_)
        next = std::min(next, ready_since_ + ENGINE_STABLE_MS);
    for (const auto& [id, client] : clients_)
        if (!client.held.empty()) next = std::min(next, client.held.front().at + HOLD_MS);
    if (next == NEVER) return NEVER;
    return next - clock_offset_;  // in the platform's clock
}

}  // namespace broker
