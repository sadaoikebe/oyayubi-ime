// The broker's decisions (REDESIGN §5), independent of the operating system.
//
// One Broker lives in one thread. The platform layer (platform_win.cpp,
// platform_posix.cpp) owns the pipes, the engine process and the clock; it
// calls the On* methods as things happen and OnTimer() when NextDeadline()
// has passed, and the Broker answers through the Platform interface.
#pragma once

#include <cstdint>
#include <deque>
#include <map>
#include <set>
#include <string>
#include <vector>

namespace broker {

// REDESIGN §11.
constexpr int ENGINE_MAX_STARTS = 3;
constexpr int64_t ENGINE_START_WINDOW_MS = 600000;
constexpr int64_t ENGINE_STABLE_MS = 60000;
constexpr int64_t ENGINE_HANG_MS = 5000;
constexpr int64_t ENGINE_START_SILENCE_MS = 30000;
constexpr int64_t BACKOFF_MS[] = {0, 2000, 10000};
constexpr int64_t HOLD_MS = 8000;
constexpr size_t HOLD_MAX = 256;
constexpr int PROTO = 1;
constexpr int64_t NEVER = INT64_MAX;

using ClientId = uint64_t;

// Why the engine was given up (§6.6, §9.3).
enum class Stop { None, ProgramFiles, Dictionary, Cache, Repeated, Hang };
const char* StopName(Stop stop);

enum class EngineState { NotInstalled, Stopped, Starting, Ready, Backoff, Failed };

// What the user is told, once (§9.3): a stop, or a warning by its code.
struct Notice {
    bool stopped = false;
    Stop reason = Stop::None;
    std::string warning;  // config, lm, learning-read, learning-write
};

class Platform {
public:
    virtual ~Platform() = default;
    virtual int64_t Now() = 0;  // monotonic milliseconds
    // Start the engine; false when it could not even be started (the exit
    // is then reported by the Broker itself, not by the platform).
    virtual bool StartEngine(std::string& error) = 0;
    virtual void WriteEngine(const std::string& line) = 0;  // without '\n'
    // Forcibly end it; OnEngineExit follows once its output has been read.
    virtual void KillEngine() = 0;
    virtual void WriteClient(ClientId client, const std::string& line) = 0;
    virtual void Notify(const Notice& notice) = 0;
    virtual void ClearStopped() = 0;  // the stop the user was told about is over
    virtual void OpenAdmin() = 0;
    virtual void ResetSpawnCount() = 0;  // §7.3 condition 4
    virtual bool MaintenanceActive() = 0;  // §8.1
    virtual void Log(const std::string& line) = 0;
    virtual void Quit() = 0;
};

class Broker {
public:
    struct Options {
        bool installed = true;  // engine.json present (§5.2)
        bool test_hooks = false;  // accept {"op":"test_advance","ms":N}
    };
    Broker(Platform& platform, Options options);

    void Start();
    void OnClientConnected(ClientId client);
    void OnClientLine(ClientId client, const std::string& line);
    void OnClientClosed(ClientId client);
    void OnEngineLine(const std::string& line);
    void OnEngineStderrLine(const std::string& line);
    void OnEngineExit(int code);
    void OnTimer();
    int64_t NextDeadline() const;
    void Retry();  // the user asked: "もう一度試す", or a settings restart (§9.4)

    // For the platform and the tests.
    int64_t Now() const;
    EngineState State() const { return state_; }
    const char* ClientState() const;  // ready, starting, down, not-installed
    Stop LastStop() const { return last_stop_; }
    int Starts() const { return starts_total_; }

private:
    struct Held {
        long long seq;
        std::string line;
        int64_t at;
    };
    struct Client {
        bool hello = false;
        bool incompatible = false;
        bool session = false;  // opened in the current engine
        bool direct = false;  // input mode, from the engine's last reply
        std::deque<Held> held;
    };
    struct InFlight {
        ClientId client;
        long long seq;  // negative: the broker's own request (no TIP waits for it)
        int64_t sent;
    };

    void StartEngine();
    void EngineGone(int code);
    void Fail(Stop reason);
    void SetState(EngineState state);
    void Broadcast(const std::string& line);
    void OpenSession(ClientId id, Client& client, bool restore);
    void Forward(ClientId id, Client& client, long long seq, const std::string& line);
    void Hold(ClientId id, Client& client, long long seq, const std::string& line);
    void DropHeld(ClientId id, Client& client, const char* state);
    void Kill(Stop reason);
    void Warn(const std::string& code);
    void Log(const std::string& line) { platform_.Log(line); }

    Platform& platform_;
    Options options_;
    int64_t clock_offset_ = 0;
    EngineState state_ = EngineState::Stopped;
    std::map<ClientId, Client> clients_;
    std::deque<InFlight> in_flight_;
    std::deque<int64_t> start_times_;  // within ENGINE_START_WINDOW_MS
    int starts_total_ = 0;
    int failures_ = 0;  // since the engine was last stable for ENGINE_STABLE_MS
    int early_exits_ = 0;  // consecutive exits before ready
    bool retry_attempt_ = false;  // started by the user from FAILED: one attempt
    bool restarting_ = false;  // killed on purpose to start again (Retry while running)
    bool killing_ = false;
    Stop kill_reason_ = Stop::None;
    bool ever_ready_ = false;  // an engine was ready before: the next one replaces it
    int64_t backoff_until_ = NEVER;
    int64_t last_output_ = 0;
    int64_t ready_since_ = 0;
    bool stable_ = false;
    long long internal_seq_ = 0;
    Stop last_stop_ = Stop::None;
    std::string last_fatal_;  // the engine's last "fatal ..." stderr line
    std::vector<std::string> warnings_;
};

}  // namespace broker
