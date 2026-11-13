#include "support.h"

#include <chrono>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <iterator>
#include <system_error>

#include "../tip/tip_json.h"

namespace broker {

std::string Timestamp() {
    using namespace std::chrono;
    auto now = system_clock::now();
    std::time_t t = system_clock::to_time_t(now);
    std::tm tm{};
#ifdef _WIN32
    localtime_s(&tm, &t);
#else
    localtime_r(&t, &tm);
#endif
    char buf[40];
    std::strftime(buf, sizeof buf, "%Y-%m-%dT%H:%M:%S", &tm);
    long ms = (long)(duration_cast<milliseconds>(now.time_since_epoch()).count() % 1000);
    char out[48];
    std::snprintf(out, sizeof out, "%s.%03ld", buf, ms);
    return out;
}

void BoundedLog::Write(const std::string& line) {
    {
        std::ofstream out(path_, std::ios::binary | std::ios::app);
        if (!out) return;  // no log directory: nothing else depends on the log
        out << line << '\n';
    }
    Limit();
}

void BoundedLog::Rotate() {
    std::error_code ec;
    if (fs::exists(path_, ec)) fs::rename(path_, prev_, ec);
}

void BoundedLog::Limit() {
    std::error_code ec;
    uintmax_t size = fs::file_size(path_, ec);
    if (ec || size <= LOG_MAX_BYTES) return;
    if (!trim_) {
        fs::rename(path_, prev_, ec);
        return;
    }
    // Keep the newest half, from a line start.
    std::string data;
    {
        std::ifstream in(path_, std::ios::binary);
        data.assign(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
    }
    size_t from = data.size() - LOG_MAX_BYTES / 2;
    size_t nl = data.find('\n', from);
    from = nl == std::string::npos ? data.size() : nl + 1;
    std::ofstream out(path_, std::ios::binary | std::ios::trunc);
    out << "[earlier lines dropped: the log is kept under 1 MB]\n";
    out.write(data.data() + from, (std::streamsize)(data.size() - from));
}

void RecordNotice(const fs::path& logs, const Notice& notice) {
    std::string what = notice.stopped
        ? std::string("\"stopped\":\"") + StopName(notice.reason) + "\""
        : "\"warning\":" + tip_json::Quote(notice.warning);
    BoundedLog(logs / "notices.log", logs / "notices.prev.log", false)
        .Write("{\"time\":\"" + Timestamp() + "\"," + what + "}");
}

bool ReadEngineConfig(const fs::path& path, EngineConfig& config, std::string& error) {
    std::ifstream in(path, std::ios::binary);
    if (!in) {
        error = "cannot read " + path.u8string();
        return false;
    }
    std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (text.compare(0, 3, "\xEF\xBB\xBF") == 0) text.erase(0, 3);  // PowerShell may add a BOM
    tip_json::Value v;
    if (!tip_json::Parser(text).Parse(v) || v.kind != tip_json::Value::Object) {
        error = "engine.json is not a JSON object";
        return false;
    }
    config.python = v.Get("python").text;
    config.pythonw = v.Get("pythonw").text;
    config.app = v.Get("app").text;
    config.args.clear();
    for (const auto& a : v.Get("args").array) config.args.push_back(a.text);
    config.admin_args.clear();
    for (const auto& a : v.Get("admin_args").array) config.admin_args.push_back(a.text);
    if (config.python.empty() || config.app.empty() || config.args.empty()) {
        error = "engine.json lacks python, app or args";
        return false;
    }
    return true;
}

// -- self-test -----------------------------------------------------------------

namespace {
constexpr int64_t SELFTEST_REPLY_MS = ENGINE_HANG_MS;
constexpr int64_t SELFTEST_EXIT_MS = 10000;
// かいはつ on the NICOLA-A plain layer, then Space: one conversion.
constexpr int SELFTEST_KEYS[] = {0x57, 0x4C, 0x48, 0x4F, 0x20};
}  // namespace

void SelfTest::Start() {
    std::string error;
    deadline_ = platform_.Now() + ENGINE_START_SILENCE_MS;
    if (!platform_.StartEngine(error)) {
        phase_ = Finished;
        platform_.Log("selftest: the engine could not be started: " + error);
        platform_.Finish(SELFTEST_CANNOT_START);
    }
}

void SelfTest::Send(const std::string& op, int vk, int t) {
    ++sent_;
    platform_.WriteEngine("{\"sid\":1,\"seq\":" + std::to_string(sent_) + ",\"op\":\"" + op +
                          "\",\"vk\":" + std::to_string(vk) + ",\"t\":" + std::to_string(t) + "}");
}

void SelfTest::OnEngineLine(const std::string& line) {
    tip_json::Value v;
    if (!tip_json::Parser(line).Parse(v) || v.kind != tip_json::Value::Object) return;
    if (phase_ == Starting) {
        if (v.Get("progress").kind != tip_json::Value::Null) {
            deadline_ = platform_.Now() + ENGINE_START_SILENCE_MS;
            return;
        }
        if (v.Get("ready").boolean) {
            platform_.Log("selftest: engine ready");
            phase_ = Converting;
            platform_.WriteEngine("{\"op\":\"open\",\"sid\":1}");
            int t = 0;
            for (int vk : SELFTEST_KEYS) {
                Send("down", vk, t);
                Send("up", vk, t + 20);
                t += 300;
            }
            deadline_ = platform_.Now() + SELFTEST_REPLY_MS;
        }
        return;
    }
    if (phase_ != Converting || v.Get("seq").kind == tip_json::Value::Null) return;
    ++answered_;
    deadline_ = platform_.Now() + SELFTEST_REPLY_MS;
    if (v.Get("converted").boolean) converted_ = true;
    if (answered_ < sent_) return;
    int windows = platform_.CountEngineWindows();
    if (!converted_) {
        Done(SELFTEST_NOT_CONVERTED, "the conversion did not convert");
    } else if (windows > 0) {
        Done(SELFTEST_WINDOWS, std::to_string(windows) + " window(s) appeared");
    } else {
        Done(SELFTEST_OK, windows < 0 ? "converted (windows not counted on this platform)"
                                      : "converted, no window appeared");
    }
}

void SelfTest::OnEngineStderrLine(const std::string& line) {
    if (line.compare(0, 6, "fatal ") == 0) last_fatal_ = line;
    if (phase_ == Starting) deadline_ = platform_.Now() + ENGINE_START_SILENCE_MS;
}

void SelfTest::Done(int code, const std::string& why) {
    result_ = code;
    platform_.Log("selftest: " + why);
    phase_ = Ending;
    deadline_ = platform_.Now() + SELFTEST_EXIT_MS;
    platform_.CloseEngineInput();
}

void SelfTest::OnEngineExit(int code) {
    if (phase_ == Finished) return;
    if (phase_ == Starting || phase_ == Converting) {
        result_ = phase_ == Starting ? SELFTEST_NOT_READY : SELFTEST_NO_REPLY;
        platform_.Log("selftest: the engine exited with code " + std::to_string(code) +
                      (last_fatal_.empty() ? "" : " (" + last_fatal_ + ")"));
    }
    phase_ = Finished;
    platform_.Finish(result_);
}

void SelfTest::OnTimer() {
    if (platform_.Now() < deadline_ || phase_ == Finished) return;
    if (phase_ == Ending) {
        platform_.Log("selftest: the engine did not exit; ending it");
        platform_.KillEngine();
        deadline_ = NEVER;
        return;
    }
    result_ = phase_ == Starting ? SELFTEST_NOT_READY : SELFTEST_NO_REPLY;
    platform_.Log(phase_ == Starting ? "selftest: the engine fell silent before ready"
                                     : "selftest: no reply from the engine");
    phase_ = Ending;
    deadline_ = NEVER;
    platform_.KillEngine();
}

int64_t SelfTest::NextDeadline() const { return phase_ == Finished ? NEVER : deadline_; }

}  // namespace broker
