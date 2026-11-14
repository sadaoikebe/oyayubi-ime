// oyayubi-broker.exe: the broker on Windows (REDESIGN §5).
//
// One per sign-in (Local\oyayubi-ime-broker). No console; its only window is
// a hidden one for the notification icon. Everything else happens on one
// thread around an I/O completion port: the TIPs' named pipe, the engine's
// output (read by two threads that post each line), the engine's exit, the
// notification clicks, and the Broker's timers.
//
//   oyayubi-broker.exe                     the normal start (Run key, or a TIP)
//   oyayubi-broker.exe --selftest ENGINE_JSON
//   test/development overrides: --engine-json PATH --data-dir DIR --pipe NAME --test-hooks
#include <windows.h>

#include <commctrl.h>
#include <sddl.h>
#include <shellapi.h>
#include <shlobj.h>
#include <userenv.h>

#include <algorithm>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "broker_core.h"
#include "support.h"

#ifndef OYAYUBI_TAG
#define OYAYUBI_TAG "dev"
#endif

namespace broker {
namespace {

constexpr wchar_t BROKER_MUTEX[] = L"Local\\oyayubi-ime-broker";
constexpr wchar_t MAINTENANCE_MUTEX[] = L"Local\\oyayubi-ime-maintenance";
constexpr wchar_t SPAWN_MUTEX[] = L"Local\\oyayubi-ime-spawn";
constexpr wchar_t PRODUCT_KEY[] = L"Software\\oyayubi-ime";
constexpr wchar_t SESSION_KEY[] = L"Software\\oyayubi-ime\\Session";
constexpr wchar_t INSTALL_HELP[] =
    L"https://github.com/sadaoikebe/oyayubi-ime/blob/main/docs/INSTALL.md";
constexpr DWORD PIPE_BUFFER = 64 * 1024;
constexpr UINT WM_APP_NOTICE = WM_APP + 1;
constexpr UINT WM_APP_CLEAR = WM_APP + 2;
constexpr UINT WM_APP_TRAY = WM_APP + 3;
constexpr UINT WM_APP_EXIT = WM_APP + 4;
constexpr UINT WM_APP_REASON = WM_APP + 5;
constexpr ULONG_PTR KEY_IO = 1, KEY_ENGINE = 2, KEY_UI = 3;
constexpr int UI_RETRY = 1;
constexpr int BUTTON_RETRY = 101, BUTTON_LOGS = 102, BUTTON_REINSTALL = 103;

std::wstring Wide(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), w.data(), n);
    return w;
}

std::string LastError(const char* what) { return std::string(what) + " failed (" +
                                                  std::to_string(GetLastError()) + ")"; }

fs::path KnownFolder(REFKNOWNFOLDERID id) {
    PWSTR path = nullptr;
    fs::path out;
    if (SUCCEEDED(SHGetKnownFolderPath(id, 0, nullptr, &path))) out = path;
    CoTaskMemFree(path);
    return out;
}

// One argument for CreateProcess's command line (the CommandLineToArgvW rules).
std::wstring Quote(const std::wstring& arg) {
    if (!arg.empty() && arg.find_first_of(L" \t\"") == std::wstring::npos) return arg;
    std::wstring out = L"\"";
    size_t backslashes = 0;
    for (wchar_t c : arg) {
        if (c == L'\\') {
            ++backslashes;
            continue;
        }
        out.append(c == L'"' ? backslashes * 2 + 1 : backslashes, L'\\');
        backslashes = 0;
        out += c;
    }
    out.append(backslashes * 2, L'\\');
    return out + L"\"";
}

std::wstring CommandLine(const std::string& program, const std::vector<std::string>& args) {
    std::wstring line = Quote(Wide(program));
    for (const auto& a : args) line += L" " + Quote(Wide(a));
    return line;
}

std::wstring UserSid() {
    HANDLE token = nullptr;
    std::wstring sid;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return sid;
    DWORD size = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &size);
    std::vector<BYTE> buffer(size);
    if (GetTokenInformation(token, TokenUser, buffer.data(), size, &size)) {
        LPWSTR text = nullptr;
        if (ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(buffer.data())->User.Sid, &text)) {
            sid = text;
            LocalFree(text);
        }
    }
    CloseHandle(token);
    return sid;
}

std::wstring DefaultPipeName() {
    DWORD session = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &session);
    return L"\\\\.\\pipe\\oyayubi-ime-" + UserSid() + L"-" + std::to_wstring(session);
}

std::wstring ReadTag() {
    wchar_t value[128];
    DWORD size = sizeof value;
    if (RegGetValueW(HKEY_CURRENT_USER, PRODUCT_KEY, L"Tag", RRF_RT_REG_SZ, nullptr, value,
                     &size) != ERROR_SUCCESS)
        return {};
    return value;
}

