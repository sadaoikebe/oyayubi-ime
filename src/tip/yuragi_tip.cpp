// yuragi-ime TSF text service (REDESIGN §7).
//
// The TIP runs inside other people's applications, so it does as little as
// possible: it sends keys to the broker over a named pipe and writes the
// replies on screen. It never starts Python, never retries, keeps no files.
// The only process it may start is the broker, under the four conditions of
// §7.3. Its decisions that need no TSF live in tip_logic.h (tested on any OS).
//
// Practices from SampleIME / CorvusSKK / Mozc:
//   - disabled / empty context / keyboard closed: not our key
//   - empty composition: Back/Enter/Esc go to the application
//   - RequestEditSession: SYNC, then ASYNCDONTCARE on TF_E_SYNCHRONOUS
// Never block ActivateEx (Windows 11's TextInputHost is shared).

#include <windows.h>
#include <oleauto.h>
#include <olectl.h>
#include <initguid.h>
#include <msctf.h>
#include <ctffunc.h>
#include <inputscope.h>
#include <sddl.h>
#include <userenv.h>

#include <algorithm>
#include <climits>
#include <cstdarg>
#include <cstdio>
#include <cwchar>
#include <deque>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "resource.h"
#include "tip_json.h"
#include "tip_logic.h"

#ifndef YURAGI_TAG
#define YURAGI_TAG "dev"
#endif

using tip_logic::Link;
using tip_logic::Message;
using tip_logic::Mode;
using tip_logic::Notice;

// {A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}
static const CLSID CLSID_YuragiTip = {
    0xa7c4e201, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}
static const GUID GUID_YuragiProfile = {
    0xa7c4e202, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E206-0B3A-4F11-9E61-0C1A0B7E0A01}
static const GUID GUID_YuragiToggle = {
    0xa7c4e206, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E207-0B3A-4F11-9E61-0C1A0B7E0A01}
// Alt+` on the Japanese 101 layout (kbd101.dll) reaches TSF as Alt+VK_KANJI,
// never as VK_OEM_3, so the default toggle also preserves that alias.
static const GUID GUID_YuragiToggleKanji = {
    0xa7c4e207, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};

static const GUID kCats[] = {
    GUID_TFCAT_TIP_KEYBOARD,
    GUID_TFCAT_DISPLAYATTRIBUTEPROVIDER,
    GUID_TFCAT_TIPCAP_COMLESS,
    GUID_TFCAT_TIPCAP_INPUTMODECOMPARTMENT,
    GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT,
    GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT,
};

static LONG g_locks = 0;
static HINSTANCE g_hinst = nullptr;
static bool g_log = false;  // tip.json "log": true

static const wchar_t kProductKey[] = L"Software\\yuragi-ime";
static const wchar_t kSessionKey[] = L"Software\\yuragi-ime\\Session";
static const wchar_t kMaintenanceMutex[] = L"Local\\yuragi-ime-maintenance";
static const wchar_t kSpawnMutex[] = L"Local\\yuragi-ime-spawn";

// Only when tip.json asks for it: every application writes here.
static void Log(const wchar_t* fmt, ...) {
    if (!g_log) return;
    wchar_t path[MAX_PATH];
    GetTempPathW(MAX_PATH, path);
    wcscat_s(path, L"yuragi_tip.log");
    FILE* f = nullptr;
    _wfopen_s(&f, path, L"ab");
    if (!f) return;
    wchar_t buf[1024];
    va_list ap;
    va_start(ap, fmt);
    vswprintf_s(buf, fmt, ap);
    va_end(ap);
    fwprintf(f, L"%s\n", buf);
    fclose(f);
}

static std::wstring Utf8ToWide(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    if (n <= 0) return {};
    std::wstring w(n, 0);
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), w.data(), n);
    return w;
}

// Key times for the chord FSM: the high-resolution counter.
static ULONGLONG NowMs() {
    static LARGE_INTEGER frequency = [] {
        LARGE_INTEGER f;
        QueryPerformanceFrequency(&f);
        return f;
    }();
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return (ULONGLONG)(c.QuadPart * 1000 / frequency.QuadPart);
}

static UINT VkOf(WPARAM w) { return (UINT)(w & 0xFF); }

static tip_logic::Mods HeldMods() {
    tip_logic::Mods m;
    m.shift = (GetKeyState(VK_SHIFT) & 0x8000) != 0;
    m.ctrl = (GetKeyState(VK_CONTROL) & 0x8000) != 0;
    m.alt = (GetKeyState(VK_MENU) & 0x8000) != 0;
    m.win = ((GetKeyState(VK_LWIN) | GetKeyState(VK_RWIN)) & 0x8000) != 0;
    return m;
}
static unsigned ModMask(tip_logic::Mods m) {
    return (m.shift ? 1u : 0u) | (m.ctrl ? 2u : 0u) | (m.alt ? 4u : 0u) | (m.win ? 8u : 0u);
}

static bool CompartmentSet(ITfCompartmentMgr* cm, REFGUID guid) {
    ITfCompartment* c = nullptr;
    bool set = false;
    if (SUCCEEDED(cm->GetCompartment(guid, &c)) && c) {
        VARIANT v;
        VariantInit(&v);
        if (SUCCEEDED(c->GetValue(&v)) && v.vt == VT_I4 && v.lVal) set = true;
        VariantClear(&v);
        c->Release();
    }
    return set;
}

// Disabled or empty contexts are not ours (SampleIME / CorvusSKK / Mozc).
static bool KeyboardDisabled(ITfThreadMgr* tm) {
    if (!tm) return true;
    ITfDocumentMgr* dm = nullptr;
    if (FAILED(tm->GetFocus(&dm)) || !dm) return true;
    ITfContext* ctx = nullptr;
    if (FAILED(dm->GetTop(&ctx)) || !ctx) {
        dm->Release();
        return true;
    }
    bool disabled = false;
    ITfCompartmentMgr* cm = nullptr;
    if (SUCCEEDED(ctx->QueryInterface(IID_ITfCompartmentMgr, (void**)&cm)) && cm) {
        disabled = CompartmentSet(cm, GUID_COMPARTMENT_KEYBOARD_DISABLED) ||
                   CompartmentSet(cm, GUID_COMPARTMENT_EMPTYCONTEXT);
        cm->Release();
    }
    ctx->Release();
    dm->Release();
    return disabled;
}

static bool KeyboardClosed(ITfThreadMgr* tm) {
    if (!tm) return true;
    ITfCompartmentMgr* cm = nullptr;
    if (FAILED(tm->QueryInterface(IID_ITfCompartmentMgr, (void**)&cm)) || !cm) return false;
    bool closed = false;
    ITfCompartment* c = nullptr;
    if (SUCCEEDED(cm->GetCompartment(GUID_COMPARTMENT_KEYBOARD_OPENCLOSE, &c)) && c) {
        VARIANT v;
        VariantInit(&v);
        if (SUCCEEDED(c->GetValue(&v)) && v.vt == VT_I4 && v.lVal == 0) closed = true;
        VariantClear(&v);
        c->Release();
    }
    cm->Release();
    return closed;
}

static void SetKeyboardOpen(ITfThreadMgr* tm, TfClientId id, BOOL open) {
    if (!tm || id == TF_CLIENTID_NULL) return;
    ITfCompartmentMgr* cm = nullptr;
    if (FAILED(tm->QueryInterface(IID_ITfCompartmentMgr, (void**)&cm)) || !cm) return;
    ITfCompartment* c = nullptr;
    if (SUCCEEDED(cm->GetCompartment(GUID_COMPARTMENT_KEYBOARD_OPENCLOSE, &c)) && c) {
        VARIANT v;
        VariantInit(&v);
        v.vt = VT_I4;
        v.lVal = open ? 1 : 0;
        c->SetValue(id, &v);
        VariantClear(&v);
        c->Release();
    }
    cm->Release();
}

static std::wstring DllDir() {
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(g_hinst, path, MAX_PATH);
    std::wstring s(path);
    size_t slash = s.find_last_of(L"\\/");
    if (slash != std::wstring::npos) s.resize(slash);
    return s;
}

static bool ReadJsonFile(const std::wstring& path, tip_json::Value& value) {
    FILE* f = nullptr;
    if (_wfopen_s(&f, path.c_str(), L"rb") || !f) return false;
    std::string bytes;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) != 0) bytes.append(buf, n);
    fclose(f);
    if (bytes.compare(0, 3, "\xEF\xBB\xBF") == 0) bytes.erase(0, 3);
    return tip_json::Parser(bytes).Parse(value) && value.kind == tip_json::Value::Object;
}

// %USERPROFILE%\.yuragi-ime\tip.json (optional):
//   {"toggle_vk":192,"toggle_mods":["alt"],"log":false,"log_text":false}
struct TipConfig {
    UINT toggle_vk = VK_OEM_3;
    unsigned toggle_mods = 4;  // shift=1, ctrl=2, alt=4, win=8
    bool log = false, log_text = false;
    void Load() {
        wchar_t profile[MAX_PATH];
        DWORD n = GetEnvironmentVariableW(L"USERPROFILE", profile, MAX_PATH);
        if (!n || n >= MAX_PATH) return;
        tip_json::Value v;
        if (!ReadJsonFile(std::wstring(profile) + L"\\.yuragi-ime\\tip.json", v)) return;
        int vk;
        if (tip_logic::ReadInt(v, "toggle_vk", vk) && vk > 0 && vk < 256) toggle_vk = (UINT)vk;
        const auto& mods = v.Get("toggle_mods");
        if (mods.kind == tip_json::Value::Array) {
            unsigned mask = 0;
            bool valid = true;
            for (const auto& m : mods.array) {
                if (m.text == "shift") mask |= 1;
                else if (m.text == "ctrl") mask |= 2;
                else if (m.text == "alt") mask |= 4;
                else if (m.text == "win") mask |= 8;
                else valid = false;
            }
            if (valid) toggle_mods = mask;
        }
        tip_logic::ReadBool(v, "log", log);
        tip_logic::ReadBool(v, "log_text", log_text);
    }
};

// -- the broker: whether this process may start it (§7.3) --------------------------

static std::wstring PipeName() {
    std::wstring name = L"\\\\.\\pipe\\yuragi-ime-";
    HANDLE token = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) {
        DWORD size = 0;
        GetTokenInformation(token, TokenUser, nullptr, 0, &size);
        std::vector<BYTE> buffer(size);
        if (GetTokenInformation(token, TokenUser, buffer.data(), size, &size)) {
            LPWSTR sid = nullptr;
            if (ConvertSidToStringSidW(reinterpret_cast<TOKEN_USER*>(buffer.data())->User.Sid, &sid)) {
                name += sid;
                LocalFree(sid);
            }
        }
        CloseHandle(token);
    }
    DWORD session = 0;
    ProcessIdToSessionId(GetCurrentProcessId(), &session);
    return name + L"-" + std::to_wstring(session);
}

static bool TagMatches() {
    wchar_t value[128];
    DWORD size = sizeof value;
    if (RegGetValueW(HKEY_CURRENT_USER, kProductKey, L"Tag", RRF_RT_REG_SZ, nullptr, value, &size) !=
        ERROR_SUCCESS)
        return false;
    return Utf8ToWide(YURAGI_TAG) == value;
}

static bool MaintenanceActive() {
    HANDLE mutex = OpenMutexW(SYNCHRONIZE, FALSE, kMaintenanceMutex);
    if (!mutex) return false;
    CloseHandle(mutex);
    return true;
}

// Medium integrity exactly, and not an app container: neither sandboxed (it
// could not read the dictionary) nor elevated (the broker must not run as admin).
static bool MediumIntegrity() {
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
    bool medium = false;
    DWORD container = 0, size = sizeof container;
    bool app_container = GetTokenInformation(token, TokenIsAppContainer, &container, size, &size) &&
                         container;
    size = 0;
    GetTokenInformation(token, TokenIntegrityLevel, nullptr, 0, &size);
    std::vector<BYTE> buffer(size);
    if (!app_container &&
        GetTokenInformation(token, TokenIntegrityLevel, buffer.data(), size, &size)) {
        auto label = reinterpret_cast<TOKEN_MANDATORY_LABEL*>(buffer.data());
        DWORD rid = *GetSidSubAuthority(label->Label.Sid,
                                        *GetSidSubAuthorityCount(label->Label.Sid) - 1);
        medium = rid >= SECURITY_MANDATORY_MEDIUM_RID && rid < SECURITY_MANDATORY_HIGH_RID;
    }
    CloseHandle(token);
    return medium;
}

