// The broker on POSIX, for testing the same Broker as on Windows (REDESIGN §5.7).
//
// The TIP's named pipe is a Unix domain socket here, the job object is
// PR_SET_PDEATHSIG, the notification is a line in logs/notices.log, and the
// maintenance mutex is a lock held on <data-dir>/maintenance.lock.
//
//   oyayubi-broker --engine-json PATH [--data-dir DIR] [--socket PATH] [--test-hooks]
//   oyayubi-broker --selftest PATH [--data-dir DIR]
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/prctl.h>
#endif

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "broker_core.h"
#include "support.h"

extern char** environ;

namespace broker {
namespace {

int64_t MonotonicMs() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

void SetNonBlocking(int fd) { fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK); }

// Lines out of a byte stream; returns false at end of stream.
bool ReadLines(int fd, std::string& buffer, std::vector<std::string>& lines) {
    char chunk[65536];
    for (;;) {
        ssize_t n = read(fd, chunk, sizeof chunk);
        if (n > 0) {
            buffer.append(chunk, (size_t)n);
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) break;
        size_t start = 0;
        for (size_t nl; (nl = buffer.find('\n', start)) != std::string::npos; start = nl + 1)
            lines.push_back(buffer.substr(start, nl - start));
        buffer.erase(0, start);
        if (!buffer.empty()) lines.push_back(buffer);  // a last line without '\n'
        buffer.clear();
        return false;
    }
    size_t start = 0;
    for (size_t nl; (nl = buffer.find('\n', start)) != std::string::npos; start = nl + 1) {
        std::string line = buffer.substr(start, nl - start);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        lines.push_back(line);
    }
    buffer.erase(0, start);
    return true;
}

class PosixPlatform : public SelfTestPlatform {
public:
    PosixPlatform(fs::path data_dir, EngineConfig config, bool have_config)
        : data_dir_(data_dir), config_(config), have_config_(have_config),
          broker_log_(data_dir / "logs" / "broker.log", data_dir / "logs" / "broker.prev.log", false),
          engine_log_(data_dir / "logs" / "engine.log", data_dir / "logs" / "engine.prev.log", true) {
        std::error_code ec;
        fs::create_directories(data_dir / "logs", ec);
    }

    void SetHandler(Handler* handler) { handler_ = handler; }

    // -- Platform --------------------------------------------------------------
    int64_t Now() override { return MonotonicMs(); }

    bool StartEngine(std::string& error) override {
        if (!have_config_) {
            error = "no engine.json";
            return false;
        }
        int in[2], out[2], err[2];
        if (pipe(in) || pipe(out) || pipe(err)) {
            error = std::string("pipe: ") + strerror(errno);
            return false;
        }
        std::vector<std::string> env_strings;
        for (char** e = environ; *e; ++e) {
            std::string kv = *e;
            std::string key = kv.substr(0, kv.find('='));
            if (key == "PYTHONPATH" || key == "PYTHONHOME" || key == "PYTHONUTF8" ||
                key == "PYTHONIOENCODING" || key == "PYTHONNOUSERSITE" || key == "HF_HUB_OFFLINE")
                continue;
            env_strings.push_back(kv);
        }
        for (const char* add : {"PYTHONUTF8=1", "PYTHONIOENCODING=utf-8", "PYTHONNOUSERSITE=1",
                                "HF_HUB_OFFLINE=1"})
            env_strings.push_back(add);
        std::vector<char*> envp;
        for (auto& s : env_strings) envp.push_back(s.data());
        envp.push_back(nullptr);
        std::vector<std::string> argv_strings{config_.python};
        argv_strings.insert(argv_strings.end(), config_.args.begin(), config_.args.end());
        std::vector<char*> argv;
        for (auto& s : argv_strings) argv.push_back(s.data());
        argv.push_back(nullptr);
        engine_log_.Rotate();
        pid_t parent = getpid();
        pid_t pid = fork();
        if (pid < 0) {
            error = std::string("fork: ") + strerror(errno);
            return false;
        }
        if (pid == 0) {
#ifdef __linux__
            prctl(PR_SET_PDEATHSIG, SIGKILL);  // the broker's death ends the engine (the job)
            if (getppid() != parent) _exit(127);
#endif
            dup2(in[0], 0);
            dup2(out[1], 1);
            dup2(err[1], 2);
            for (int fd : {in[0], in[1], out[0], out[1], err[0], err[1]}) close(fd);
            if (chdir(config_.app.c_str()) != 0) _exit(127);
            execve(argv[0], argv.data(), envp.data());
            _exit(127);
        }
        close(in[0]);
        close(out[1]);
        close(err[1]);
        pid_ = pid;
        pidfd_ = (int)syscall(SYS_pidfd_open, pid, 0);
        in_fd_ = in[1];
        out_fd_ = out[0];
        err_fd_ = err[0];
        SetNonBlocking(in_fd_);
        SetNonBlocking(out_fd_);
        SetNonBlocking(err_fd_);
        exited_ = false;
        out_buf_.clear();
        err_buf_.clear();
        in_queue_.clear();
        return true;
    }