// §5.3: the user's default environment, not ours; Python told to be quiet and
// local; nothing that could point it at another Python's libraries.
std::vector<wchar_t> EngineEnvironment() {
    std::vector<std::wstring> vars;
    HANDLE token = nullptr;
    LPVOID block = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, &token) &&
        CreateEnvironmentBlock(&block, token, FALSE)) {
        for (auto p = static_cast<const wchar_t*>(block); *p; p += wcslen(p) + 1) vars.push_back(p);
        DestroyEnvironmentBlock(block);
    }
    if (token) CloseHandle(token);
    auto key = [](const std::wstring& v) {
        std::wstring k = v.substr(0, v.find(L'=', 1));
        CharUpperBuffW(k.data(), (DWORD)k.size());
        return k;
    };
    const std::set<std::wstring> drop = {L"PYTHONPATH", L"PYTHONHOME", L"PYTHONUTF8",
                                         L"PYTHONIOENCODING", L"PYTHONNOUSERSITE",
                                         L"HF_HUB_OFFLINE"};
    vars.erase(std::remove_if(vars.begin(), vars.end(),
                              [&](const std::wstring& v) { return drop.count(key(v)) > 0; }),
               vars.end());
    for (const wchar_t* add : {L"PYTHONUTF8=1", L"PYTHONIOENCODING=utf-8", L"PYTHONNOUSERSITE=1",
                               L"HF_HUB_OFFLINE=1"})
        vars.push_back(add);
    std::sort(vars.begin(), vars.end(),
              [&](const std::wstring& a, const std::wstring& b) { return key(a) < key(b); });
    std::vector<wchar_t> out;
    for (const auto& v : vars) {
        out.insert(out.end(), v.begin(), v.end());
        out.push_back(L'\0');
    }
    out.push_back(L'\0');
    return out;
}

// Lines out of a pipe, on a thread; each line is posted to the loop.
struct EngineEvent {
    enum Kind { Out, Err, Exit } kind;
    int generation;
    std::string line;
    int code = 0;
};

void PostEngine(HANDLE iocp, EngineEvent* event) {
    PostQueuedCompletionStatus(iocp, 0, KEY_ENGINE, reinterpret_cast<LPOVERLAPPED>(event));
}

void ReadLinesFrom(HANDLE pipe, HANDLE iocp, int generation, EngineEvent::Kind kind) {
    std::string buffer;
    char chunk[65536];
    DWORD n = 0;
    while (ReadFile(pipe, chunk, sizeof chunk, &n, nullptr) && n > 0) {
        buffer.append(chunk, n);
        size_t start = 0;
        for (size_t nl; (nl = buffer.find('\n', start)) != std::string::npos; start = nl + 1) {
            std::string line = buffer.substr(start, nl - start);
            if (!line.empty() && line.back() == '\r') line.pop_back();
            PostEngine(iocp, new EngineEvent{kind, generation, line});
        }
        buffer.erase(0, start);
    }
    if (!buffer.empty()) PostEngine(iocp, new EngineEvent{kind, generation, buffer});
    CloseHandle(pipe);
}

// The engine's stdin: lines are written by a thread, so a stuck engine cannot
// block the loop (its timers must keep running to end it).
class EngineInput {
public:
    explicit EngineInput(HANDLE pipe) : pipe_(pipe), thread_([this] { Run(); }) {}
    ~EngineInput() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            closing_ = true;
        }
        ready_.notify_one();
        thread_.join();
    }
    void Write(const std::string& line) {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            queue_.push_back(line + "\n");
        }
        ready_.notify_one();
    }

private:
    void Run() {
        for (;;) {
            std::string data;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                ready_.wait(lock, [this] { return closing_ || !queue_.empty(); });
                if (queue_.empty()) break;
                data = std::move(queue_.front());
                queue_.pop_front();
            }
            DWORD written = 0;
            if (!WriteFile(pipe_, data.data(), (DWORD)data.size(), &written, nullptr)) break;
        }
        CloseHandle(pipe_);  // the engine reads end-of-input and exits
    }
    HANDLE pipe_;
    std::mutex mutex_;
    std::condition_variable ready_;
    std::deque<std::string> queue_;
    bool closing_ = false;
    std::thread thread_;
};

// -- the notification (§9.3) ---------------------------------------------------

struct Texts {
    const wchar_t* title;
    const wchar_t* body;
    const wchar_t* detail;
};