// The spawn count of this sign-in, in a volatile key (gone at sign-out),
// read and raised under Local\yuragi-ime-spawn. Returns the decision.
static tip_logic::SpawnDecision ClaimSpawn() {
    tip_logic::SpawnFacts facts;
    facts.tag_matches = TagMatches();
    facts.maintenance = MaintenanceActive();
    facts.medium_integrity = MediumIntegrity();
    auto decision = tip_logic::DecideSpawn(facts);
    if (decision != tip_logic::SpawnDecision::Spawn) return decision;
    HANDLE mutex = CreateMutexW(nullptr, FALSE, kSpawnMutex);
    if (!mutex) return tip_logic::SpawnDecision::Spent;
    if (WaitForSingleObject(mutex, 1000) != WAIT_OBJECT_0) {
        CloseHandle(mutex);
        return tip_logic::SpawnDecision::Spent;
    }
    HKEY key = nullptr;
    if (RegCreateKeyExW(HKEY_CURRENT_USER, kSessionKey, 0, nullptr, REG_OPTION_VOLATILE,
                        KEY_QUERY_VALUE | KEY_SET_VALUE, nullptr, &key, nullptr) == ERROR_SUCCESS) {
        DWORD count = 0, size = sizeof count;
        if (RegGetValueW(key, nullptr, L"BrokerSpawns", RRF_RT_REG_DWORD, nullptr, &count, &size) !=
            ERROR_SUCCESS)
            count = 0;
        facts.spawns = count;
        decision = tip_logic::DecideSpawn(facts);
        if (decision == tip_logic::SpawnDecision::Spawn) {
            ++count;
            RegSetValueExW(key, L"BrokerSpawns", 0, REG_DWORD, (const BYTE*)&count, sizeof count);
        }
        RegCloseKey(key);
    } else {
        decision = tip_logic::SpawnDecision::Spent;
    }
    ReleaseMutex(mutex);
    CloseHandle(mutex);
    return decision;
}

// The broker beside this DLL (Program Files: nobody else can replace it),
// away from the application's job, with no handles and the user's default
// environment. A failure is not retried here (§7.3).
static bool SpawnBroker() {
    std::wstring exe = DllDir() + L"\\yuragi-broker.exe";
    std::wstring command = L"\"" + exe + L"\"";
    LPVOID environment = nullptr;
    HANDLE token = nullptr;
    if (OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, &token)) {
        CreateEnvironmentBlock(&environment, token, FALSE);
        CloseHandle(token);
    }
    STARTUPINFOW si = {};
    si.cb = sizeof si;
    PROCESS_INFORMATION pi = {};
    std::wstring dir = DllDir();
    BOOL made = CreateProcessW(exe.c_str(), command.data(), nullptr, nullptr, FALSE,
                               CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB |
                                   CREATE_UNICODE_ENVIRONMENT,
                               environment, dir.c_str(), &si, &pi);
    DWORD error = made ? 0 : GetLastError();
    if (environment) DestroyEnvironmentBlock(environment);
    if (!made) {
        Log(L"broker not started (error %lu)", error);
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    Log(L"broker started");
    return true;
}

// -- the connection (§7.2) ----------------------------------------------------------

// One pipe and one reader thread per Tip. The thread waits only for the pipe,
// the stop signal and the connect signal; nothing runs on a clock. Each line
// read is queued and the TIP's window is told; writes come from the UI thread.
class BrokerLink {
public:
    struct Item {
        enum Kind { Line, Connected, Lost, Unavailable, Connecting } kind;
        std::string line;
        Link link = Link::Connected;
    };

    BrokerLink() = default;
    ~BrokerLink() { Stop(); }

    void Start(HWND notify, UINT message) {
        notify_ = notify;
        message_ = message;
        name_ = PipeName();
        stop_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        connect_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        arrived_ = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        write_done_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        thread_ = std::thread([this] { Run(); });
    }
    void Stop() {
        if (!thread_.joinable()) return;
        SetEvent(stop_);
        thread_.join();
        for (HANDLE h : {stop_, connect_, arrived_, write_done_}) CloseHandle(h);
        stop_ = connect_ = arrived_ = write_done_ = nullptr;
    }
    void RequestConnect() { SetEvent(connect_); }
    bool Connected() {
        std::lock_guard<std::mutex> lock(pipe_lock_);
        return pipe_ != INVALID_HANDLE_VALUE;
    }
    HANDLE Arrived() const { return arrived_; }

    // From the UI thread, bounded: a write that does not finish in budget_ms
    // is cancelled and reported as failed (that key passes through).
    bool Write(const std::string& line, DWORD budget_ms) {
        std::lock_guard<std::mutex> lock(pipe_lock_);
        if (pipe_ == INVALID_HANDLE_VALUE) return false;
        return WriteTo(pipe_, write_done_, line, budget_ms);
    }

    bool Pop(Item& item) {
        std::lock_guard<std::mutex> lock(queue_lock_);
        if (queue_.empty()) return false;
        item = std::move(queue_.front());
        queue_.pop_front();
        return true;
    }

private:
    static bool WriteTo(HANDLE pipe, HANDLE done, const std::string& line, DWORD budget_ms) {
        std::string data = line + "\n";
        OVERLAPPED ov = {};
        ResetEvent(done);
        ov.hEvent = done;
        DWORD written = 0;
        if (!WriteFile(pipe, data.data(), (DWORD)data.size(), nullptr, &ov)) {
            if (GetLastError() != ERROR_IO_PENDING) return false;
            if (WaitForSingleObject(done, budget_ms) != WAIT_OBJECT_0) {
                CancelIoEx(pipe, &ov);
                GetOverlappedResult(pipe, &ov, &written, TRUE);
                return false;
            }
        }
        return GetOverlappedResult(pipe, &ov, &written, FALSE) && written == data.size();
    }

    void Push(Item item) {
        {
            std::lock_guard<std::mutex> lock(queue_lock_);
            queue_.push_back(std::move(item));
        }
        SetEvent(arrived_);
        PostMessageW(notify_, message_, 0, 0);
    }

    bool Stopping() { return WaitForSingleObject(stop_, 0) == WAIT_OBJECT_0; }

    void Run() {
        for (;;) {
            HANDLE waits[] = {stop_, connect_};
            if (WaitForMultipleObjects(2, waits, FALSE, INFINITE) != WAIT_OBJECT_0 + 1) return;
            Link why = Link::Unreachable;
            HANDLE pipe = Open(why);
            if (pipe == INVALID_HANDLE_VALUE) {
                if (Stopping()) return;
                Push({Item::Unavailable, {}, why});
                continue;
            }
            // The hello goes first; only then may the UI thread write (keys).
            HANDLE hello_done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
            bool greeted = WriteTo(pipe, hello_done, tip_logic::Hello(YURAGI_TAG),
                                   tip_logic::KEY_BUDGET_MS);
            CloseHandle(hello_done);
            if (!greeted) {
                CloseHandle(pipe);
                Push({Item::Unavailable, {}, Link::Unreachable});
                continue;
            }
            {
                std::lock_guard<std::mutex> lock(pipe_lock_);
                pipe_ = pipe;
            }
            Push({Item::Connected, {}, Link::Connected});
            Read(pipe);
            Close();
            if (Stopping()) return;
            Push({Item::Lost, {}, Link::Connecting});
        }
    }

    void Close() {
        std::lock_guard<std::mutex> lock(pipe_lock_);
        if (pipe_ != INVALID_HANDLE_VALUE) CloseHandle(pipe_);
        pipe_ = INVALID_HANDLE_VALUE;
    }