    void WriteEngine(const std::string& line) override {
        if (in_fd_ < 0) return;
        in_queue_ += line + "\n";
        FlushEngineInput();
    }

    void KillEngine() override {
        if (pid_ > 0 && !exited_) kill(pid_, SIGKILL);
    }

    void CloseEngineInput() override {
        if (in_fd_ >= 0) close(in_fd_);
        in_fd_ = -1;
    }

    int CountEngineWindows() override { return -1; }

    void WriteClient(ClientId id, const std::string& line) override {
        auto it = clients_.find(id);
        if (it == clients_.end()) return;
        it->second.out += line + "\n";
        FlushClient(it->second);
    }

    void Notify(const Notice& notice) override {
        // No notification area here: the record is the notice (support.h).
        RecordNotice(data_dir_ / "logs", notice);
    }
    void ClearStopped() override { Log("notice cleared"); }

    void OpenAdmin() override {
        if (config_.pythonw.empty()) return;
        pid_t pid = fork();
        if (pid == 0) {
            setsid();
            if (chdir(config_.app.c_str()) != 0) _exit(127);
            std::vector<std::string> args{config_.pythonw};
            args.insert(args.end(), config_.admin_args.begin(), config_.admin_args.end());
            std::vector<char*> argv;
            for (auto& s : args) argv.push_back(s.data());
            argv.push_back(nullptr);
            execv(argv[0], argv.data());
            _exit(127);
        }
        admin_pids_.push_back(pid);
    }

    void ResetSpawnCount() override { Log("engine stable: TIP spawn count reset"); }

    bool MaintenanceActive() override {
        int fd = open((data_dir_ / "maintenance.lock").c_str(), O_RDONLY | O_CLOEXEC);
        if (fd < 0) return false;
        bool held = flock(fd, LOCK_SH | LOCK_NB) != 0 && errno == EWOULDBLOCK;
        close(fd);
        return held;
    }

    void Log(const std::string& line) override { broker_log_.Write(Timestamp() + " " + line); }
    void Quit() override { quit_ = true; }
    void Finish(int code) override {
        finish_code_ = code;
        quit_ = true;
    }

    // -- event loop ------------------------------------------------------------
    bool Listen(const fs::path& path) {
        listen_fd_ = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
        sockaddr_un addr{};
        addr.sun_family = AF_UNIX;
        std::string p = path.string();
        if (p.size() >= sizeof addr.sun_path) return false;
        std::strcpy(addr.sun_path, p.c_str());
        unlink(p.c_str());  // a socket left by a broker that died (we hold the instance lock)
        mode_t old = umask(077);  // this user only
        bool ok = bind(listen_fd_, (sockaddr*)&addr, sizeof addr) == 0 && listen(listen_fd_, 64) == 0;
        umask(old);
        SetNonBlocking(listen_fd_);
        return ok;
    }