Texts TextsFor(const Notice& n) {
    const wchar_t* stopped_title = L"oyayubi-ime: 日本語変換が止まりました";
    const wchar_t* stopped_body =
        L"入力済みの文字はそのまま残っています。ここを押すと、原因と直し方を表示します。";
    if (n.stopped) {
        switch (n.reason) {
            case Stop::ProgramFiles:
            case Stop::Dictionary:
                return {stopped_title, stopped_body,
                        L"必要なファイルが見つからないか、壊れています。入れ直すと直ります。"
                        L"学習した内容と登録した単語は残ります。"};
            case Stop::Cache:
                return {stopped_title, stopped_body,
                        L"辞書の準備ができませんでした。ディスクの空きを確認してください"
                        L"（約 300MB 必要です）。"};
            case Stop::Hang:
                return {stopped_title, stopped_body, L"変換プログラムが反応しなくなりました。"};
            default:
                return {stopped_title, stopped_body, L"変換プログラムが繰り返し止まりました。"};
        }
    }
    if (n.warning == "config")
        return {L"oyayubi-ime: 設定ファイルを読めませんでした", L"標準の設定で動いています。",
                L"設定ファイル（.oyayubi-ime\\config.json）の一部を読めませんでした。"
                L"読めなかった項目は標準の値で動いています。設定ウィンドウの「設定」タブで直せます。"};
    if (n.warning == "lm")
        return {L"oyayubi-ime: 言語モデルを使えません", L"辞書だけで変換しています。",
                L"変換の候補を選び直す言語モデルを読み込めませんでした。"
                L"辞書だけで変換しています。入れ直すと直ることがあります。"};
    if (n.warning == "learning-read")
        return {L"oyayubi-ime: 学習の記録を読めませんでした", L"学習なしで動いています。",
                L"学習の記録（.oyayubi-ime\\learning.jsonl）を読めませんでした。"
                L"ほかのプログラムが開いている可能性があります。次に起動したとき、もう一度読みます。"};
    if (n.warning == "learning-write")
        return {L"oyayubi-ime: 学習を保存できませんでした", L"覚えた内容はこの間だけ有効です。",
                L"学習の記録を書き込めませんでした。ディスクの空きを確認してください。"};
    return {L"oyayubi-ime: 注意", L"", L""};
}

class NotifyUi {
public:
    NotifyUi(HANDLE iocp, fs::path logs) : iocp_(iocp), logs_(logs) {}
    ~NotifyUi() { Stop(); }

    void Start() {
        HANDLE created = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        thread_ = std::thread([this, created] { Run(created); });
        WaitForSingleObject(created, INFINITE);
        CloseHandle(created);
    }
    void Stop() {
        if (thread_.joinable()) {
            PostMessageW(hwnd_, WM_APP_EXIT, 0, 0);
            thread_.join();
        }
    }
    void Post(const Notice& notice) { PostMessageW(hwnd_, WM_APP_NOTICE, 0, (LPARAM)new Notice(notice)); }
    void ClearStopped() { PostMessageW(hwnd_, WM_APP_CLEAR, 0, 0); }
    void ShowReason() { PostMessageW(hwnd_, WM_APP_REASON, 0, 0); }

private:
    void Run(HANDLE created) {
        HINSTANCE instance = GetModuleHandleW(nullptr);
        WNDCLASSEXW wc = {};
        wc.cbSize = sizeof wc;
        wc.lpfnWndProc = &NotifyUi::Proc;
        wc.hInstance = instance;
        wc.lpszClassName = L"oyayubi-ime-broker";
        RegisterClassExW(&wc);
        // Never shown: it exists to receive the notification icon's messages.
        hwnd_ = CreateWindowExW(0, wc.lpszClassName, L"oyayubi-ime", WS_OVERLAPPED, 0, 0, 0, 0,
                                nullptr, nullptr, instance, this);
        taskbar_created_ = RegisterWindowMessageW(L"TaskbarCreated");
        SetEvent(created);
        MSG msg;
        while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        RemoveIcon();
        DestroyWindow(hwnd_);
    }

    static LRESULT CALLBACK Proc(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
        if (message == WM_NCCREATE) {
            auto self = static_cast<NotifyUi*>(reinterpret_cast<CREATESTRUCTW*>(lparam)->lpCreateParams);
            SetWindowLongPtrW(hwnd, GWLP_USERDATA, (LONG_PTR)self);
        }
        auto self = reinterpret_cast<NotifyUi*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (self) return self->Handle(hwnd, message, wparam, lparam);
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }

    LRESULT Handle(HWND hwnd, UINT message, WPARAM wparam, LPARAM lparam) {
        if (message == WM_APP_NOTICE) {
            std::unique_ptr<Notice> notice(reinterpret_cast<Notice*>(lparam));
            if (notice->stopped || !stopped_) current_ = *notice;
            if (notice->stopped) stopped_ = true;
            ShowBalloon(*notice);
            return 0;
        }
        if (message == WM_APP_CLEAR) {
            stopped_ = false;
            RemoveIcon();
            return 0;
        }
        if (message == WM_APP_REASON) {
            if (stopped_) ShowDialog();  // asked for from the TIP's menu
            return 0;
        }
        if (message == WM_APP_EXIT || message == WM_ENDSESSION) {
            PostQuitMessage(0);
            return 0;
        }
        if (message == WM_APP_TRAY) {
            switch (LOWORD(lparam)) {
                case NIN_BALLOONUSERCLICK:
                case NIN_SELECT:
                case NIN_KEYSELECT:
                case WM_CONTEXTMENU:
                    ShowDialog();  // the user asked for it (§9.3)
                    break;
                case NIN_BALLOONTIMEOUT:
                    if (!stopped_) RemoveIcon();  // a warning leaves no icon behind
                    break;
            }
            return 0;
        }
        if (message == taskbar_created_ && icon_) {
            icon_ = false;  // Explorer restarted: the icon is gone; show the stop again
            if (stopped_) ShowBalloon(current_);
            return 0;
        }
        return DefWindowProcW(hwnd, message, wparam, lparam);
    }