    // The pipe; when there is none, maybe start the broker (§7.3) and wait for
    // its pipe at most BROKER_WAIT_MS, looking every BROKER_POLL_MS.
    HANDLE Open(Link& why) {
        bool spawned = false;
        ULONGLONG deadline = 0;
        for (;;) {
            HANDLE pipe = CreateFileW(name_.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                                      OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
            if (pipe != INVALID_HANDLE_VALUE) return pipe;
            DWORD error = GetLastError();
            if (error == ERROR_ACCESS_DENIED) {
                why = Link::NotAllowed;  // e.g. a sandbox the pipe does not admit (§13)
                return INVALID_HANDLE_VALUE;
            }
            if (error != ERROR_FILE_NOT_FOUND && error != ERROR_PIPE_BUSY) {
                why = Link::Unreachable;
                return INVALID_HANDLE_VALUE;
            }
            if (error == ERROR_FILE_NOT_FOUND && !spawned) {
                switch (ClaimSpawn()) {
                    case tip_logic::SpawnDecision::NotAllowed:
                        why = Link::NotAllowed;
                        return INVALID_HANDLE_VALUE;
                    case tip_logic::SpawnDecision::Spent:
                        why = Link::Unreachable;
                        return INVALID_HANDLE_VALUE;
                    case tip_logic::SpawnDecision::Spawn:
                        break;
                }
                if (!SpawnBroker()) {
                    why = Link::Unreachable;
                    return INVALID_HANDLE_VALUE;
                }
                spawned = true;
                deadline = GetTickCount64() + tip_logic::BROKER_WAIT_MS;
                Push({Item::Connecting, {}, Link::Connecting});
            }
            if (!spawned) deadline = deadline ? deadline : GetTickCount64() + tip_logic::BROKER_WAIT_MS;
            if (GetTickCount64() >= deadline) {
                why = Link::Unreachable;
                return INVALID_HANDLE_VALUE;
            }
            if (WaitForSingleObject(stop_, tip_logic::BROKER_POLL_MS) == WAIT_OBJECT_0)
                return INVALID_HANDLE_VALUE;
        }
    }

    void Read(HANDLE pipe) {
        HANDLE done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        std::string buffer;
        char chunk[16384];
        for (;;) {
            OVERLAPPED ov = {};
            ov.hEvent = done;
            ResetEvent(done);
            DWORD n = 0;
            if (!ReadFile(pipe, chunk, sizeof chunk, nullptr, &ov) && GetLastError() != ERROR_IO_PENDING)
                break;
            HANDLE waits[] = {stop_, done};
            if (WaitForMultipleObjects(2, waits, FALSE, INFINITE) == WAIT_OBJECT_0) {
                CancelIoEx(pipe, &ov);
                GetOverlappedResult(pipe, &ov, &n, TRUE);
                break;
            }
            if (!GetOverlappedResult(pipe, &ov, &n, FALSE) || n == 0) break;
            buffer.append(chunk, n);
            size_t start = 0;
            for (size_t nl; (nl = buffer.find('\n', start)) != std::string::npos; start = nl + 1)
                Push({Item::Line, buffer.substr(start, nl - start), Link::Connected});
            buffer.erase(0, start);
        }
        CloseHandle(done);
    }

    HWND notify_ = nullptr;
    UINT message_ = 0;
    std::wstring name_;
    HANDLE stop_ = nullptr, connect_ = nullptr, arrived_ = nullptr, write_done_ = nullptr;
    std::mutex pipe_lock_, queue_lock_;
    HANDLE pipe_ = INVALID_HANDLE_VALUE;
    std::deque<Item> queue_;
    std::thread thread_;
};

// -- display attributes ----------------------------------------------------------------

// Three published display attributes: raw input, converted, focused bunsetsu.
static const GUID kAttributeGuids[] = {
    {0xa7c4e203, 0x0b3a, 0x4f11, {0x9e,0x61,0x0c,0x1a,0x0b,0x7e,0x0a,0x01}},
    {0xa7c4e204, 0x0b3a, 0x4f11, {0x9e,0x61,0x0c,0x1a,0x0b,0x7e,0x0a,0x01}},
    {0xa7c4e205, 0x0b3a, 0x4f11, {0x9e,0x61,0x0c,0x1a,0x0b,0x7e,0x0a,0x01}},
};
static TF_DISPLAYATTRIBUTE DefaultAttribute(int index) {
    TF_DISPLAYATTRIBUTE a{};
    a.crText.type = TF_CT_NONE; a.crBk.type = TF_CT_NONE;
    a.crLine.type = TF_CT_NONE;
    a.lsStyle = index == 0 ? TF_LS_DOT : TF_LS_SOLID;
    a.fBoldLine = index == 2;
    a.bAttr = index == 0 ? TF_ATTR_INPUT : (index == 1 ? TF_ATTR_CONVERTED : TF_ATTR_TARGET_CONVERTED);
    return a;
}
class AttributeInfo final : public ITfDisplayAttributeInfo {
    LONG refs_ = 1;
    int index_;
    TF_DISPLAYATTRIBUTE attribute_;
public:
    explicit AttributeInfo(int i) : index_(i), attribute_(DefaultAttribute(i)) { InterlockedIncrement(&g_locks); }
    ~AttributeInfo() { InterlockedDecrement(&g_locks); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id, void** pp) override {
        if (!pp) return E_POINTER;
        *pp = nullptr;
        if (id != IID_IUnknown && id != IID_ITfDisplayAttributeInfo) return E_NOINTERFACE;
        *pp = static_cast<ITfDisplayAttributeInfo*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG n = InterlockedDecrement(&refs_); if (!n) delete this; return n;
    }
    HRESULT STDMETHODCALLTYPE GetGUID(GUID* g) override {
        if (!g) return E_POINTER; *g = kAttributeGuids[index_]; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetDescription(BSTR* b) override {
        if (!b) return E_POINTER;
        const wchar_t* names[] = {L"yuragi input", L"yuragi converted", L"yuragi focused segment"};
        *b = SysAllocString(names[index_]); return *b ? S_OK : E_OUTOFMEMORY;
    }
    HRESULT STDMETHODCALLTYPE GetAttributeInfo(TF_DISPLAYATTRIBUTE* a) override {
        if (!a) return E_POINTER; *a = attribute_; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE SetAttributeInfo(const TF_DISPLAYATTRIBUTE* a) override {
        if (!a) return E_POINTER; attribute_ = *a; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Reset() override { attribute_ = DefaultAttribute(index_); return S_OK; }
};
class AttributeEnum final : public IEnumTfDisplayAttributeInfo {
    LONG refs_ = 1;
    ULONG index_ = 0;
public:
    AttributeEnum() { InterlockedIncrement(&g_locks); }
    ~AttributeEnum() { InterlockedDecrement(&g_locks); }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id, void** pp) override {
        if (!pp) return E_POINTER;
        *pp = nullptr;
        if (id != IID_IUnknown && id != IID_IEnumTfDisplayAttributeInfo) return E_NOINTERFACE;
        *pp = static_cast<IEnumTfDisplayAttributeInfo*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG n = InterlockedDecrement(&refs_); if (!n) delete this; return n;
    }
    HRESULT STDMETHODCALLTYPE Clone(IEnumTfDisplayAttributeInfo** pp) override {
        if (!pp) return E_POINTER;
        auto* e = new AttributeEnum(); e->index_ = index_; *pp = e; return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Next(ULONG count, ITfDisplayAttributeInfo** infos, ULONG* fetched) override {
        if (!infos || (!fetched && count != 1)) return E_POINTER;
        ULONG n = 0;
        while (n < count && index_ < 3) infos[n++] = new AttributeInfo((int)index_++);
        if (fetched) *fetched = n;
        return n == count ? S_OK : S_FALSE;
    }
    HRESULT STDMETHODCALLTYPE Reset() override { index_ = 0; return S_OK; }
    HRESULT STDMETHODCALLTYPE Skip(ULONG count) override {
        ULONG n = (std::min)(count, 3UL - index_); index_ += n;
        return n == count ? S_OK : S_FALSE;
    }
};

// -- the input field's kind (§7.7) -------------------------------------------------

// Password fields are not ours. Only a field that says so counts: a context
// with no selection or no input scope is not evidence of a password.
static bool PasswordField(ITfContext* ctx, TfEditCookie ec) {
    TF_SELECTION selection{};
    ULONG fetched = 0;
    if (FAILED(ctx->GetSelection(ec, TF_DEFAULT_SELECTION, 1, &selection, &fetched)) || !fetched)
        return false;
    bool password = false;
    ITfReadOnlyProperty* prop = nullptr;
    if (SUCCEEDED(ctx->GetAppProperty(GUID_PROP_INPUTSCOPE, &prop)) && prop) {
        VARIANT v;
        VariantInit(&v);
        if (SUCCEEDED(prop->GetValue(ec, selection.range, &v)) && v.vt == VT_UNKNOWN && v.punkVal) {
            ITfInputScope* scope = nullptr;
            if (SUCCEEDED(v.punkVal->QueryInterface(IID_ITfInputScope, (void**)&scope))) {
                InputScope* scopes = nullptr;
                UINT count = 0;
                if (SUCCEEDED(scope->GetInputScopes(&scopes, &count))) {
                    for (UINT i = 0; i < count; ++i)
                        if (scopes[i] == IS_PASSWORD || scopes[i] == IS_NUMERIC_PASSWORD) password = true;
                }
                CoTaskMemFree(scopes);
                scope->Release();
            }
        }
        VariantClear(&v);
        prop->Release();
    }
    selection.range->Release();
    return password;
}

// -- composition and its edits ------------------------------------------------------

struct CompositionState {
    ITfContext* ctx;
    ITfComposition* composition = nullptr;
    bool active = true, ending = false, position_pending = false, show_candidates = false;
    std::vector<Message> pending;
    explicit CompositionState(ITfContext* c) : ctx(c) { ctx->AddRef(); }
    ~CompositionState() {
        if (composition) composition->Release();
        ctx->Release();
    }
};

// Python's display offsets count code points; TSF ranges count UTF-16 units.
static LONG Utf16Offset(const std::wstring& text, int codepoints) {
    size_t i = 0;
    for (int n = 0; n < codepoints && i < text.size(); ++n) {
        wchar_t c = text[i++];
        if (c >= 0xd800 && c <= 0xdbff && i < text.size() &&
            text[i] >= 0xdc00 && text[i] <= 0xdfff) ++i;
    }
    return (LONG)i;
}

// Our popups are drawn in the host's DPI mode: in a per-monitor aware application
// (Notepad, most of Windows 11) nothing scales them, so every size is given at 96 dpi
// and scaled to the window's monitor here.
static int Px(HWND h, int pixels_at_96) {
    return MulDiv(pixels_at_96, (int)GetDpiForWindow(h), 96);
}

// Below the caret, in screen coordinates; hidden when the text is not laid out.
static void PlaceBelow(TfEditCookie ec, ITfContext* ctx, ITfRange* caret, HWND window) {
    if (!window) return;
    ITfContextView* view = nullptr;
    bool positioned = false;
    if (SUCCEEDED(ctx->GetActiveView(&view))) {
        RECT rect{};
        BOOL clipped = FALSE;
        // S_OK with no height means the text is not visible. The caret range is
        // collapsed, so a visible caret may come back zero wide (Notepad does):
        // IsRectEmpty would call that empty.
        if (SUCCEEDED(view->GetTextExt(ec, caret, &rect, &clipped)) && rect.bottom > rect.top) {
            SetWindowPos(window, HWND_TOPMOST, rect.left, rect.bottom + Px(window, 4), 0, 0,
                         SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            positioned = true;
        }
        view->Release();
    }
    if (!positioned) ShowWindow(window, SW_HIDE);
}

using RetiredStates = std::vector<std::shared_ptr<CompositionState>>;

class EditSession final : public ITfEditSession {
public:
    enum Kind { Apply, Finalize, Position };
    EditSession(Kind kind, std::shared_ptr<CompositionState> state, ITfCompositionSink* sink,
                const TfGuidAtom* atoms, HWND* candidate, RetiredStates* retired)
        : kind_(kind), state_(std::move(state)), sink_(sink), candidate_(candidate), retired_(retired) {
        sink_->AddRef();
        for (int i = 0; i < 3; ++i) atoms_[i] = atoms[i];
    }
    ~EditSession() {
        if (kind_ == Position) state_->position_pending = false;
        sink_->Release();
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** pp) override {
        if (!pp) return E_POINTER;
        *pp = nullptr;
        if (riid != IID_IUnknown && riid != IID_ITfEditSession) return E_NOINTERFACE;
        *pp = static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG n = InterlockedDecrement(&refs_); if (!n) delete this; return n;
    }

    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie ec) override {
        if (kind_ == Position) {
            state_->position_pending = false;
            if (!state_->active || !state_->composition || !state_->show_candidates ||
                !candidate_ || !*candidate_) return S_OK;
            ITfRange* caret = nullptr;
            if (SUCCEEDED(state_->composition->GetRange(&caret))) {
                caret->Collapse(ec, TF_ANCHOR_END);
                PlaceBelow(ec, state_->ctx, caret, *candidate_);
                caret->Release();
            }
            return S_OK;
        }
        if (kind_ == Finalize) return FinalizeState(ec, state_);
        if (!state_->active) return S_OK;
        // Synchronous edits can overtake queued asynchronous cleanup: finish
        // the retired compositions of this context first (keeping their text).
        for (const auto& old : *retired_) {
            if (old->ctx == state_->ctx && old->composition) {
                HRESULT hr = FinalizeState(ec, old);
                if (FAILED(hr)) return hr;
            }
        }
        retired_->erase(std::remove_if(retired_->begin(), retired_->end(),
            [](const std::shared_ptr<CompositionState>& old) { return !old->composition; }),
            retired_->end());
        std::vector<Message> pending;
        pending.swap(state_->pending);
        for (const auto& r : pending) {
            if (!state_->active) break;
            // A converted phrase can commit and start the next composition in
            // the same reply. Apply both in order under the same edit cookie.
            HRESULT hr = S_OK;
            if (!r.commit.empty()) hr = Write(ec, Utf8ToWide(r.commit), true, r);
            if (SUCCEEDED(hr)) hr = Write(ec, Utf8ToWide(r.composition), false, r);
            if (FAILED(hr)) return hr;
        }
        return S_OK;
    }

private:
    void End(TfEditCookie ec, const std::shared_ptr<CompositionState>& state) {
        if (!state->composition) return;
        state->ending = true;
        ITfComposition* comp = state->composition;
        state->composition = nullptr;
        comp->EndComposition(ec);
        comp->Release();
        state->ending = false;
    }
    // §7.5: the text stays as shown; only the conversion ends.
    HRESULT FinalizeState(TfEditCookie ec, const std::shared_ptr<CompositionState>& state) {
        if (!state->composition) return S_OK;
        ITfRange* range = nullptr;
        HRESULT hr = state->composition->GetRange(&range);
        if (SUCCEEDED(hr)) {
            ITfProperty* prop = nullptr;
            if (SUCCEEDED(state->ctx->GetProperty(GUID_PROP_ATTRIBUTE, &prop))) {
                prop->Clear(ec, range);
                prop->Release();
            }
            range->Release();
        }
        End(ec, state);
        return S_OK;
    }
    void Attribute(TfEditCookie ec, ITfProperty* prop, ITfRange* range, TfGuidAtom atom) {
        if (atom == TF_INVALID_GUIDATOM) return;
        VARIANT v; VariantInit(&v); v.vt = VT_I4; v.lVal = (LONG)atom;
        prop->SetValue(ec, range, &v);
    }
    HRESULT Write(TfEditCookie ec, const std::wstring& text, bool commit, const Message& reply) {
        ITfContext* ctx = state_->ctx;
        ITfRange* range = nullptr;
        HRESULT hr = S_OK;
        if (state_->composition) {
            hr = state_->composition->GetRange(&range);
            if (SUCCEEDED(hr)) hr = range->SetText(ec, 0, text.data(), (LONG)text.size());
        } else if (!text.empty()) {
            ITfInsertAtSelection* ins = nullptr;
            hr = ctx->QueryInterface(IID_ITfInsertAtSelection, (void**)&ins);
            if (SUCCEEDED(hr)) {
                // Acquire ownership before inserting preedit. StartComposition
                // can return S_OK with a null composition when the app rejects it.
                hr = commit ? ins->InsertTextAtSelection(ec, 0, text.data(), (LONG)text.size(), &range) :
                              ins->InsertTextAtSelection(ec, TF_IAS_QUERYONLY, L"", 0, &range);
                ins->Release();
            }
            if (SUCCEEDED(hr) && range && !commit) {
                ITfContextComposition* cc = nullptr;
                hr = ctx->QueryInterface(IID_ITfContextComposition, (void**)&cc);
                if (SUCCEEDED(hr)) {
                    hr = cc->StartComposition(ec, range, sink_, &state_->composition);
                    cc->Release();
                    if (SUCCEEDED(hr)) {
                        hr = state_->composition ?
                            range->SetText(ec, 0, text.data(), (LONG)text.size()) : E_FAIL;
                    }
                }
            }
        }
        if (FAILED(hr) || !range) { if (range) range->Release(); return hr; }
        ITfProperty* prop = nullptr;
        if (SUCCEEDED(ctx->GetProperty(GUID_PROP_ATTRIBUTE, &prop))) {
            prop->Clear(ec, range);
            if (!commit && !text.empty()) {
                Attribute(ec, prop, range, atoms_[reply.converted ? 1 : 0]);
                for (size_t i = 0; i < reply.segments.size(); ++i) {
                    const auto& seg = reply.segments[i];
                    LONG start = Utf16Offset(text, seg.display_start);
                    LONG end = Utf16Offset(text, seg.display_end);
                    ITfRange* part = nullptr;
                    if (SUCCEEDED(range->Clone(&part))) {
                        LONG moved = 0;
                        part->Collapse(ec, TF_ANCHOR_START);
                        part->ShiftEnd(ec, end, &moved, nullptr);
                        part->ShiftStart(ec, start, &moved, nullptr);
                        Attribute(ec, prop, part, atoms_[reply.converted ? ((int)i == reply.focus ? 2 : 1) : 0]);
                        part->Release();
                    }
                }
            }
            prop->Release();
        }
        ITfRange* caret = nullptr;
        if (SUCCEEDED(range->Clone(&caret))) {
            caret->Collapse(ec, TF_ANCHOR_END);
            TF_SELECTION selection{};
            selection.range = caret; selection.style.ase = TF_AE_NONE;
            ctx->SetSelection(ec, 1, &selection);
            if (candidate_ && *candidate_ && !commit && reply.converted && !reply.candidates.empty())
                PlaceBelow(ec, ctx, caret, *candidate_);
            caret->Release();
        }
        if (commit || text.empty()) End(ec, state_);
        range->Release();
        return S_OK;
    }

    LONG refs_ = 1;
    Kind kind_;
    std::shared_ptr<CompositionState> state_;
    ITfCompositionSink* sink_;
    TfGuidAtom atoms_[3];
    HWND* candidate_;
    RetiredStates* retired_;
};

// -- the taskbar glyph (§9.1) --------------------------------------------------------

// The taskbar input indicator (Windows 8+) shows only an item whose guidItem
// is GUID_LBI_INPUTMODE, drawn from its icon. The DLL has no icon resource,
// so the glyph is rendered at run time in the taskbar's foreground colour.
static bool TaskbarIsLight() {
    DWORD value = 0, size = sizeof(value);
    return RegGetValueW(HKEY_CURRENT_USER,
               L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
               L"SystemUsesLightTheme", RRF_RT_REG_DWORD, nullptr, &value, &size) == ERROR_SUCCESS
           && value != 0;
}
// Sized for the taskbar's scale, not this application's: in a DPI-unaware application
// GetSystemMetrics answers at 96 dpi (16 px) while a 150 % taskbar shows 24 px.
static HICON MakeModeIcon(const wchar_t* glyph, bool faint) {
    UINT dpi = GetDpiForWindow(FindWindowW(L"Shell_TrayWnd", nullptr));
    int cx = GetSystemMetricsForDpi(SM_CXSMICON, dpi), cy = GetSystemMetricsForDpi(SM_CYSMICON, dpi);
    BITMAPINFO bi{};
    bi.bmiHeader.biSize = sizeof(bi.bmiHeader);
    bi.bmiHeader.biWidth = cx; bi.bmiHeader.biHeight = -cy;
    bi.bmiHeader.biPlanes = 1; bi.bmiHeader.biBitCount = 32; bi.bmiHeader.biCompression = BI_RGB;
    void* bits = nullptr;
    HDC screen = GetDC(nullptr);
    HDC dc = CreateCompatibleDC(screen);
    HBITMAP color = CreateDIBSection(screen, &bi, DIB_RGB_COLORS, &bits, nullptr, 0);
    ReleaseDC(nullptr, screen);
    if (!dc || !color || !bits) {
        if (color) DeleteObject(color);
        if (dc) DeleteDC(dc);
        return nullptr;
    }
    HGDIOBJ old_bmp = SelectObject(dc, color);
    // White glyph on black; its grey level becomes the alpha below. The font is as tall
    // as the icon; the glyph is drawn once to find its ink, then again centred on the
    // ink (centring the text line leaves it small and low, the line has leading).
    HFONT font = CreateFontW(-cy, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                             DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
                             ANTIALIASED_QUALITY, DEFAULT_PITCH, L"Yu Gothic UI");
    HGDIOBJ old_font = SelectObject(dc, font);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, RGB(255, 255, 255));
    auto draw = [&](int dx, int dy) {
        memset(bits, 0, (size_t)cx * cy * 4);
        RECT rc{dx, dy, cx + dx, cy + dy};
        DrawTextW(dc, glyph, -1, &rc, DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX);
        GdiFlush();
    };
    draw(0, 0);
    int left = cx, top = cy, right = -1, bottom = -1;
    const BYTE* ink = static_cast<const BYTE*>(bits);
    for (int y = 0; y < cy; ++y)
        for (int x = 0; x < cx; ++x, ink += 4)
            if (ink[0] | ink[1] | ink[2]) {
                left = (std::min)(left, x); right = (std::max)(right, x);
                top = (std::min)(top, y); bottom = (std::max)(bottom, y);
            }
    if (right >= 0) draw((cx - 1 - right - left) / 2, (cy - 1 - bottom - top) / 2);
    SelectObject(dc, old_font);
    DeleteObject(font);
    SelectObject(dc, old_bmp);
    DeleteDC(dc);
    BYTE fg = TaskbarIsLight() ? 0 : 255;
    auto* px = static_cast<BYTE*>(bits);
    for (int i = 0; i < cx * cy; ++i, px += 4) {
        BYTE a = (std::max)(px[0], (std::max)(px[1], px[2]));
        if (faint) a = (BYTE)(a * 2 / 5);  // "ゆ" drawn faint while preparing
        px[0] = px[1] = px[2] = fg; px[3] = a;
    }
    HBITMAP mask = CreateBitmap(cx, cy, 1, 1, nullptr);
    ICONINFO ii{};
    ii.fIcon = TRUE; ii.hbmColor = color; ii.hbmMask = mask;
    HICON icon = mask ? CreateIconIndirect(&ii) : nullptr;
    if (mask) DeleteObject(mask);
    DeleteObject(color);
    return icon;
}

// -- the one-line notice (§9.2) ------------------------------------------------------

static const wchar_t* NoticeText(Notice n) {
    switch (n) {
        case Notice::Preparing: return L"変換の準備をしています…";
        case Notice::Stopped: return L"日本語変換が止まっています。タスクバーの「!」を右クリックしてください";
        case Notice::TooLong: return L"長すぎます。いったん確定してください";
        case Notice::Reopen: return L"ゆらぎを更新しました。このアプリを開き直すと使えます";
        case Notice::None: break;
    }
    return L"";
}

// Neither the notice nor the candidate list takes focus or clicks.
static LRESULT CALLBACK PopupProc(HWND h, UINT m, WPARAM w, LPARAM l) {
    if (m == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
    if (m == WM_NCHITTEST) return HTTRANSPARENT;
    if (m == WM_DPICHANGED) {  // moved to a monitor with another scale: Windows' size for it
        const RECT* r = (const RECT*)l;
        SetWindowPos(h, nullptr, r->left, r->top, r->right - r->left, r->bottom - r->top,
                     SWP_NOZORDER | SWP_NOACTIVATE);
        InvalidateRect(h, nullptr, TRUE);
        return 0;
    }
    return DefWindowProcW(h, m, w, l);
}

class CaretSession;

class Tip final : public ITfTextInputProcessorEx,
            public ITfKeyEventSink,
            public ITfThreadMgrEventSink,
            public ITfCompositionSink,
            public ITfDisplayAttributeProvider,
            public ITfLangBarItemButton,
            public ITfSource,
            public ITfTextLayoutSink {
public:
    Tip() { InterlockedIncrement(&g_locks); }
    ~Tip() { InterlockedDecrement(&g_locks); }

    // -- IUnknown -----------------------------------------------------------------
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** pp) override {
        if (!pp) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ITfTextInputProcessor ||
            riid == IID_ITfTextInputProcessorEx) {
            *pp = static_cast<ITfTextInputProcessorEx*>(this);
        } else if (riid == IID_ITfKeyEventSink) {
            *pp = static_cast<ITfKeyEventSink*>(this);
        } else if (riid == IID_ITfThreadMgrEventSink) {
            *pp = static_cast<ITfThreadMgrEventSink*>(this);
        } else if (riid == IID_ITfCompositionSink) {
            *pp = static_cast<ITfCompositionSink*>(this);
        } else if (riid == IID_ITfDisplayAttributeProvider) {
            *pp = static_cast<ITfDisplayAttributeProvider*>(this);
        } else if (riid == IID_ITfLangBarItem || riid == IID_ITfLangBarItemButton) {
            *pp = static_cast<ITfLangBarItemButton*>(this);
        } else if (riid == IID_ITfSource) {
            *pp = static_cast<ITfSource*>(this);
        } else if (riid == IID_ITfTextLayoutSink) {
            *pp = static_cast<ITfTextLayoutSink*>(this);
        } else {
            *pp = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG n = InterlockedDecrement(&refs_);
        if (!n) delete this;
        return n;
    }

    // -- activation -----------------------------------------------------------------
    HRESULT STDMETHODCALLTYPE Activate(ITfThreadMgr* ptim, TfClientId tid) override {
        return ActivateEx(ptim, tid, 0);
    }
    HRESULT STDMETHODCALLTYPE ActivateEx(ITfThreadMgr* ptim, TfClientId tid, DWORD) override {
        tm_ = ptim;
        tm_->AddRef();
        client_ = tid;
        settings_.Load();
        g_log = settings_.log;
        ITfSource* src = nullptr;
        if (SUCCEEDED(tm_->QueryInterface(IID_ITfSource, (void**)&src))) {
            src->AdviseSink(IID_ITfThreadMgrEventSink, (ITfThreadMgrEventSink*)this, &cookie_tm_);
            src->Release();
        }
        ITfKeystrokeMgr* km = nullptr;
        if (SUCCEEDED(tm_->QueryInterface(IID_ITfKeystrokeMgr, (void**)&km))) {
            km->AdviseKeyEventSink(client_, this, TRUE);
            km->Release();
        }
        ITfCategoryMgr* cat = nullptr;
        if (SUCCEEDED(CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
                                       IID_ITfCategoryMgr, (void**)&cat))) {
            for (int i = 0; i < 3; ++i) cat->RegisterGUID(kAttributeGuids[i], &atoms_[i]);
            cat->Release();
        }
        EnsureWindow();
        SetKeyboardOpen(tm_, client_, TRUE);
        PreserveToggle();
        ITfLangBarItemMgr* lbm = nullptr;
        if (SUCCEEDED(tm_->QueryInterface(IID_ITfLangBarItemMgr, (void**)&lbm))) {
            if (SUCCEEDED(lbm->AddItem(this))) langbar_ = true;
            lbm->Release();
        }
        link_.Start(hwnd_, kLinkMessage);
        TryConnect();  // §7.2: one of the three moments a connection is tried
        CheckScope();
        Log(L"ActivateEx client=%u", (unsigned)tid);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Deactivate() override {
        FinishLocal();
        link_.Stop();
        UnadviseLayout();
        for (UINT id : {kTimerReply, kTimerPending, kTimerNotice}) KillTimer(hwnd_, id);
        for (HWND* w : {&cand_hwnd_, &notice_hwnd_, &hwnd_}) {
            if (*w) DestroyWindow(*w);
            *w = nullptr;
        }
        if (scope_ctx_) {
            scope_ctx_->Release();
            scope_ctx_ = nullptr;
        }
        if (tm_) {
            if (langbar_) {
                ITfLangBarItemMgr* lbm = nullptr;
                if (SUCCEEDED(tm_->QueryInterface(IID_ITfLangBarItemMgr, (void**)&lbm))) {
                    lbm->RemoveItem(this);
                    lbm->Release();
                }
                langbar_ = false;
            }
            ITfKeystrokeMgr* km = nullptr;
            if (SUCCEEDED(tm_->QueryInterface(IID_ITfKeystrokeMgr, (void**)&km))) {
                UnpreserveToggle(km);
                km->UnadviseKeyEventSink(client_);
                km->Release();
            }
            ITfSource* src = nullptr;
            if (cookie_tm_ != TF_INVALID_COOKIE &&
                SUCCEEDED(tm_->QueryInterface(IID_ITfSource, (void**)&src))) {
                src->UnadviseSink(cookie_tm_);
                src->Release();
            }
            tm_->Release();
            tm_ = nullptr;
        }
        client_ = TF_CLIENTID_NULL;
        cookie_tm_ = TF_INVALID_COOKIE;
        return S_OK;
    }

    // -- focus ------------------------------------------------------------------------
    HRESULT STDMETHODCALLTYPE OnSetFocus(ITfDocumentMgr*, ITfDocumentMgr*) override {
        FocusMoved();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnInitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnUninitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPushContext(ITfContext*) override { FocusMoved(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPopContext(ITfContext*) override { FocusMoved(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnSetFocus(BOOL) override { FocusMoved(); return S_OK; }

    // The application ended the composition (focus moved, etc.): its text is the app's now.
    HRESULT STDMETHODCALLTYPE OnCompositionTerminated(TfEditCookie, ITfComposition* c) override {
        if (state_ && !state_->ending && state_->composition == c) {
            state_->composition->Release();
            state_->composition = nullptr;
            FinishLocal();
            SendRequest("reset");
        }
        return S_OK;
    }

    // ITfTextLayoutSink (§7.6): the candidate list follows the text.
    HRESULT STDMETHODCALLTYPE OnLayoutChange(ITfContext*, TfLayoutCode, ITfContextView*) override {
        RequestCandidatePosition();
        return S_OK;
    }

    // -- keys (§7.4) ----------------------------------------------------------------------
    HRESULT STDMETHODCALLTYPE OnTestKeyDown(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        ReleaseToggleLatch();
        *e = CanEat(VkOf(w)) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnTestKeyUp(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        UINT vk = VkOf(w);
        *e = Eligible() && ((toggle_down_ && vk == toggle_active_vk_ && toggle_eaten_) ||
                            (!direct_ && down_eaten_[vk])) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyDown(ITfContext*, WPARAM w, LPARAM l, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        *e = FALSE;
        UINT vk = VkOf(w);
        ReleaseToggleLatch();
        if (!Eligible()) return S_OK;
        if (FocusChanged()) FocusMoved();
        if (toggle_down_ && vk == toggle_active_vk_) {
            *e = toggle_eaten_ ? TRUE : FALSE;
            return S_OK;
        }
        // The toggle precedes the Alt/Ctrl/Win exclusion; a repeat never toggles again.
        if (ToggleKey(vk)) {
            if (l & (LPARAM(1) << 30)) return S_OK;
            bool eaten = Toggle();
            toggle_down_ = true; toggle_eaten_ = eaten; toggle_active_vk_ = vk;
            down_eaten_[vk] = eaten;
            *e = eaten ? TRUE : FALSE;
            return S_OK;
        }
        Mode mode = CurrentMode();
        bool kana = !direct_ && !HeldMods().Shortcut() &&
                    (tip_logic::CharacterVk(vk) || vk == VK_SPACE);
        if (kana && !link_.Connected()) TryConnect();  // §7.2
        if (kana && mode == Mode::Stopped && !stopped_told_) {
            stopped_told_ = true;  // once per focus (§9.2)
            ShowNotice(Notice::Stopped);
        }
        if (!CanEat(vk)) return S_OK;
        bool eaten = SendKey(true, vk);
        if (eaten) {
            if (!down_eaten_[vk]) down_mods_[vk] = HeldMods();
            down_eaten_[vk] = true;
        }
        *e = eaten ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyUp(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        *e = FALSE;
        UINT vk = VkOf(w);
        if (!Eligible()) return S_OK;
        if (toggle_down_ && vk == toggle_active_vk_) {
            *e = toggle_eaten_ ? TRUE : FALSE;
            toggle_down_ = toggle_eaten_ = false;
            down_eaten_[vk] = false;
            return S_OK;
        }
        bool promised = down_eaten_[vk];
        down_eaten_[vk] = false;
        if (!promised || direct_ || !link_.Connected()) return S_OK;
        // The release carries the consumed down's modifiers, so a Ctrl pressed
        // meanwhile cannot hide it from the FSM.
        *e = SendKey(false, vk) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnPreservedKey(ITfContext*, REFGUID guid, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        *e = FALSE;
        bool alias = guid == GUID_YuragiToggleKanji;
        if (guid != GUID_YuragiToggle && !alias) return S_OK;
        if (!Eligible()) return S_OK;
        UINT vk = alias ? preserved_alias_.uVKey : settings_.toggle_vk;
        if (!ToggleKey(vk)) return S_OK;
        // Shared with the OnKeyDown path, so one press never toggles twice.
        if (!toggle_down_) {
            bool eaten = Toggle();
            toggle_down_ = true; toggle_eaten_ = eaten; toggle_active_vk_ = vk;
            down_eaten_[vk] = eaten;
        }
        *e = toggle_eaten_ ? TRUE : FALSE;
        return S_OK;
    }

    // -- display attributes ---------------------------------------------------------------
    HRESULT STDMETHODCALLTYPE EnumDisplayAttributeInfo(IEnumTfDisplayAttributeInfo** pp) override {
        if (!pp) return E_POINTER; *pp = new AttributeEnum(); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetDisplayAttributeInfo(REFGUID guid, ITfDisplayAttributeInfo** pp) override {
        if (!pp) return E_POINTER;
        *pp = nullptr;
        for (int i = 0; i < 3; ++i) if (guid == kAttributeGuids[i]) {
            *pp = new AttributeInfo(i); return S_OK;
        }
        return E_INVALIDARG;
    }

    // -- the taskbar item (§9.1, §9.4) ----------------------------------------------------
    HRESULT STDMETHODCALLTYPE GetInfo(TF_LANGBARITEMINFO* info) override {
        if (!info) return E_INVALIDARG;
        info->clsidService = CLSID_YuragiTip;
        info->guidItem = GUID_LBI_INPUTMODE;
        info->dwStyle = TF_LBI_STYLE_BTN_BUTTON | TF_LBI_STYLE_SHOWNINTRAY;
        info->ulSort = 0;
        wcsncpy_s(info->szDescription, ARRAYSIZE(info->szDescription),
                  L"ゆらぎ 入力モード", _TRUNCATE);
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetStatus(DWORD* status) override {
        if (!status) return E_INVALIDARG;
        *status = 0;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE Show(BOOL) override { return E_NOTIMPL; }
    HRESULT STDMETHODCALLTYPE GetTooltipString(BSTR* tip) override {
        if (!tip) return E_INVALIDARG;
        const wchar_t* text = L"";
        switch (CurrentMode()) {
            case Mode::Normal:
                text = direct_ ? L"ゆらぎ: 直接入力（クリックでかな入力）"
                               : L"ゆらぎ: かな入力（クリックで直接入力）";
                break;
            case Mode::Preparing: text = L"ゆらぎ: 変換の準備をしています"; break;
            case Mode::Stopped: text = L"ゆらぎ: 日本語変換が止まっています（右クリックで操作）"; break;
            case Mode::Excluded: text = L"ゆらぎ: このアプリでは日本語変換を使えません"; break;
        }
        *tip = SysAllocString(text);
        return *tip ? S_OK : E_OUTOFMEMORY;
    }
    // The menu's items (§9.4); grayed: shown but not selectable.
    const wchar_t* MenuText(int item, bool* grayed) const {
        *grayed = false;
        switch (item) {
            case tip_logic::MENU_TOGGLE: return direct_ ? L"かな入力に切り替え" : L"直接入力に切り替え";
            case tip_logic::MENU_ADMIN: return L"設定を開く";
            case tip_logic::MENU_RETRY: return L"もう一度試す";
            case tip_logic::MENU_REASON: return L"原因を表示";
            case tip_logic::MENU_PREPARING:
                *grayed = true;
                return L"（変換の準備をしています）";
        }
        return L"";
    }
    HRESULT STDMETHODCALLTYPE OnClick(TfLBIClick click, POINT pt, const RECT*) override {
        if (click == TF_LBI_CLK_LEFT && CurrentMode() == Mode::Normal && Eligible()) Toggle();
        // The taskbar input indicator hands a right click over as OnClick and never
        // calls InitMenu, so the menu is shown here (owned by this thread's focus window).
        if (click == TF_LBI_CLK_RIGHT) {
            HMENU popup = CreatePopupMenu();
            for (int item : tip_logic::MenuFor(CurrentMode())) {
                bool grayed;
                const wchar_t* text = MenuText(item, &grayed);
                AppendMenuW(popup, MF_STRING | (grayed ? MF_GRAYED : 0), item, text);
            }
            UINT align = GetSystemMetrics(SM_MENUDROPALIGNMENT) ? TPM_RIGHTALIGN : TPM_LEFTALIGN;
            UINT id = (UINT)TrackPopupMenuEx(popup, align | TPM_BOTTOMALIGN | TPM_RETURNCMD |
                                                 TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                             pt.x, pt.y, GetFocus(), nullptr);
            DestroyMenu(popup);
            if (id) OnMenuSelect(id);
        }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE InitMenu(ITfMenu* menu) override {
        if (!menu) return E_INVALIDARG;
        for (int item : tip_logic::MenuFor(CurrentMode())) {
            bool grayed;
            const wchar_t* text = MenuText(item, &grayed);
            menu->AddMenuItem(item, grayed ? TF_LBMENUF_GRAYED : 0, nullptr, nullptr, text,
                              (ULONG)wcslen(text), nullptr);
        }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnMenuSelect(UINT id) override {
        switch (id) {
            case tip_logic::MENU_TOGGLE:
                if (Eligible()) Toggle();
                break;
            case tip_logic::MENU_ADMIN:
                // §9.4: the broker opens it (the TIP starts no Python).
                SendOp("open_admin");
                break;
            case tip_logic::MENU_RETRY:
                if (link_.Connected()) SendOp("retry");
                else TryConnect(true);
                break;
            case tip_logic::MENU_REASON:
                SendOp("show_reason");
                break;
        }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetIcon(HICON* icon) override {
        if (!icon) return E_INVALIDARG;
        Mode mode = CurrentMode();
        *icon = MakeModeIcon(Utf8ToWide(tip_logic::Glyph(mode, direct_)).c_str(),
                             tip_logic::Faint(mode));  // the caller destroys it
        return *icon ? S_OK : E_FAIL;
    }
    HRESULT STDMETHODCALLTYPE GetText(BSTR* text) override {
        if (!text) return E_INVALIDARG;
        *text = SysAllocString(Utf8ToWide(tip_logic::Glyph(CurrentMode(), direct_)).c_str());
        return *text ? S_OK : E_OUTOFMEMORY;
    }
    // ITfSource: the language bar advises its item sink here.
    HRESULT STDMETHODCALLTYPE AdviseSink(REFIID riid, IUnknown* unk, DWORD* cookie) override {
        if (!unk || !cookie) return E_INVALIDARG;
        if (riid != IID_ITfLangBarItemSink) return CONNECT_E_CANNOTCONNECT;
        if (lb_sink_) return CONNECT_E_ADVISELIMIT;
        if (FAILED(unk->QueryInterface(IID_ITfLangBarItemSink, (void**)&lb_sink_))) {
            lb_sink_ = nullptr;
            return E_NOINTERFACE;
        }
        *cookie = kLangBarCookie;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE UnadviseSink(DWORD cookie) override {
        if (cookie != kLangBarCookie || !lb_sink_) return CONNECT_E_NOCONNECTION;
        lb_sink_->Release();
        lb_sink_ = nullptr;
        return S_OK;
    }

    // Called by the scope and caret edit sessions.
    void ScopeChecked(ITfContext* ctx, bool password) {
        if (ctx != scope_ctx_) return;  // the focus moved on meanwhile
        scope_ = password ? Scope::Password : Scope::Allowed;
    }
    void PlaceNotice(TfEditCookie ec, ITfContext* ctx, ITfRange* caret) {
        if (notice_ != Notice::None) PlaceBelow(ec, ctx, caret, notice_hwnd_);
    }
    ITfCompositionSink* Sink() { return this; }
    std::shared_ptr<CompositionState> State() { return state_; }

private:
    enum class Scope { Unknown, Allowed, Password };
    static const UINT kLinkMessage = WM_APP + 1;
    static const UINT_PTR kTimerReply = 1, kTimerPending = 2, kTimerNotice = 3;
    static const DWORD kLangBarCookie = 0x6f79;

    // -- the TIP's window: link messages and its one-shot timers ---------------------------
    static LRESULT CALLBACK WindowProc(HWND h, UINT m, WPARAM w, LPARAM l) {
        auto* self = (Tip*)GetWindowLongPtrW(h, GWLP_USERDATA);
        if (self && m == kLinkMessage) {
            self->Drain(false, 0, nullptr);
            return 0;
        }
        if (self && m == WM_TIMER) {
            KillTimer(h, w);  // every timer here rings once
            self->OnTimer((UINT_PTR)w);
            return 0;
        }
        return DefWindowProcW(h, m, w, l);
    }

    void EnsureWindow() {
        if (hwnd_) return;
        WNDCLASSW wc{};
        wc.lpfnWndProc = WindowProc;
        wc.hInstance = g_hinst;
        wc.lpszClassName = L"YuragiTip";
        RegisterClassW(&wc);
        hwnd_ = CreateWindowExW(0, wc.lpszClassName, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr,
                                g_hinst, nullptr);
        SetWindowLongPtrW(hwnd_, GWLP_USERDATA, (LONG_PTR)this);
    }

    void OnTimer(UINT_PTR id) {
        if (id == kTimerReply) {
            // §7.6: the FSM's chord deadline, or the LM's next look.
            if (!direct_) SendRequest("timeout", "\"t\":" + std::to_string(NowMs()));
        } else if (id == kTimerPending) {
            if (in_flight_.Waiting() && NowMs() >= in_flight_.NoticeAt()) ShowNotice(Notice::Preparing);
        } else if (id == kTimerNotice) {
            HideNotice();
        }
    }

    void ArmTimer(UINT_PTR id, ULONGLONG at) {
        ULONGLONG now = NowMs();
        SetTimer(hwnd_, id, at > now ? (UINT)(std::min<ULONGLONG>)(at - now, 0x7fffffff) : 0, nullptr);
    }

    // -- connection -------------------------------------------------------------------------
    void TryConnect(bool user = false) {
        if (link_.Connected()) return;
        if (!user && !connect_gate_.Allow(NowMs())) return;
        if (user) connect_gate_.Allow(NowMs());
        link_.RequestConnect();
    }

    Mode CurrentMode() const { return tip_logic::ModeFor(link_state_, broker_state_); }

    // Lines and link changes, in order. sync: inside a key event (edits may
    // be synchronous). Returns whether the reply for `wanted` was handled;
    // that reply is copied to *reply (the caller decides the key with it).
    bool Drain(bool sync, long long wanted, Message* reply) {
        bool found = false;
        BrokerLink::Item item;
        Mode before = CurrentMode();
        while (link_.Pop(item)) {
            switch (item.kind) {
                case BrokerLink::Item::Connected:
                    link_state_ = Link::Connected;
                    broker_state_.clear();
                    in_flight_.Clear();
                    break;
                case BrokerLink::Item::Connecting:
                    link_state_ = Link::Connecting;
                    break;
                case BrokerLink::Item::Unavailable:
                    link_state_ = item.link;
                    break;
                case BrokerLink::Item::Lost:
                    // §7.5: the broker is gone; what is shown stays.
                    Log(L"broker connection lost");
                    link_state_ = Link::Connecting;
                    broker_state_.clear();
                    in_flight_.Clear();
                    FinishLocal();
                    break;
                case BrokerLink::Item::Line: {
                    Message m;
                    if (!tip_logic::ParseMessage(item.line, m)) break;
                    if (Handle(m, sync, wanted)) {
                        found = true;
                        if (reply) *reply = m;
                    }
                    break;
                }
            }
            if (found) break;  // the caller applies the wanted reply itself
        }
        if (CurrentMode() != before) UpdateLangBar();
        if (!in_flight_.Waiting() && notice_ == Notice::Preparing) HideNotice();
        return found;
    }

    // One message from the broker. Returns true when it is the reply `wanted`.
    bool Handle(const Message& m, bool sync, long long wanted) {
        if (m.hello || m.event == "state") {
            broker_state_ = m.state;
            if (m.state == "incompatible" && !reopen_told_) {
                reopen_told_ = true;  // once per focus (§9.2)
                ShowNotice(Notice::Reopen);
            }
        }
        if (tip_logic::CommitsVisibleText(m)) FinishLocal();
        if (!m.has_seq) return false;
        if (!in_flight_.Answer(m.seq)) return false;  // a request we gave up or never sent
        if (!m.engine) return false;  // {"seq","state"}: not processed; handled above
        // Sent for the field the focus has left: its text was committed there;
        // writing this reply now would put it into the new field.
        if (m.seq <= stale_upto_) return false;
        if (m.error) {
            NoteReply(m);  // the engine started a clean session; the text stays (§7.5)
            return false;
        }
        if (m.seq == wanted) return true;
        NoteReply(m);
        ApplyReply(m, sync);
        return false;
    }

    // -- sending ---------------------------------------------------------------------------
    // A numbered request whose reply is applied when it comes (timer polls,
    // resets). Only while keys go to the broker at all.
    void SendRequest(const std::string& op, const std::string& extra = "") {
        if (!link_.Connected() || !tip_logic::SendsKeys(CurrentMode())) return;
        long long seq = in_flight_.Next(NowMs());
        if (!link_.Write(tip_logic::OpRequest(seq, op, extra), tip_logic::KEY_BUDGET_MS))
            in_flight_.Answer(seq);
    }
    // A request to the broker itself (retry, open_admin, show_reason): no reply.
    void SendOp(const char* op) {
        link_.Write(std::string("{\"op\":\"") + op + "\"}", tip_logic::KEY_BUDGET_MS);
    }

    // One key: sent with its number; its reply is awaited at most KEY_BUDGET_MS.
    // In time, it is applied at once and decides the key. Late, the key is
    // eaten as OnTestKeyDown said, nothing on screen changes, the connection
    // stays, and the reply is applied when it arrives (§7.4.2).
    bool SendKey(bool down, UINT vk) {
        tip_logic::Local before = LocalState();
        bool before_timer = timer_pending_;
        int before_tokens = n_tokens_;
        tip_logic::Mods mods = down ? HeldMods() : down_mods_[vk];
        ULONGLONG start = NowMs();
        long long seq = in_flight_.Next(start);
        if (!link_.Write(tip_logic::KeyRequest(seq, down, vk, start, mods), tip_logic::KEY_BUDGET_MS)) {
            in_flight_.Answer(seq);
            return false;  // not sent: the key goes to the application
        }
        ULONGLONG deadline = start + tip_logic::KEY_BUDGET_MS;
        Message r;
        for (;;) {
            if (Drain(true, seq, &r)) break;
            // Answered without the engine ("down") or with an error: the key
            // was taken as OnTestKeyDown said; the visible text was committed.
            if (!in_flight_.Waiting(seq)) return true;
            ULONGLONG now = NowMs();
            if (now >= deadline) {
                ArmTimer(kTimerPending, in_flight_.NoticeAt());
                return true;
            }
            WaitForSingleObject(link_.Arrived(), (DWORD)(deadline - now));
        }
        bool eat = tip_logic::EatAfterReply(down, vk, mods, before, before_timer, before_tokens, r);
        NoteReply(r);
        if (settings_.log_text)
            Log(L"%s vk=%u comp=%s commit=%s", down ? L"down" : L"up", vk,
                Utf8ToWide(r.composition).c_str(), Utf8ToWide(r.commit).c_str());
        ApplyReply(r, true);
        return eat;
    }

    bool Toggle() {
        if (!tip_logic::SendsKeys(CurrentMode())) return false;
        direct_ = !direct_;  // shown at once; the engine follows in order
        FinishLocal();
        UpdateLangBar();
        long long seq = in_flight_.Next(NowMs());
        std::string request = tip_logic::OpRequest(seq, "set_direct",
                                                   direct_ ? "\"direct\":true" : "\"direct\":false");
        if (!link_.Write(request, tip_logic::KEY_BUDGET_MS)) in_flight_.Answer(seq);
        return true;
    }

    // -- replies ------------------------------------------------------------------------------
    tip_logic::Local LocalState() const {
        tip_logic::Local l;
        l.composing = composing_;
        l.converted = converted_;
        l.candidates = !cands_.empty() || pending_;
        l.awaiting = in_flight_.Waiting();
        return l;
    }

    void NoteReply(const Message& r) {
        bool mode_changed = direct_ != r.direct;
        direct_ = r.direct;
        if (mode_changed) UpdateLangBar();
        pending_ = r.pending;
        n_tokens_ = r.n_tokens;
        composing_ = !r.composition.empty() || r.n_tokens > 0 || r.has_timer;
        converted_ = r.converted || r.pending;
        timer_pending_ = r.has_timer && !r.direct;
        cands_.clear();
        for (const auto& c : r.candidates) cands_.push_back(Utf8ToWide(c));
        cand_index_ = r.cand_index;
        if (!r.direct && r.converted && !r.candidates.empty()) ShowCands();
        else HideCands();
        if (timer_pending_) ArmTimer(kTimerReply, r.timer);
        else KillTimer(hwnd_, kTimerReply);
        if (r.full) ShowNotice(Notice::TooLong);
        else if (notice_ == Notice::TooLong && (!r.commit.empty() || !composing_)) HideNotice();
    }

    void ApplyReply(const Message& r, bool sync) {
        ITfContext* ctx = FocusContext();
        if (!ctx) return;
        if (state_ && state_->ctx != ctx) {
            ctx->Release();
            FocusMoved();
            return;
        }
        if (!state_) {
            state_ = std::make_shared<CompositionState>(ctx);
            AdviseLayout(ctx);
        }
        state_->show_candidates = !r.direct && r.converted && !r.candidates.empty();
        ctx->Release();
        state_->pending.push_back(r);
        RequestEdit(EditSession::Apply, state_, sync);
    }

    void RequestEdit(EditSession::Kind kind, std::shared_ptr<CompositionState> state, bool sync) {
        auto* es = new EditSession(kind, state, this, atoms_, &cand_hwnd_, &retired_);
        HRESULT result = S_OK;
        DWORD flags = (kind == EditSession::Position ? TF_ES_READ : TF_ES_READWRITE) |
                      (sync ? TF_ES_SYNC : TF_ES_ASYNCDONTCARE);
        HRESULT hr = state->ctx->RequestEditSession(client_, es, flags, &result);
        if (sync && (hr == TF_E_SYNCHRONOUS || result == TF_E_SYNCHRONOUS))
            hr = state->ctx->RequestEditSession(client_, es, (flags & ~TF_ES_SYNC) | TF_ES_ASYNCDONTCARE,
                                                &result);
        // The application may refuse an edit (a read-only field): nothing more
        // is done about it; the connection is the broker's business, not the app's.
        if (FAILED(hr) || FAILED(result)) {
            state->pending.clear();
            Log(L"edit refused hr=%08lx result=%08lx", hr, result);
        }
        es->Release();
    }

    void RequestCandidatePosition() {
        if (!state_ || !state_->active || !state_->show_candidates || state_->position_pending) return;
        state_->position_pending = true;
        RequestEdit(EditSession::Position, state_, false);
    }

    // §7.5: the conversion ends with its text on screen; local state starts over.
    void FinishLocal() {
        auto old = state_;
        state_.reset();
        UnadviseLayout();
        if (old) {
            old->active = false;
            old->pending.clear();
            retired_.push_back(old);
            if (old->composition && client_ != TF_CLIENTID_NULL) RequestEdit(EditSession::Finalize, old, true);
        }
        retired_.erase(std::remove_if(retired_.begin(), retired_.end(),
            [](const std::shared_ptr<CompositionState>& o) { return !o->composition; }), retired_.end());
        composing_ = converted_ = pending_ = timer_pending_ = false;
        n_tokens_ = 0;
        for (bool& eaten : down_eaten_) eaten = false;
        toggle_down_ = toggle_eaten_ = false;
        if (hwnd_) KillTimer(hwnd_, kTimerReply);
        HideCands();
        if (notice_ == Notice::TooLong) HideNotice();
    }

    // A new field: what was being converted is kept as typed, the engine
    // forgets it, and the field's kind is looked up once (§7.7).
    void FocusMoved() {
        FinishLocal();
        stale_upto_ = in_flight_.Last();
        SendRequest("reset");
        stopped_told_ = reopen_told_ = false;
        HideNotice();
        CheckScope();
    }

    ITfContext* FocusContext() {
        ITfDocumentMgr* dm = nullptr;
        if (!tm_ || FAILED(tm_->GetFocus(&dm)) || !dm) return nullptr;
        ITfContext* ctx = nullptr;
        dm->GetTop(&ctx);
        dm->Release();
        return ctx;
    }
    bool FocusChanged() {
        ITfContext* ctx = FocusContext();
        bool changed = ctx != scope_ctx_;
        if (ctx) ctx->Release();
        return changed;
    }

    void CheckScope();

    // -- eating ------------------------------------------------------------------------------
    bool Eligible() { return tm_ && !KeyboardDisabled(tm_) && !KeyboardClosed(tm_); }
    bool CanEat(UINT vk) {
        if (!Eligible() || scope_ != Scope::Allowed) return false;
        if (ToggleKey(vk) || (toggle_down_ && vk == toggle_active_vk_))
            return tip_logic::SendsKeys(CurrentMode());
        if (direct_ || !link_.Connected() || !tip_logic::SendsKeys(CurrentMode())) return false;
        return tip_logic::WantKey(vk, HeldMods(), LocalState());
    }

    // -- the toggle key ------------------------------------------------------------------------
    static UINT ToggleAlias(UINT vk, unsigned mods) {
        return vk == VK_OEM_3 && (mods & 4) ? VK_KANJI : 0;
    }
    bool ToggleKey(UINT vk) const {
        bool ours = vk == settings_.toggle_vk ||
                    (vk != 0 && vk == ToggleAlias(settings_.toggle_vk, settings_.toggle_mods));
        return ours && ModMask(HeldMods()) == settings_.toggle_mods;
    }
    // §7.6: a preserved key's release may never reach OnKeyUp; look at the key
    // state on the next key instead of on a timer.
    void ReleaseToggleLatch() {
        if (toggle_down_ && !(GetKeyState(toggle_active_vk_) & 0x8000)) {
            down_eaten_[toggle_active_vk_] = false;
            toggle_down_ = toggle_eaten_ = false;
        }
    }
    void PreserveToggle() {
        ITfKeystrokeMgr* km = nullptr;
        if (FAILED(tm_->QueryInterface(IID_ITfKeystrokeMgr, (void**)&km))) return;
        preserved_toggle_ = {settings_.toggle_vk, 0};
        // tip.json uses shift=1/ctrl=2/alt=4, unlike TSF's modifier flags.
        if (settings_.toggle_mods & 1) preserved_toggle_.uModifiers |= TF_MOD_SHIFT;
        if (settings_.toggle_mods & 2) preserved_toggle_.uModifiers |= TF_MOD_CONTROL;
        if (settings_.toggle_mods & 4) preserved_toggle_.uModifiers |= TF_MOD_ALT;
        // TSF has no Win modifier flag; ToggleKey checks the full mask.
        toggle_preserved_ = SUCCEEDED(km->PreserveKey(client_, GUID_YuragiToggle,
                                                      &preserved_toggle_, nullptr, 0));
        if (UINT alias = ToggleAlias(settings_.toggle_vk, settings_.toggle_mods)) {
            preserved_alias_ = {alias, preserved_toggle_.uModifiers};
            alias_preserved_ = SUCCEEDED(km->PreserveKey(client_, GUID_YuragiToggleKanji,
                                                         &preserved_alias_, nullptr, 0));
        }
        km->Release();
    }
    void UnpreserveToggle(ITfKeystrokeMgr* km) {
        if (alias_preserved_) km->UnpreserveKey(GUID_YuragiToggleKanji, &preserved_alias_);
        if (toggle_preserved_) km->UnpreserveKey(GUID_YuragiToggle, &preserved_toggle_);
        alias_preserved_ = toggle_preserved_ = false;
    }

    // -- the candidate list and the notice --------------------------------------------------------
    static LRESULT CALLBACK CandProc(HWND h, UINT m, WPARAM w, LPARAM l) {
        if (m == WM_PAINT) {
            auto* self = (Tip*)GetWindowLongPtrW(h, GWLP_USERDATA);
            PAINTSTRUCT ps;
            HDC hdc = BeginPaint(h, &ps);
            RECT rc;
            GetClientRect(h, &rc);
            FillRect(hdc, &rc, (HBRUSH)GetStockObject(WHITE_BRUSH));
            if (self) {
                HFONT font = CreateFontW(Px(h, 18), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                         SHIFTJIS_CHARSET, 0, 0, 0, 0, L"Yu Gothic UI");
                HGDIOBJ old = SelectObject(hdc, font);
                const int row = Px(h, 22);
                int y = Px(h, 2);
                for (size_t i = 0; i < self->cands_.size(); ++i) {
                    RECT line = {Px(h, 4), y, rc.right - Px(h, 4), y + row};
                    wchar_t buf[256];
                    swprintf_s(buf, L"%d. %s", (int)i + 1, self->cands_[i].c_str());
                    if ((int)i == self->cand_index_) {
                        SetBkColor(hdc, RGB(0x20, 0x60, 0xC0));
                        SetTextColor(hdc, RGB(255, 255, 255));
                    } else {
                        SetBkColor(hdc, RGB(255, 255, 255));
                        SetTextColor(hdc, RGB(0, 0, 0));
                    }
                    ExtTextOutW(hdc, Px(h, 6), y + Px(h, 2), ETO_OPAQUE, &line, buf,
                                (UINT)wcslen(buf), nullptr);
                    y += row;
                }
                SelectObject(hdc, old);
                DeleteObject(font);
            }
            EndPaint(h, &ps);
            return 0;
        }
        return PopupProc(h, m, w, l);
    }
    static LRESULT CALLBACK NoticeProc(HWND h, UINT m, WPARAM w, LPARAM l) {
        if (m == WM_PAINT) {
            auto* self = (Tip*)GetWindowLongPtrW(h, GWLP_USERDATA);
            PAINTSTRUCT ps;
            HDC hdc = BeginPaint(h, &ps);
            RECT rc;
            GetClientRect(h, &rc);
            FillRect(hdc, &rc, (HBRUSH)GetStockObject(WHITE_BRUSH));
            if (self) {
                HFONT font = CreateFontW(Px(h, 16), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                         SHIFTJIS_CHARSET, 0, 0, 0, 0, L"Yu Gothic UI");
                HGDIOBJ old = SelectObject(hdc, font);
                SetBkMode(hdc, TRANSPARENT);
                SetTextColor(hdc, RGB(0x30, 0x30, 0x30));
                RECT text = {Px(h, 6), Px(h, 2), rc.right - Px(h, 6), rc.bottom - Px(h, 2)};
                DrawTextW(hdc, NoticeText(self->notice_), -1, &text,
                          DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX);
                SelectObject(hdc, old);
                DeleteObject(font);
            }
            EndPaint(h, &ps);
            return 0;
        }
        return PopupProc(h, m, w, l);
    }
    HWND Popup(const wchar_t* cls, WNDPROC proc) {
        WNDCLASSW wc{};
        wc.lpfnWndProc = proc;
        wc.hInstance = g_hinst;
        wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
        wc.lpszClassName = cls;
        RegisterClassW(&wc);
        HWND h = CreateWindowExW(WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_TRANSPARENT,
                                 cls, L"", WS_POPUP | WS_BORDER, 0, 0, 280, 24, nullptr, nullptr,
                                 g_hinst, nullptr);
        SetWindowLongPtrW(h, GWLP_USERDATA, (LONG_PTR)this);
        return h;
    }
    void HideCands() {
        if (cand_hwnd_) ShowWindow(cand_hwnd_, SW_HIDE);
        cands_.clear();
        cand_index_ = 0;
    }
    void ShowCands() {
        if (cands_.empty()) {
            HideCands();
            return;
        }
        if (!cand_hwnd_) cand_hwnd_ = Popup(L"YuragiCand", CandProc);
        int rows = (int)cands_.size();
        // Placement and showing happen under an edit cookie (GetTextExt).
        SetWindowPos(cand_hwnd_, nullptr, 0, 0, Px(cand_hwnd_, 280),
                     Px(cand_hwnd_, 6 + rows * 22),
                     SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
        InvalidateRect(cand_hwnd_, nullptr, TRUE);
    }
    void ShowNotice(Notice n);
    void HideNotice() {
        notice_ = Notice::None;
        if (notice_hwnd_) ShowWindow(notice_hwnd_, SW_HIDE);
        if (hwnd_) KillTimer(hwnd_, kTimerNotice);
    }

    // -- layout sink ----------------------------------------------------------------------------
    void AdviseLayout(ITfContext* ctx) {
        UnadviseLayout();
        ITfSource* src = nullptr;
        if (SUCCEEDED(ctx->QueryInterface(IID_ITfSource, (void**)&src))) {
            if (SUCCEEDED(src->AdviseSink(IID_ITfTextLayoutSink, (ITfTextLayoutSink*)this,
                                          &layout_cookie_))) {
                layout_ctx_ = ctx;
                layout_ctx_->AddRef();
            }
            src->Release();
        }
    }
    void UnadviseLayout() {
        if (!layout_ctx_) return;
        ITfSource* src = nullptr;
        if (SUCCEEDED(layout_ctx_->QueryInterface(IID_ITfSource, (void**)&src))) {
            src->UnadviseSink(layout_cookie_);
            src->Release();
        }
        layout_ctx_->Release();
        layout_ctx_ = nullptr;
        layout_cookie_ = TF_INVALID_COOKIE;
    }

    void UpdateLangBar() {
        if (lb_sink_) lb_sink_->OnUpdate(TF_LBI_ICON | TF_LBI_TEXT | TF_LBI_TOOLTIP);
    }

    LONG refs_ = 1;
    ITfThreadMgr* tm_ = nullptr;
    TfClientId client_ = TF_CLIENTID_NULL;
    DWORD cookie_tm_ = TF_INVALID_COOKIE;
    TfGuidAtom atoms_[3] = {TF_INVALID_GUIDATOM, TF_INVALID_GUIDATOM, TF_INVALID_GUIDATOM};
    TipConfig settings_;
    std::shared_ptr<CompositionState> state_;
    RetiredStates retired_;
    BrokerLink link_;
    Link link_state_ = Link::Connecting;
    std::string broker_state_;
    tip_logic::InFlight in_flight_;
    long long stale_upto_ = 0;  // replies up to this number belong to a field left behind
    tip_logic::ConnectGate connect_gate_;
    HWND hwnd_ = nullptr, cand_hwnd_ = nullptr, notice_hwnd_ = nullptr;
    Scope scope_ = Scope::Unknown;
    ITfContext* scope_ctx_ = nullptr;
    ITfContext* layout_ctx_ = nullptr;
    DWORD layout_cookie_ = TF_INVALID_COOKIE;
    Notice notice_ = Notice::None;
    bool stopped_told_ = false, reopen_told_ = false;
    bool direct_ = false;
    bool composing_ = false, converted_ = false, pending_ = false, timer_pending_ = false;
    int n_tokens_ = 0;
    std::vector<std::wstring> cands_;
    int cand_index_ = 0;
    bool down_eaten_[256]{};
    tip_logic::Mods down_mods_[256]{};
    bool toggle_down_ = false, toggle_eaten_ = false;
    UINT toggle_active_vk_ = 0;
    TF_PRESERVEDKEY preserved_toggle_{}, preserved_alias_{};
    bool toggle_preserved_ = false, alias_preserved_ = false;
    bool langbar_ = false;
    ITfLangBarItemSink* lb_sink_ = nullptr;
};

// Read-only sessions: the field's kind (§7.7) and the caret for the notice (§9.2).
class ReadSession final : public ITfEditSession {
public:
    enum Kind { Scope, Caret };
    ReadSession(Kind kind, Tip* tip, ITfContext* ctx) : kind_(kind), tip_(tip), ctx_(ctx) {
        tip_->AddRef();
        ctx_->AddRef();
    }
    ~ReadSession() {
        ctx_->Release();
        tip_->Release();
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID id, void** pp) override {
        if (!pp) return E_POINTER;
        *pp = nullptr;
        if (id != IID_IUnknown && id != IID_ITfEditSession) return E_NOINTERFACE;
        *pp = static_cast<ITfEditSession*>(this); AddRef(); return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG n = InterlockedDecrement(&refs_); if (!n) delete this; return n;
    }
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie ec) override {
        if (kind_ == Scope) {
            tip_->ScopeChecked(ctx_, PasswordField(ctx_, ec));
            return S_OK;
        }
        ITfRange* caret = nullptr;
        auto state = tip_->State();
        if (state && state->composition && state->ctx == ctx_) {
            if (SUCCEEDED(state->composition->GetRange(&caret))) caret->Collapse(ec, TF_ANCHOR_END);
        } else {
            TF_SELECTION selection{};
            ULONG fetched = 0;
            if (SUCCEEDED(ctx_->GetSelection(ec, TF_DEFAULT_SELECTION, 1, &selection, &fetched)) &&
                fetched)
                caret = selection.range;
        }
        if (caret) {
            tip_->PlaceNotice(ec, ctx_, caret);
            caret->Release();
        }
        return S_OK;
    }

private:
    LONG refs_ = 1;
    Kind kind_;
    Tip* tip_;
    ITfContext* ctx_;
};

// §7.7: once per focus. Until the answer is known the field is "being looked
// at" and keys pass through.
void Tip::CheckScope() {
    if (scope_ctx_) scope_ctx_->Release();
    scope_ctx_ = FocusContext();
    scope_ = Scope::Unknown;
    if (!scope_ctx_ || client_ == TF_CLIENTID_NULL) return;
    auto* es = new ReadSession(ReadSession::Scope, this, scope_ctx_);
    HRESULT result = S_OK;
    HRESULT hr = scope_ctx_->RequestEditSession(client_, es, TF_ES_SYNC | TF_ES_READ, &result);
    if (hr == TF_E_SYNCHRONOUS || result == TF_E_SYNCHRONOUS)
        scope_ctx_->RequestEditSession(client_, es, TF_ES_ASYNC | TF_ES_READ, &result);
    es->Release();
}

void Tip::ShowNotice(Notice n) {
    if (!hwnd_) return;
    notice_ = n;
    if (!notice_hwnd_) notice_hwnd_ = Popup(L"YuragiNotice", NoticeProc);
    HDC dc = GetDC(notice_hwnd_);
    HFONT font = CreateFontW(Px(notice_hwnd_, 16), 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                             SHIFTJIS_CHARSET, 0, 0, 0, 0, L"Yu Gothic UI");
    HGDIOBJ old = SelectObject(dc, font);
    SIZE size{};
    const wchar_t* text = NoticeText(n);
    GetTextExtentPoint32W(dc, text, (int)wcslen(text), &size);
    SelectObject(dc, old);
    DeleteObject(font);
    ReleaseDC(notice_hwnd_, dc);
    SetWindowPos(notice_hwnd_, nullptr, 0, 0, size.cx + Px(notice_hwnd_, 14),
                 size.cy + Px(notice_hwnd_, 6),
                 SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
    InvalidateRect(notice_hwnd_, nullptr, TRUE);
    KillTimer(hwnd_, kTimerNotice);
    if (n == Notice::Stopped || n == Notice::Reopen) ArmTimer(kTimerNotice, NowMs() + tip_logic::NOTICE_SHOW_MS);
    // Placed under the caret by a read-only session.
    ITfContext* ctx = FocusContext();
    if (!ctx) return;
    auto* es = new ReadSession(ReadSession::Caret, this, ctx);
    HRESULT result = S_OK;
    ctx->RequestEditSession(client_, es, TF_ES_ASYNCDONTCARE | TF_ES_READ, &result);
    es->Release();
    ctx->Release();
}

class Factory final : public IClassFactory {
public:
    LONG refs_ = 1;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** pp) override {
        if (!pp) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_IClassFactory) {
            *pp = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *pp = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        LONG n = InterlockedDecrement(&refs_);
        if (!n) delete this;
        return n;
    }
    HRESULT STDMETHODCALLTYPE CreateInstance(IUnknown* outer, REFIID riid, void** pp) override {
        if (outer) return CLASS_E_NOAGGREGATION;
        auto* t = new Tip();
        HRESULT hr = t->QueryInterface(riid, pp);
        t->Release();
        return hr;
    }
    HRESULT STDMETHODCALLTYPE LockServer(BOOL lock) override {
        if (lock) InterlockedIncrement(&g_locks);
        else InterlockedDecrement(&g_locks);
        return S_OK;
    }
};

// -- registration (§8.1: run elevated by packaging/elevated.ps1, then read back) ----------

static HRESULT SetSz(HKEY root, const wchar_t* sub, const wchar_t* name, const wchar_t* val) {
    HKEY k = nullptr;
    LONG e = RegCreateKeyExW(root, sub, 0, nullptr, 0, KEY_WRITE, nullptr, &k, nullptr);
    if (e != ERROR_SUCCESS) return HRESULT_FROM_WIN32(e);
    e = RegSetValueExW(k, name, 0, REG_SZ, (const BYTE*)val, (DWORD)((wcslen(val) + 1) * 2));
    RegCloseKey(k);
    return HRESULT_FROM_WIN32(e);
}

static std::wstring DllPath() {
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(g_hinst, path, MAX_PATH);
    return path;
}

static std::wstring GuidStr(const GUID& g) {
    wchar_t s[64];
    StringFromGUID2(g, s, 64);
    return s;
}

static std::wstring ClsidKey() { return L"Software\\Classes\\CLSID\\" + GuidStr(CLSID_YuragiTip); }

// The machine's registration only (every user's applications load the DLL).
static HRESULT RegisterCom() {
    std::wstring clsid = ClsidKey();
    HRESULT hr = SetSz(HKEY_LOCAL_MACHINE, clsid.c_str(), nullptr, L"ゆらぎ");
    if (FAILED(hr)) return hr;
    std::wstring inproc = clsid + L"\\InprocServer32";
    hr = SetSz(HKEY_LOCAL_MACHINE, inproc.c_str(), nullptr, DllPath().c_str());
    if (FAILED(hr)) return hr;
    return SetSz(HKEY_LOCAL_MACHINE, inproc.c_str(), L"ThreadingModel", L"Apartment");
}

static HRESULT RegisterCategories(BOOL add) {
    ITfCategoryMgr* cat = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_ITfCategoryMgr, (void**)&cat);
    if (FAILED(hr)) return hr;
    for (const GUID& g : kCats) {
        HRESULT one = add ? cat->RegisterCategory(CLSID_YuragiTip, g, CLSID_YuragiTip)
                          : cat->UnregisterCategory(CLSID_YuragiTip, g, CLSID_YuragiTip);
        if (FAILED(one) && SUCCEEDED(hr)) hr = one;
    }
    cat->Release();
    return hr;
}

static HRESULT RegisterProfile(BOOL add) {
    // CorvusSKK / SampleIME: ProfileMgr only. The old Register+AddLanguageProfile
    // pair plus this API together made Win11 attach the wrong CJK engine.
    ITfInputProcessorProfileMgr* mgr = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_ITfInputProcessorProfileMgr, (void**)&mgr);
    if (FAILED(hr) || !mgr) return FAILED(hr) ? hr : E_FAIL;
    LANGID ja = MAKELANGID(LANG_JAPANESE, SUBLANG_JAPANESE_JAPAN);
    if (add) {
        std::wstring path = DllPath();
        hr = mgr->RegisterProfile(CLSID_YuragiTip, ja, GUID_YuragiProfile, L"ゆらぎ",
                                  (ULONG)wcslen(L"ゆらぎ"), path.c_str(), (ULONG)path.size(),
                                  (ULONG)-IDI_YURAGI,  // negative: a resource ID, as ExtractIcon reads it
                                  nullptr, 0, TRUE, 0);
        mgr->Release();
        HRESULT categories = RegisterCategories(TRUE);
        return FAILED(hr) ? hr : categories;
    }
    // Categories first: UnregisterProfile deletes the TIP's whole key, categories
    // included, after which UnregisterCategory fails with E_FAIL.
    HRESULT categories = RegisterCategories(FALSE);
    hr = mgr->UnregisterProfile(CLSID_YuragiTip, ja, GUID_YuragiProfile, TF_URP_ALLPROFILES);
    mgr->Release();
    return FAILED(categories) ? categories : hr;
}

BOOL APIENTRY DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) g_hinst = h;
    return TRUE;
}

STDAPI DllCanUnloadNow() { return g_locks ? S_FALSE : S_OK; }

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv) {
    if (!IsEqualCLSID(rclsid, CLSID_YuragiTip)) return CLASS_E_CLASSNOTAVAILABLE;
    auto* f = new Factory();
    HRESULT hr = f->QueryInterface(riid, ppv);
    f->Release();
    return hr;
}

STDAPI DllRegisterServer() {
    HRESULT co = CoInitialize(nullptr);
    HRESULT hr = RegisterCom();
    if (SUCCEEDED(hr)) hr = RegisterProfile(TRUE);
    if (co == S_OK) CoUninitialize();
    return hr;
}

// Every step runs; the first failure is returned (I-6: no false "OK").
STDAPI DllUnregisterServer() {
    HRESULT co = CoInitialize(nullptr);
    HRESULT hr = RegisterProfile(FALSE);
    LONG e = RegDeleteTreeW(HKEY_LOCAL_MACHINE, ClsidKey().c_str());
    if (e != ERROR_SUCCESS && e != ERROR_FILE_NOT_FOUND && SUCCEEDED(hr)) hr = HRESULT_FROM_WIN32(e);
    if (co == S_OK) CoUninitialize();
    return hr;
}