    int Run() {
        while (!quit_) {
            std::vector<pollfd> fds;
            std::vector<std::pair<int, ClientId>> kinds;  // what each pollfd is
            auto add = [&](int fd, short events, int kind, ClientId id = 0) {
                fds.push_back({fd, events, 0});
                kinds.push_back({kind, id});
            };
            if (listen_fd_ >= 0) add(listen_fd_, POLLIN, 0);
            for (auto& [id, c] : clients_)
                add(c.fd, (short)(POLLIN | (c.out.empty() ? 0 : POLLOUT)), 1, id);
            if (out_fd_ >= 0) add(out_fd_, POLLIN, 2);
            if (err_fd_ >= 0) add(err_fd_, POLLIN, 3);
            if (pidfd_ >= 0 && !exited_) add(pidfd_, POLLIN, 4);
            if (in_fd_ >= 0 && !in_queue_.empty()) add(in_fd_, POLLOUT, 5);
            int64_t deadline = handler_->NextDeadline();
            int timeout = -1;
            if (deadline != NEVER) {
                int64_t wait = deadline - Now();
                timeout = wait <= 0 ? 0 : (int)std::min<int64_t>(wait, 60000);
            }
            int n = poll(fds.data(), fds.size(), timeout);
            if (n < 0 && errno != EINTR) return 1;
            for (size_t i = 0; i < fds.size() && !quit_; ++i) {
                if (!fds[i].revents) continue;
                auto [kind, id] = kinds[i];
                if (kind == 0) Accept();
                else if (kind == 1) ServeClient(id, fds[i].revents);
                else if (kind == 2) EngineOutput();
                else if (kind == 3) EngineErrors();
                else if (kind == 4) EngineExited();
                else if (kind == 5) FlushEngineInput();
            }
            MaybeReportExit();
            if (!quit_ && handler_->NextDeadline() <= Now()) handler_->OnTimer();
            for (auto it = admin_pids_.begin(); it != admin_pids_.end();)
                it = waitpid(*it, nullptr, WNOHANG) != 0 ? admin_pids_.erase(it) : it + 1;
        }
        return finish_code_;
    }

private:
    struct Client {
        int fd;
        std::string in, out;
    };

    void Accept() {
        for (;;) {
            int fd = accept4(listen_fd_, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
            if (fd < 0) return;
            ClientId id = next_client_++;
            clients_[id] = Client{fd, {}, {}};
            handler_->OnClientConnected(id);
        }
    }

    void ServeClient(ClientId id, short revents) {
        auto it = clients_.find(id);
        if (it == clients_.end()) return;
        if (revents & POLLOUT) FlushClient(it->second);
        if (revents & (POLLIN | POLLHUP | POLLERR)) {
            std::vector<std::string> lines;
            bool open = ReadLines(it->second.fd, it->second.in, lines);
            for (auto& line : lines) {
                if (clients_.count(id)) handler_->OnClientLine(id, line);
                if (quit_) return;
            }
            if (!open) {
                auto again = clients_.find(id);
                if (again != clients_.end()) {
                    close(again->second.fd);
                    clients_.erase(again);
                    handler_->OnClientClosed(id);
                }
            }
        }
    }

    void FlushClient(Client& c) {
        while (!c.out.empty()) {
            ssize_t n = send(c.fd, c.out.data(), c.out.size(), MSG_NOSIGNAL);
            if (n <= 0) return;  // full (POLLOUT later) or gone (POLLHUP later)
            c.out.erase(0, (size_t)n);
        }
    }

    void FlushEngineInput() {
        while (in_fd_ >= 0 && !in_queue_.empty()) {
            ssize_t n = write(in_fd_, in_queue_.data(), in_queue_.size());
            if (n > 0) {
                in_queue_.erase(0, (size_t)n);
                continue;
            }
            if (n < 0 && errno == EAGAIN) return;
            in_queue_.clear();  // the engine is gone; its exit is on the way
            return;
        }
    }

    void EngineOutput() {
        std::vector<std::string> lines;
        bool open = ReadLines(out_fd_, out_buf_, lines);
        for (auto& line : lines) handler_->OnEngineLine(line);
        if (!open) {
            close(out_fd_);
            out_fd_ = -1;
        }
    }

    void EngineErrors() {
        std::vector<std::string> lines;
        bool open = ReadLines(err_fd_, err_buf_, lines);
        for (auto& line : lines) {
            engine_log_.Write(line);
            handler_->OnEngineStderrLine(line);
        }
        if (!open) {
            close(err_fd_);
            err_fd_ = -1;
        }
    }

    void EngineExited() {
        int status = 0;
        if (waitpid(pid_, &status, WNOHANG) != pid_) return;
        exited_ = true;
        exit_code_ = WIFEXITED(status) ? WEXITSTATUS(status) : -WTERMSIG(status);
    }

    // The exit is reported once all of the engine's output has been read.
    void MaybeReportExit() {
        if (pid_ <= 0 || !exited_ || out_fd_ >= 0 || err_fd_ >= 0) return;
        close(pidfd_);
        pidfd_ = -1;
        CloseEngineInput();
        pid_ = -1;
        handler_->OnEngineExit(exit_code_);
    }

    fs::path data_dir_;
    EngineConfig config_;
    bool have_config_;
    BoundedLog broker_log_, engine_log_;
    Handler* handler_ = nullptr;
    int listen_fd_ = -1;
    std::map<ClientId, Client> clients_;
    ClientId next_client_ = 1;
    pid_t pid_ = -1;
    int pidfd_ = -1, in_fd_ = -1, out_fd_ = -1, err_fd_ = -1;
    bool exited_ = false;
    int exit_code_ = 0;
    std::string out_buf_, err_buf_, in_queue_;
    std::vector<pid_t> admin_pids_;
    bool quit_ = false;
    int finish_code_ = 0;
};

}  // namespace
}  // namespace broker