    void ShowBalloon(const Notice& notice) {
        Texts t = TextsFor(notice);
        NOTIFYICONDATAW nid = {};
        nid.cbSize = sizeof nid;
        nid.hWnd = hwnd_;
        nid.uID = 1;
        nid.uFlags = NIF_ICON | NIF_MESSAGE | NIF_TIP | NIF_SHOWTIP | NIF_INFO;
        nid.uCallbackMessage = WM_APP_TRAY;
        nid.hIcon = LoadIconW(nullptr, notice.stopped ? IDI_WARNING : IDI_INFORMATION);
        wcsncpy_s(nid.szTip, L"oyayubi-ime", _TRUNCATE);
        wcsncpy_s(nid.szInfoTitle, t.title, _TRUNCATE);
        std::wstring body = t.body;
        if (!notice.stopped) body += L"変換は使えます。ここを押すと詳しく表示します。";
        wcsncpy_s(nid.szInfo, body.c_str(), _TRUNCATE);
        nid.dwInfoFlags = (notice.stopped ? NIIF_WARNING : NIIF_INFO) | NIIF_NOSOUND;
        if (!icon_) {
            Shell_NotifyIconW(NIM_ADD, &nid);
            nid.uVersion = NOTIFYICON_VERSION_4;
            Shell_NotifyIconW(NIM_SETVERSION, &nid);
            icon_ = true;
        } else {
            Shell_NotifyIconW(NIM_MODIFY, &nid);
        }
    }

    void RemoveIcon() {
        if (!icon_) return;
        NOTIFYICONDATAW nid = {};
        nid.cbSize = sizeof nid;
        nid.hWnd = hwnd_;
        nid.uID = 1;
        Shell_NotifyIconW(NIM_DELETE, &nid);
        icon_ = false;
    }

    void ShowDialog() {
        Texts t = TextsFor(current_);
        std::vector<TASKDIALOG_BUTTON> buttons;
        if (current_.stopped) {
            if (current_.reason == Stop::ProgramFiles || current_.reason == Stop::Dictionary) {
                buttons.push_back({BUTTON_REINSTALL, L"入れ直す手順を開く"});
            } else {
                buttons.push_back({BUTTON_RETRY, L"もう一度試す"});
                if (current_.reason != Stop::Cache)
                    buttons.push_back({BUTTON_LOGS, L"記録のフォルダを開く"});
            }
        }
        buttons.push_back({IDCLOSE, L"閉じる"});
        TASKDIALOGCONFIG config = {};
        config.cbSize = sizeof config;
        config.dwFlags = TDF_ALLOW_DIALOG_CANCELLATION | TDF_USE_COMMAND_LINKS;
        config.pszWindowTitle = L"oyayubi-ime";
        config.pszMainIcon = current_.stopped ? TD_WARNING_ICON : TD_INFORMATION_ICON;
        config.pszMainInstruction = t.title + wcslen(L"oyayubi-ime: ");
        config.pszContent = t.detail;
        config.pButtons = buttons.data();
        config.cButtons = (UINT)buttons.size();
        int pressed = 0;
        if (FAILED(TaskDialogIndirect(&config, &pressed, nullptr, nullptr))) return;
        if (pressed == BUTTON_RETRY) {
            PostQueuedCompletionStatus(iocp_, 0, KEY_UI, reinterpret_cast<LPOVERLAPPED>(UI_RETRY));
        } else if (pressed == BUTTON_LOGS) {
            ShellExecuteW(nullptr, L"open", logs_.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
        } else if (pressed == BUTTON_REINSTALL) {
            ShellExecuteW(nullptr, L"open", INSTALL_HELP, nullptr, nullptr, SW_SHOWNORMAL);
        }
    }

    HANDLE iocp_;
    fs::path logs_;
    std::thread thread_;
    HWND hwnd_ = nullptr;
    UINT taskbar_created_ = 0;
    bool icon_ = false;
    bool stopped_ = false;
    Notice current_;
};

// -- the platform ----------------------------------------------------------------

struct Op : OVERLAPPED {
    enum Kind { Listen, Read, Write } kind;
    struct Pipe* pipe;
};

struct Pipe {
    ClientId id = 0;
    HANDLE handle = INVALID_HANDLE_VALUE;
    Op read{}, write{};
    char buffer[8192];
    std::string in;
    std::deque<std::string> out;
    std::string writing;
    bool reading = false, write_pending = false, closed = false;
};

class WinPlatform : public SelfTestPlatform {
    struct Search {
        std::set<DWORD> pids;
        int count = 0;
    };
    static BOOL CALLBACK CountWindow(HWND hwnd, LPARAM p) {
        auto search = reinterpret_cast<Search*>(p);
        DWORD pid = 0;
        GetWindowThreadProcessId(hwnd, &pid);
        if (search->pids.count(pid) && IsWindowVisible(hwnd)) ++search->count;
        return TRUE;
    }

public:
    WinPlatform(fs::path data_dir, EngineConfig config, bool have_config)
        : data_dir_(data_dir), config_(config), have_config_(have_config),
          broker_log_(data_dir / "logs" / "broker.log", data_dir / "logs" / "broker.prev.log", false),
          engine_log_(data_dir / "logs" / "engine.log", data_dir / "logs" / "engine.prev.log", true) {
        std::error_code ec;
        fs::create_directories(data_dir / "logs", ec);
        iocp_ = CreateIoCompletionPort(INVALID_HANDLE_VALUE, nullptr, 0, 1);
        job_ = CreateJobObjectW(nullptr, nullptr);
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
        // §5.3: when the broker goes, for whatever reason, the engine goes with it.
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(job_, JobObjectExtendedLimitInformation, &limits, sizeof limits);
    }

