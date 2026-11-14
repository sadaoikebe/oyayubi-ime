// Pieces both platforms share: bounded logs, engine.json, and the self-test.
#pragma once

#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

#include "broker_core.h"

namespace broker {

namespace fs = std::filesystem;

constexpr uintmax_t LOG_MAX_BYTES = 1024 * 1024;  // REDESIGN §11: 1MB x 2 generations

// What a platform event loop drives: the Broker, or the SelfTest.
class Handler {
public:
    virtual ~Handler() = default;
    virtual void OnClientConnected(ClientId) {}
    virtual void OnClientLine(ClientId, const std::string&) {}
    virtual void OnClientClosed(ClientId) {}
    virtual void OnEngineLine(const std::string& line) = 0;
    virtual void OnEngineStderrLine(const std::string& line) = 0;
    virtual void OnEngineExit(int code) = 0;
    virtual void OnTimer() = 0;
    virtual int64_t NextDeadline() const = 0;
};

class BrokerHandler : public Handler {
public:
    explicit BrokerHandler(Broker& broker) : broker_(broker) {}
    void OnClientConnected(ClientId c) override { broker_.OnClientConnected(c); }
    void OnClientLine(ClientId c, const std::string& l) override { broker_.OnClientLine(c, l); }
    void OnClientClosed(ClientId c) override { broker_.OnClientClosed(c); }
    void OnEngineLine(const std::string& l) override { broker_.OnEngineLine(l); }
    void OnEngineStderrLine(const std::string& l) override { broker_.OnEngineStderrLine(l); }
    void OnEngineExit(int code) override { broker_.OnEngineExit(code); }
    void OnTimer() override { broker_.OnTimer(); }
    int64_t NextDeadline() const override { return broker_.NextDeadline(); }

private:
    Broker& broker_;
};

// A log file of at most LOG_MAX_BYTES.
//  Rotating (broker.log): when full, it becomes <prev> and a new one starts.
//  Trimming (engine.log): Rotate() at each engine start moves the last run's
//  log to <prev>; when full, the oldest half of the current one is dropped.
class BoundedLog {
public:
    BoundedLog(fs::path path, fs::path prev, bool trim) : path_(path), prev_(prev), trim_(trim) {}
    void Write(const std::string& line);  // one line, '\n' added
    void Rotate();

private:
    void Limit();
    fs::path path_, prev_;
    bool trim_;
};

std::string Timestamp();  // local time, ISO 8601 with milliseconds

// logs/notices.log: one JSON line per notice the user was given, with its time
// ({"time":..,"stopped":"hang"} or {"time":..,"warning":"config"}). The
// management window's status tab reads the last stop from here (§9.4).
void RecordNotice(const fs::path& logs, const Notice& notice);

// engine.json (REDESIGN §4.2): written by the installer next to the version.
//  {"format":1, "python":"<abs python.exe>", "pythonw":"<abs pythonw.exe>",
//   "app":"<abs app dir: the engine's working directory>",
//   "args":["-m","yuragi.engine", ...], "admin_args":["-m","yuragi.ime.admin_ui"]}
struct EngineConfig {
    std::string python, pythonw, app;  // UTF-8
    std::vector<std::string> args, admin_args;
};
bool ReadEngineConfig(const fs::path& path, EngineConfig& config, std::string& error);

// --selftest (REDESIGN §8.2 step 3): start the engine for real, convert once,
// check that no window appeared, end it. Exit codes below.
enum SelfTestResult {
    SELFTEST_OK = 0,
    SELFTEST_NOT_READY = 1,  // exited or fell silent before ready
    SELFTEST_NOT_CONVERTED = 2,
    SELFTEST_WINDOWS = 3,  // a window appeared
    SELFTEST_NO_REPLY = 4,
    SELFTEST_CANNOT_START = 5,
};

class SelfTestPlatform : public Platform {
public:
    virtual void CloseEngineInput() = 0;
    virtual int CountEngineWindows() = 0;  // -1: this platform cannot count them
    virtual void Finish(int code) = 0;
};

class SelfTest : public Handler {
public:
    explicit SelfTest(SelfTestPlatform& platform) : platform_(platform) {}
    void Start();
    void OnEngineLine(const std::string& line) override;
    void OnEngineStderrLine(const std::string& line) override;
    void OnEngineExit(int code) override;
    void OnTimer() override;
    int64_t NextDeadline() const override;

private:
    void Send(const std::string& op, int vk, int t);
    void Done(int code, const std::string& why);
    SelfTestPlatform& platform_;
    enum { Starting, Converting, Ending, Finished } phase_ = Starting;
    int64_t deadline_ = 0;
    int sent_ = 0, answered_ = 0;
    bool converted_ = false;
    int result_ = SELFTEST_OK;
    std::string last_fatal_;
};

}  // namespace broker