int main(int argc, char** argv) {
    using namespace broker;
    signal(SIGPIPE, SIG_IGN);
    const char* home = std::getenv("HOME");
    fs::path data_dir = fs::path(home ? home : ".") / ".oyayubi-ime";
    fs::path engine_json, socket_path, selftest;
    bool test_hooks = false;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto value = [&]() -> std::string { return i + 1 < argc ? argv[++i] : ""; };
        if (a == "--data-dir") data_dir = value();
        else if (a == "--engine-json") engine_json = value();
        else if (a == "--socket") socket_path = value();
        else if (a == "--selftest") selftest = value();
        else if (a == "--test-hooks") test_hooks = true;
        else {
            std::fprintf(stderr, "unknown argument %s\n", a.c_str());
            return 2;
        }
    }
    if (socket_path.empty()) socket_path = data_dir / "broker.sock";

    if (!selftest.empty()) {
        EngineConfig config;
        std::string error;
        bool ok = ReadEngineConfig(selftest, config, error);
        PosixPlatform platform(data_dir, config, ok);
        if (!ok) platform.Log("selftest: " + error);
        SelfTest test(platform);
        platform.SetHandler(&test);
        test.Start();
        int code = platform.Run();
        std::printf("selftest %s (exit %d)\n", code == 0 ? "passed" : "failed", code);
        return code;
    }

    std::error_code ec;
    fs::create_directories(data_dir, ec);
    // One broker per user (Local\oyayubi-ime-broker on Windows).
    int lock = open((data_dir / "broker.lock").c_str(), O_CREAT | O_RDWR | O_CLOEXEC, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB) != 0) return 0;

    EngineConfig config;
    std::string error;
    bool installed = ReadEngineConfig(engine_json, config, error);
    PosixPlatform platform(data_dir, config, installed);
    platform.Log("broker start pid " + std::to_string(getpid()));
    if (!installed) platform.Log(error);
    if (!platform.Listen(socket_path)) {
        platform.Log("cannot listen on " + socket_path.string());
        return 1;
    }
    Broker::Options options;
    options.installed = installed;
    options.test_hooks = test_hooks;
    Broker core(platform, options);
    BrokerHandler handler(core);
    platform.SetHandler(&handler);
    core.Start();
    int code = platform.Run();
    unlink(socket_path.c_str());
    platform.Log("broker exit");
    return code;
}