    void SetHandler(Handler* handler) { handler_ = handler; }
    void StartUi() {
        ui_ = std::make_unique<NotifyUi>(iocp_, data_dir_ / "logs");
        ui_->Start();
    }
    HANDLE Iocp() const { return iocp_; }

    // -- Platform --------------------------------------------------------------
    int64_t Now() override { return (int64_t)GetTickCount64(); }

    bool StartEngine(std::string& error) override {
        if (!have_config_) {
            error = "no engine.json";
            return false;
        }
        std::wstring python = Wide(config_.python);
        // §5.3: only the python.exe named in engine.json; no other Python is looked for.
        if (GetFileAttributesW(python.c_str()) == INVALID_FILE_ATTRIBUTES) {
            error = "python.exe not found: " + config_.python;
            return false;
        }
        SECURITY_ATTRIBUTES inherit{sizeof inherit, nullptr, TRUE};
        HANDLE in_r = nullptr, in_w = nullptr, out_r = nullptr, out_w = nullptr, err_r = nullptr,
               err_w = nullptr;
        if (!CreatePipe(&in_r, &in_w, &inherit, 0) || !CreatePipe(&out_r, &out_w, &inherit, 0) ||
            !CreatePipe(&err_r, &err_w, &inherit, 0)) {
            error = LastError("CreatePipe");
            for (HANDLE h : {in_r, in_w, out_r, out_w, err_r, err_w})
                if (h) CloseHandle(h);
            return false;
        }
        for (HANDLE ours : {in_w, out_r, err_r}) SetHandleInformation(ours, HANDLE_FLAG_INHERIT, 0);

        HANDLE handles[] = {in_r, out_w, err_w};  // the child inherits these and nothing else
        SIZE_T size = 0;
        InitializeProcThreadAttributeList(nullptr, 1, 0, &size);
        std::vector<BYTE> attributes(size);
        auto list = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attributes.data());
        InitializeProcThreadAttributeList(list, 1, 0, &size);
        UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles,
                                  sizeof handles, nullptr, nullptr);
        STARTUPINFOEXW si{};
        si.StartupInfo.cb = sizeof si;
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        si.StartupInfo.hStdInput = in_r;
        si.StartupInfo.hStdOutput = out_w;
        si.StartupInfo.hStdError = err_w;
        si.lpAttributeList = list;
        std::wstring command = CommandLine(config_.python, config_.args);
        std::vector<wchar_t> environment = EngineEnvironment();
        std::wstring app = Wide(config_.app);
        PROCESS_INFORMATION pi{};
        engine_log_.Rotate();
        BOOL created = CreateProcessW(
            python.c_str(), command.data(), nullptr, nullptr, TRUE,
            CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | CREATE_SUSPENDED |
                EXTENDED_STARTUPINFO_PRESENT,
            environment.data(), app.c_str(), &si.StartupInfo, &pi);
        if (!created) error = LastError("CreateProcess");
        DeleteProcThreadAttributeList(list);
        for (HANDLE theirs : {in_r, out_w, err_w}) CloseHandle(theirs);
        if (!created) {
            for (HANDLE ours : {in_w, out_r, err_r}) CloseHandle(ours);
            return false;
        }
        if (!AssignProcessToJobObject(job_, pi.hProcess)) {
            error = LastError("AssignProcessToJobObject");
            TerminateProcess(pi.hProcess, 1);
            for (HANDLE h : {pi.hProcess, pi.hThread, in_w, out_r, err_r}) CloseHandle(h);
            return false;
        }
        ResumeThread(pi.hThread);
        CloseHandle(pi.hThread);
        ++generation_;
        process_ = pi.hProcess;
        input_ = std::make_unique<EngineInput>(in_w);
        int generation = generation_;
        HANDLE iocp = iocp_, process = process_;
        // Output first, then the exit: the exit is posted after both streams end.
        std::thread([=] {
            std::thread errors(ReadLinesFrom, err_r, iocp, generation, EngineEvent::Err);
            ReadLinesFrom(out_r, iocp, generation, EngineEvent::Out);
            errors.join();
            WaitForSingleObject(process, INFINITE);
            DWORD code = 0;
            GetExitCodeProcess(process, &code);
            auto event = new EngineEvent{EngineEvent::Exit, generation, {}};
            event->code = (int)code;
            PostEngine(iocp, event);
        }).detach();
        return true;
    }

    void WriteEngine(const std::string& line) override {
        if (input_) input_->Write(line);
    }

    void KillEngine() override {
        if (process_) TerminateJobObject(job_, 9);  // the engine and anything it started
    }

    void CloseEngineInput() override { input_.reset(); }

    int CountEngineWindows() override {
        struct Ids {
            JOBOBJECT_BASIC_PROCESS_ID_LIST list;
            ULONG_PTR more[63];
        } ids{};
        ids.list.NumberOfAssignedProcesses = 64;
        if (!QueryInformationJobObject(job_, JobObjectBasicProcessIdList, &ids, sizeof ids, nullptr))
            return -1;
        Search search;
        for (DWORD i = 0; i < ids.list.NumberOfProcessIdsInList; ++i)
            search.pids.insert((DWORD)ids.list.ProcessIdList[i]);
        EnumWindows(&WinPlatform::CountWindow, (LPARAM)&search);
        return search.count;
    }

    void WriteClient(ClientId id, const std::string& line) override {
        auto it = pipes_.find(id);
        if (it == pipes_.end() || it->second->closed) return;
        it->second->out.push_back(line + "\n");
        PumpWrite(*it->second);
    }

    void Notify(const Notice& notice) override {
        RecordNotice(data_dir_ / "logs", notice);
        if (ui_) ui_->Post(notice);
    }
    void ClearStopped() override {
        if (ui_) ui_->ClearStopped();
    }
    void ShowReason() override {
        if (ui_) ui_->ShowReason();
    }

    void OpenAdmin() override {
        if (config_.pythonw.empty()) return;
        std::wstring command = CommandLine(config_.pythonw, config_.admin_args);
        std::vector<wchar_t> environment = EngineEnvironment();
        std::wstring app = Wide(config_.app);
        STARTUPINFOW si = {};
        si.cb = sizeof si;
        PROCESS_INFORMATION pi{};
        if (CreateProcessW(Wide(config_.pythonw).c_str(), command.data(), nullptr, nullptr, FALSE,
                           CREATE_UNICODE_ENVIRONMENT, environment.data(), app.c_str(), &si, &pi)) {
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
        } else {
            Log(LastError("starting the management window"));
        }
    }

    void ResetSpawnCount() override {
        HANDLE mutex = CreateMutexW(nullptr, FALSE, SPAWN_MUTEX);
        if (!mutex) return;
        if (WaitForSingleObject(mutex, 1000) == WAIT_OBJECT_0) {
            HKEY key;
            if (RegCreateKeyExW(HKEY_CURRENT_USER, SESSION_KEY, 0, nullptr, REG_OPTION_VOLATILE,
                                KEY_SET_VALUE, nullptr, &key, nullptr) == ERROR_SUCCESS) {
                DWORD zero = 0;
                RegSetValueExW(key, L"BrokerSpawns", 0, REG_DWORD, (const BYTE*)&zero, sizeof zero);
                RegCloseKey(key);
            }
            ReleaseMutex(mutex);
        }
        CloseHandle(mutex);
    }

    bool MaintenanceActive() override {
        HANDLE mutex = OpenMutexW(SYNCHRONIZE, FALSE, MAINTENANCE_MUTEX);
        if (!mutex) return false;
        CloseHandle(mutex);
        return true;
    }

    void Log(const std::string& line) override { broker_log_.Write(Timestamp() + " " + line); }
    void Quit() override { quit_ = true; }
    void Finish(int code) override {
        finish_code_ = code;
        quit_ = true;
    }

    // -- the pipe for the TIPs (§5.5) -------------------------------------------
    bool Listen(const std::wstring& name, std::string& error) {
        pipe_name_ = name;
        // The user, and every app package (sandboxed apps such as the Start
        // menu's search box) may connect; nobody else. Remote clients never.
        std::wstring sddl = L"D:P(A;;GA;;;" + UserSid() + L")(A;;GRGW;;;AC)";
        if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1,
                                                                  &security_, nullptr)) {
            error = LastError("ConvertStringSecurityDescriptor");
            return false;
        }
        return NextInstance(true, error);
    }

    int Run() {
        while (!quit_) {
            int64_t deadline = handler_->NextDeadline();
            DWORD timeout = INFINITE;
            if (deadline != NEVER) {
                int64_t wait = deadline - Now();
                timeout = wait <= 0 ? 0 : (DWORD)std::min<int64_t>(wait, 60000);
            }
            DWORD bytes = 0;
            ULONG_PTR key = 0;
            LPOVERLAPPED overlapped = nullptr;
            BOOL ok = GetQueuedCompletionStatus(iocp_, &bytes, &key, &overlapped, timeout);
            DWORD error = ok ? ERROR_SUCCESS : GetLastError();
            if (overlapped) {
                if (key == KEY_IO) Completed(static_cast<Op*>(overlapped), bytes, error);
                else if (key == KEY_ENGINE) Engine(reinterpret_cast<EngineEvent*>(overlapped));
                else if (key == KEY_UI && (INT_PTR)overlapped == UI_RETRY) retry_requested_ = true;
            }
            if (retry_requested_ && retry_) {
                retry_requested_ = false;
                retry_();
            }
            if (!quit_ && handler_->NextDeadline() <= Now()) handler_->OnTimer();
        }
        if (ui_) ui_->Stop();
        return finish_code_;
    }

    template <class F>
    void OnRetry(F f) { retry_ = f; }

private:
    bool NextInstance(bool first, std::string& error) {
        SECURITY_ATTRIBUTES sa{sizeof sa, security_, FALSE};
        HANDLE pipe = CreateNamedPipeW(
            pipe_name_.c_str(),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | (first ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0),
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            PIPE_UNLIMITED_INSTANCES, PIPE_BUFFER, PIPE_BUFFER, 0, &sa);
        if (pipe == INVALID_HANDLE_VALUE) {
            error = LastError("CreateNamedPipe");
            return false;
        }
        CreateIoCompletionPort(pipe, iocp_, KEY_IO, 0);
        listening_ = std::make_unique<Pipe>();
        listening_->handle = pipe;
        listening_->read.kind = Op::Listen;
        listening_->read.pipe = listening_.get();
        if (ConnectNamedPipe(pipe, &listening_->read)) return true;  // completion is queued
        DWORD e = GetLastError();
        if (e == ERROR_IO_PENDING) return true;
        if (e == ERROR_PIPE_CONNECTED) {
            // Connected before we asked: no completion will come; take it now.
            PostQueuedCompletionStatus(iocp_, 0, KEY_IO, &listening_->read);
            return true;
        }
        error = LastError("ConnectNamedPipe");
        CloseHandle(pipe);
        return false;
    }

    void Completed(Op* op, DWORD bytes, DWORD error) {
        Pipe* pipe = op->pipe;
        if (op->kind == Op::Listen) {
            std::unique_ptr<Pipe> owned = std::move(listening_);
            std::string why;
            if (!NextInstance(false, why)) Log("cannot wait for the next TIP: " + why);
            if (error != ERROR_SUCCESS && error != ERROR_PIPE_CONNECTED) {
                CloseHandle(owned->handle);
                return;
            }
            owned->id = next_client_++;
            Pipe* p = owned.get();
            pipes_[p->id] = std::move(owned);
            handler_->OnClientConnected(p->id);
            StartRead(*p);
            return;
        }
        if (op->kind == Op::Read) {
            pipe->reading = false;
            if (error == ERROR_SUCCESS && bytes > 0 && !pipe->closed) {
                pipe->in.append(pipe->buffer, bytes);
                size_t start = 0;
                ClientId id = pipe->id;
                for (size_t nl; !pipe->closed &&
                                (nl = pipe->in.find('\n', start)) != std::string::npos;
                     start = nl + 1) {
                    std::string line = pipe->in.substr(start, nl - start);
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    handler_->OnClientLine(id, line);  // may close this pipe (quit)
                }
                pipe->in.erase(0, start);
                StartRead(*pipe);
            } else {
                Close(*pipe);
            }
        } else {
            pipe->write_pending = false;
            if (error != ERROR_SUCCESS) Close(*pipe);
            else PumpWrite(*pipe);
        }
        Reap(*pipe);
    }

    void StartRead(Pipe& p) {
        if (p.closed) return;
        p.read = {};
        p.read.kind = Op::Read;
        p.read.pipe = &p;
        p.reading = true;
        if (!ReadFile(p.handle, p.buffer, sizeof p.buffer, nullptr, &p.read) &&
            GetLastError() != ERROR_IO_PENDING) {
            p.reading = false;
            Close(p);
        }
    }

    void PumpWrite(Pipe& p) {
        if (p.write_pending || p.closed || p.out.empty()) return;
        p.writing.clear();
        while (!p.out.empty()) {
            p.writing += p.out.front();
            p.out.pop_front();
        }
        p.write = {};
        p.write.kind = Op::Write;
        p.write.pipe = &p;
        p.write_pending = true;
        if (!WriteFile(p.handle, p.writing.data(), (DWORD)p.writing.size(), nullptr, &p.write) &&
            GetLastError() != ERROR_IO_PENDING) {
            p.write_pending = false;
            Close(p);
        }
    }

    void Close(Pipe& p) {
        if (p.closed) return;
        p.closed = true;
        CancelIoEx(p.handle, nullptr);
        handler_->OnClientClosed(p.id);
    }

    void Reap(Pipe& p) {
        if (!p.closed || p.reading || p.write_pending) return;
        CloseHandle(p.handle);
        pipes_.erase(p.id);
    }

    void Engine(EngineEvent* raw) {
        std::unique_ptr<EngineEvent> event(raw);
        if (event->generation != generation_) return;
        switch (event->kind) {
            case EngineEvent::Out: handler_->OnEngineLine(event->line); break;
            case EngineEvent::Err:
                engine_log_.Write(event->line);
                handler_->OnEngineStderrLine(event->line);
                break;
            case EngineEvent::Exit:
                input_.reset();
                CloseHandle(process_);
                process_ = nullptr;
                handler_->OnEngineExit(event->code);
                break;
        }
    }

    fs::path data_dir_;
    EngineConfig config_;
    bool have_config_;
    BoundedLog broker_log_, engine_log_;
    Handler* handler_ = nullptr;
    HANDLE iocp_ = nullptr, job_ = nullptr, process_ = nullptr;
    PSECURITY_DESCRIPTOR security_ = nullptr;
    std::wstring pipe_name_;
    std::unique_ptr<Pipe> listening_;
    std::map<ClientId, std::unique_ptr<Pipe>> pipes_;
    ClientId next_client_ = 1;
    int generation_ = 0;
    std::unique_ptr<EngineInput> input_;
    std::unique_ptr<NotifyUi> ui_;
    std::function<void()> retry_;
    bool retry_requested_ = false;
    bool quit_ = false;
    int finish_code_ = 0;
};

void Report(const std::string& text) {
    // A GUI program writes to stdout only when the caller gave it one (setup.ps1 does).
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (out && out != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        std::string line = text + "\r\n";
        WriteFile(out, line.data(), (DWORD)line.size(), &written, nullptr);
    }
}

}  // namespace
}  // namespace broker

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
    using namespace broker;
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    fs::path data_dir = KnownFolder(FOLDERID_Profile) / L".oyayubi-ime";
    fs::path engine_json, selftest;
    std::wstring pipe_name;
    bool test_hooks = false;
    for (int i = 1; i < argc; ++i) {
        std::wstring a = argv[i];
        auto value = [&]() -> std::wstring { return i + 1 < argc ? argv[++i] : L""; };
        if (a == L"--data-dir") data_dir = value();
        else if (a == L"--engine-json") engine_json = value();
        else if (a == L"--pipe") pipe_name = value();
        else if (a == L"--selftest") selftest = value();
        else if (a == L"--test-hooks") test_hooks = true;
        else return 2;
    }
    LocalFree(argv);
    INITCOMMONCONTROLSEX icc = {sizeof icc, ICC_STANDARD_CLASSES};
    InitCommonControlsEx(&icc);

    if (!selftest.empty()) {
        EngineConfig config;
        std::string error;
        bool ok = ReadEngineConfig(selftest, config, error);
        WinPlatform platform(data_dir, config, ok);
        if (!ok) platform.Log("selftest: " + error);
        SelfTest test(platform);
        platform.SetHandler(&test);
        test.Start();
        int code = platform.Run();
        Report(code == 0 ? "selftest passed" : "selftest failed (" + std::to_string(code) + ")");
        return code;
    }

    // §5.2: one broker per sign-in; a second one leaves without a word.
    HANDLE single = CreateMutexW(nullptr, TRUE, BROKER_MUTEX);
    if (!single || GetLastError() == ERROR_ALREADY_EXISTS) return 0;
    if (engine_json.empty()) {
        // §5.2: only the version this user has installed runs.
        std::wstring tag = ReadTag();
        if (tag != Wide(OYAYUBI_TAG)) return 0;
        engine_json = KnownFolder(FOLDERID_LocalAppData) / L"oyayubi-ime" / L"versions" / tag /
                      L"engine.json";
    }
    EngineConfig config;
    std::string error;
    bool installed = ReadEngineConfig(engine_json, config, error);
    WinPlatform platform(data_dir, config, installed);
    platform.Log(std::string("broker ") + OYAYUBI_TAG + " start pid " +
                 std::to_string(GetCurrentProcessId()));
    if (!installed) platform.Log(error + " (not installed for this user)");
    if (pipe_name.empty()) pipe_name = DefaultPipeName();
    if (!platform.Listen(pipe_name, error)) {
        platform.Log("cannot open the pipe: " + error);
        return 1;
    }
    platform.StartUi();
    Broker::Options options;
    options.installed = installed;
    options.test_hooks = test_hooks;
    Broker core(platform, options);
    BrokerHandler handler(core);
    platform.SetHandler(&handler);
    platform.OnRetry([&core] { core.Retry(); });
    core.Start();
    int code = platform.Run();
    platform.Log("broker exit");
    CloseHandle(single);
    return code;
}
