// Lightweight TSF text service.
// Keys + QPC → python -m oyayubi.ime.server → composition.
//
// Practices from SampleIME / CorvusSKK / Mozc tip_keyevent_handler.cc:
//   - disabled / empty context / keyboard closed → do not eat
//   - engine not ready or one IPC miss → that key goes to the app (Mozc)
//   - do not kill the engine on a single timeout
//   - empty composition: Back/Enter/Esc go to the app (SampleIME)
//   - RequestEditSession: SYNC, then ASYNCDONTCARE on TF_E_SYNCHRONOUS
//   - SEH around key/edit so a crash does not take TextInputHost with it
// Never block ActivateEx. Win11 TextInputHost is shared.

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <oleauto.h>
#include <initguid.h>
#include <msctf.h>
#include <inputscope.h>
#include <memory>
#include <algorithm>
#include "tip_json.h"
#include <cstdarg>
#include <cstdio>
#include <climits>
#include <cwchar>
#include <process.h>
#include <string>
#include <vector>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "uuid.lib")
#pragma comment(lib, "ws2_32.lib")

// {A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}
static const CLSID CLSID_OyayubiTip = {
    0xa7c4e201, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}
static const GUID GUID_OyayubiProfile = {
    0xa7c4e202, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E206-0B3A-4F11-9E61-0C1A0B7E0A01}
static const GUID GUID_OyayubiToggle = {
    0xa7c4e206, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E207-0B3A-4F11-9E61-0C1A0B7E0A01}
// Alt+` on the Japanese 101 layout (kbd101.dll) reaches TSF as Alt+VK_KANJI,
// never as VK_OEM_3, so the default toggle also preserves that alias.
static const GUID GUID_OyayubiToggleKanji = {
    0xa7c4e207, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E203-0B3A-4F11-9E61-0C1A0B7E0A01}
static const GUID GUID_OyayubiLangBar = {
    0xa7c4e203, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};

static const GUID kCats[] = {
    GUID_TFCAT_TIP_KEYBOARD,
    GUID_TFCAT_DISPLAYATTRIBUTEPROVIDER,
    GUID_TFCAT_TIPCAP_COMLESS,
    GUID_TFCAT_TIPCAP_INPUTMODECOMPARTMENT,
    GUID_TFCAT_TIPCAP_UIELEMENTENABLED,
    GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT,
    GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT,
};

static LONG g_locks = 0;
static HINSTANCE g_hinst = nullptr;

// Hard cap on the TSF thread for every request (keys and timer polls alike).
// The LM no longer runs inside a request (the server replies "pending"), so
// this is only the one-time wait for a dead server: 3x the healthy worst case.
static const DWORD kKeyBudgetMs = 250;

static void Log(const wchar_t* fmt, ...) {
    wchar_t path[MAX_PATH];
    GetTempPathW(MAX_PATH, path);
    wcscat_s(path, L"oyayubi_tip.log");
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

static ULONGLONG NowMs() {
    LARGE_INTEGER c, f;
    QueryPerformanceCounter(&c);
    QueryPerformanceFrequency(&f);
    if (f.QuadPart == 0) return GetTickCount64();
    return (ULONGLONG)(c.QuadPart * 1000 / f.QuadPart);
}

static UINT VkOf(WPARAM w) { return (UINT)(w & 0xFF); }

static bool ModHeld() {
    if (GetKeyState(VK_CONTROL) & 0x8000) return true;
    if (GetKeyState(VK_MENU) & 0x8000) return true;
    if (GetKeyState(VK_LWIN) & 0x8000) return true;
    if (GetKeyState(VK_RWIN) & 0x8000) return true;
    return false;
}

// Match session.py and chord/keys.py; modifiers themselves stay with the app.
static bool CharacterVk(UINT vk) {
    if ((vk >= 'A' && vk <= 'Z') || (vk >= '0' && vk <= '9')) return true;
    switch (vk) {
        case VK_OEM_1: case VK_OEM_PLUS: case VK_OEM_COMMA: case VK_OEM_MINUS:
        case VK_OEM_PERIOD: case VK_OEM_2: case VK_OEM_3: case VK_OEM_4:
        case VK_OEM_5: case VK_OEM_6: case VK_OEM_7: return true;
        default: return false;
    }
}
static bool WantVk(UINT vk, bool composing, bool converted, bool candidates) {
    if (ModHeld()) return false;
    bool shift = (GetKeyState(VK_SHIFT) & 0x8000) != 0;
    if (CharacterVk(vk) || vk == VK_SPACE) return true;
    if (vk == VK_RETURN || vk == VK_BACK || vk == VK_ESCAPE) return composing;
    if (vk == VK_F7) return true;
    if (vk == VK_F6) return composing;
    if (vk == VK_F8 || vk == VK_F9 || vk == VK_F10) return composing && !shift;  // one-shot spellings
    if (vk == VK_TAB) return composing && !shift;  // predictions; eaten only if any
    if (converted) {
        if (vk == VK_LEFT || vk == VK_RIGHT) return true;
        if (!shift && (vk == VK_UP || vk == VK_DOWN || vk == VK_PRIOR || vk == VK_NEXT)) return true;
    }
    return false;
}

static bool PasswordScope(ITfContext* ctx, TfClientId client);
static bool KeyboardDisabled(ITfThreadMgr* tm, TfClientId client, bool check_scope = true) {
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
        ITfCompartment* c = nullptr;
        if (SUCCEEDED(cm->GetCompartment(GUID_COMPARTMENT_KEYBOARD_DISABLED, &c)) && c) {
            VARIANT v;
            VariantInit(&v);
            if (SUCCEEDED(c->GetValue(&v)) && v.vt == VT_I4 && v.lVal) disabled = true;
            VariantClear(&v);
            c->Release();
        }
        if (!disabled &&
            SUCCEEDED(cm->GetCompartment(GUID_COMPARTMENT_EMPTYCONTEXT, &c)) && c) {
            VARIANT v;
            VariantInit(&v);
            if (SUCCEEDED(c->GetValue(&v)) && v.vt == VT_I4 && v.lVal) disabled = true;
            VariantClear(&v);
            c->Release();
        }
        cm->Release();
    }
    if (!disabled && check_scope) disabled = PasswordScope(ctx, client);
    ctx->Release();
    dm->Release();
    return disabled;
}

// SampleIME / CorvusSKK / Mozc: closed keyboard is not our key.
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

static std::wstring RepoDir() {
    std::wstring dir = DllDir();
    // dist\NicolaIME3.dll → repo root
    size_t slash = dir.find_last_of(L"\\/");
    if (slash != std::wstring::npos) {
        std::wstring parent = dir.substr(0, slash);
        std::wstring marker = parent + L"\\oyayubi\\ime\\server.py";
        if (GetFileAttributesW(marker.c_str()) != INVALID_FILE_ATTRIBUTES) return parent;
    }
    return dir;
}

static bool FindPython(wchar_t* out, size_t cap) {
    if (SearchPathW(nullptr, L"python.exe", nullptr, (DWORD)cap, out, nullptr)) return true;
    const wchar_t* fallbacks[] = {
        L"C:\\Users\\marur\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
        L"C:\\Windows\\py.exe",
    };
    for (auto p : fallbacks) {
        if (GetFileAttributesW(p) != INVALID_FILE_ATTRIBUTES) {
            wcsncpy_s(out, cap, p, _TRUNCATE);
            return true;
        }
    }
    return false;
}

struct SegmentReply {
    int display_start = 0, display_end = 0;
};
struct ServerReply {
    std::wstring composition, commit;
    bool direct = false, pass_through = false, converted = false;
    bool pending = false;  // the conversion awaits the LM; poll with the timer
    bool predicting = false;  // Tab opened (or advanced) predictions
    int n_tokens = 0, focus = -1, page = 0;
    bool has_next_page = false, has_timer = false;
    ULONGLONG timer = 0;
    std::vector<std::wstring> candidates;
    std::vector<SegmentReply> segments;
    int cand_index = 0;
};

static bool JsonInt(const tip_json::Value& v, const char* key, int& n) {
    long long value;
    if (!v.Get(key).Int(value) || value < INT_MIN || value > INT_MAX) return false;
    n = (int)value;
    return true;
}
static bool JsonBool(const tip_json::Value& v, const char* key, bool& b) {
    const auto& x = v.Get(key);
    if (x.kind != tip_json::Value::Boolean) return false;
    b = x.boolean;
    return true;
}
static bool ParseReply(const std::string& s, ServerReply& r) {
    tip_json::Value v;
    if (!tip_json::Parser(s).Parse(v) || v.kind != tip_json::Value::Object ||
        v.Get("composition").kind != tip_json::Value::String ||
        v.Get("commit").kind != tip_json::Value::String ||
        !JsonBool(v, "direct", r.direct) || !JsonBool(v, "pass_through", r.pass_through) ||
        !JsonBool(v, "converted", r.converted) || !JsonInt(v, "n_tokens", r.n_tokens) ||
        !JsonInt(v, "focus", r.focus) || !JsonInt(v, "candidate_page", r.page) ||
        !JsonInt(v, "cand_index", r.cand_index) ||
        !JsonBool(v, "has_next_page", r.has_next_page) ||
        !tip_json::OptionalBool(v, "pending", r.pending) ||
        !tip_json::OptionalBool(v, "predicting", r.predicting)) return false;
    r.composition = Utf8ToWide(v.Get("composition").text);
    r.commit = Utf8ToWide(v.Get("commit").text);
    const auto& timer = v.Get("timer");
    if (timer.kind != tip_json::Value::Null) {
        long long n;
        if (!timer.Int(n) || n < 0) return false;
        r.has_timer = true; r.timer = (ULONGLONG)n;
    }
    const auto& candidates = v.Get("candidates");
    const auto& segments = v.Get("segments");
    if (candidates.kind != tip_json::Value::Array || segments.kind != tip_json::Value::Array)
        return false;
    for (const auto& c : candidates.array) {
        if (c.kind != tip_json::Value::String) return false;
        r.candidates.push_back(Utf8ToWide(c.text));
    }
    for (const auto& seg : segments.array) {
        SegmentReply item;
        if (!JsonInt(seg, "display_start", item.display_start) ||
            !JsonInt(seg, "display_end", item.display_end) ||
            item.display_start < 0 || item.display_end < item.display_start) return false;
        r.segments.push_back(item);
    }
    return true;
}

static bool ReadJsonFile(const std::wstring& path, tip_json::Value& value) {
    FILE* f = nullptr;
    if (_wfopen_s(&f, path.c_str(), L"rb") || !f) return false;
    std::string bytes;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) != 0) {
        bytes.append(buf, n);
        if (bytes.size() > 1024 * 1024) { fclose(f); return false; }
    }
    bool good = !ferror(f);
    fclose(f);
    return good && tip_json::Parser(bytes).Parse(value) && value.kind == tip_json::Value::Object;
}

static std::wstring Environment(const wchar_t* name) {
    DWORD size = GetEnvironmentVariableW(name, nullptr, 0);
    if (!size) return {};
    std::wstring value(size, 0);
    DWORD copied = GetEnvironmentVariableW(name, &value[0], size);
    if (!copied || copied >= size) return {};
    value.resize(copied);
    return value;
}

struct TipConfig {
    std::wstring runtime_dir;
    UINT toggle_vk = VK_OEM_3;
    unsigned toggle_mods = 4;  // shift=1, ctrl=2, alt=4, win=8
    bool log_text = false;
    void Load() {
        // Match pathlib.Path.home()/ntpath.expanduser on Windows.
        std::wstring profile = Environment(L"USERPROFILE");
        if (profile.empty()) profile = Environment(L"HOMEDRIVE") + Environment(L"HOMEPATH");
        if (profile.empty()) return;
        runtime_dir = profile + L"\\.oyayubi-ime";
        tip_json::Value v;
        if (!ReadJsonFile(runtime_dir + L"\\tip.json", v)) return;
        int vk;
        if (JsonInt(v, "toggle_vk", vk) && vk > 0 && vk < 256) toggle_vk = (UINT)vk;
        const auto& mods = v.Get("toggle_mods");
        if (mods.kind == tip_json::Value::Array) {
            unsigned mask = 0; bool valid = true;
            for (const auto& m : mods.array) {
                if (m.kind != tip_json::Value::String) { valid = false; break; }
                if (m.text == "shift") mask |= 1;
                else if (m.text == "ctrl") mask |= 2;
                else if (m.text == "alt") mask |= 4;
                else if (m.text == "win") mask |= 8;
                else { valid = false; break; }
            }
            if (valid) toggle_mods = mask;
        }
        JsonBool(v, "log_text", log_text);
    }
};

static unsigned HeldMods() {
    unsigned mods = 0;
    if (GetKeyState(VK_SHIFT) & 0x8000) mods |= 1;
    if (GetKeyState(VK_CONTROL) & 0x8000) mods |= 2;
    if (GetKeyState(VK_MENU) & 0x8000) mods |= 4;
    if ((GetKeyState(VK_LWIN) | GetKeyState(VK_RWIN)) & 0x8000) mods |= 8;
    return mods;
}
static std::string ModsJson(unsigned mask) {
    std::string out = "[";
    const char* names[] = {"shift", "ctrl", "alt", "win"};
    for (unsigned i = 0; i < 4; ++i) if (mask & (1u << i)) {
        if (out.size() > 1) out += ',';
        out += tip_json::Quote(names[i]);
    }
    return out + ']';
}

// Only the connector runs on the worker. TSF and timer requests share one
// serialized stream; a failed/ambiguous exchange always drops the connection.
struct Server {
    CRITICAL_SECTION lock;
    HANDLE stop_event = nullptr, worker = nullptr;
    SOCKET connection = INVALID_SOCKET;
    volatile LONG ready = 0, generation = 0, direct = 0, settings_loaded = 0;
    bool wsa = false, busy = false;
    // Loaded once by the worker before publishing settings_loaded/ready.
    // Immutable thereafter.
    TipConfig settings;
    std::wstring runtime_dir;
    Server() { InitializeCriticalSection(&lock); }
    ~Server() { Stop(); DeleteCriticalSection(&lock); }
    bool Alive() { return InterlockedCompareExchange(&ready, 0, 0) != 0; }
    bool SettingsLoaded() { return InterlockedCompareExchange(&settings_loaded, 0, 0) != 0; }
    LONG Generation() { return InterlockedCompareExchange(&generation, 0, 0); }
    void RememberDirect(bool value) { InterlockedExchange(&direct, value ? 1 : 0); }
    bool Stopping() { return WaitForSingleObject(stop_event, 0) == WAIT_OBJECT_0; }

    static bool WaitSocket(SOCKET s, bool writing, ULONGLONG deadline) {
        ULONGLONG now = GetTickCount64();
        if (now >= deadline) return false;
        DWORD ms = (DWORD)(deadline - now);
        fd_set fds, errors; FD_ZERO(&fds); FD_SET(s, &fds);
        FD_ZERO(&errors); FD_SET(s, &errors);
        timeval tv{}; tv.tv_sec = ms / 1000; tv.tv_usec = (ms % 1000) * 1000;
        int result = select(0, writing ? nullptr : &fds, writing ? &fds : nullptr, &errors, &tv);
        return result > 0 && !FD_ISSET(s, &errors);
    }
    static bool Exchange(SOCKET s, const std::string& json, std::string& reply,
                         ULONGLONG deadline) {
        std::string line = json + '\n';
        size_t sent = 0;
        while (sent < line.size()) {
            if (!WaitSocket(s, true, deadline)) return false;
            int n = send(s, line.data() + sent, (int)(line.size() - sent), 0);
            if (n == SOCKET_ERROR && WSAGetLastError() == WSAEWOULDBLOCK) continue;
            if (n <= 0) return false;
            sent += n;
        }
        reply.clear();
        // One outstanding request. Reading only through its newline means a
        // late response can never be drained and mistaken for another reply.
        while (reply.size() < 1024 * 1024) {
            if (!WaitSocket(s, false, deadline)) return false;
            char buf[4096];
            int n = recv(s, buf, sizeof(buf), 0);
            if (n == SOCKET_ERROR && WSAGetLastError() == WSAEWOULDBLOCK) continue;
            if (n <= 0) return false;
            reply.append(buf, n);
            size_t nl = reply.find('\n');
            if (nl != std::string::npos) {
                if (nl != reply.size() - 1) return false;
                reply.resize(nl);
                return true;
            }
        }
        return false;
    }
    SOCKET Connect(ULONGLONG startup_deadline) {
        tip_json::Value endpoint;
        int port = 0, version = 0;
        if (!ReadJsonFile(runtime_dir + L"\\endpoint.json", endpoint) ||
            !JsonInt(endpoint, "version", version) || version != 1 ||
            !JsonInt(endpoint, "port", port) || port < 1 || port > 65535 ||
            endpoint.Get("token").kind != tip_json::Value::String) return INVALID_SOCKET;
        ULONGLONG deadline = (std::min)(GetTickCount64() + 250, startup_deadline);
        SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (s == INVALID_SOCKET) return s;
        // Neither this socket nor an active resident connection is inherited by Python.
        SetHandleInformation((HANDLE)s, HANDLE_FLAG_INHERIT, 0);
        u_long nonblocking = 1;
        if (ioctlsocket(s, FIONBIO, &nonblocking) == SOCKET_ERROR) {
            closesocket(s); return INVALID_SOCKET;
        }
        sockaddr_in addr{};
        addr.sin_family = AF_INET; addr.sin_port = htons((u_short)port);
        addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        int result = connect(s, (sockaddr*)&addr, sizeof(addr));
        if (result == SOCKET_ERROR) {
            if (WSAGetLastError() != WSAEWOULDBLOCK || !WaitSocket(s, true, deadline)) {
                closesocket(s); return INVALID_SOCKET;
            }
            int error = 0, size = sizeof(error);
            if (getsockopt(s, SOL_SOCKET, SO_ERROR, (char*)&error, &size) == SOCKET_ERROR || error) {
                closesocket(s); return INVALID_SOCKET;
            }
        }
        std::string reply;
        tip_json::Value hello;
        std::string request = "{\"op\":\"hello\",\"version\":1,\"token\":" +
                              tip_json::Quote(endpoint.Get("token").text) + "}";
        bool ok = false;
        if (!Exchange(s, request, reply, deadline) || !tip_json::Parser(reply).Parse(hello) ||
            hello.kind != tip_json::Value::Object || hello.object.size() != 3 ||
            !JsonBool(hello, "ok", ok) || !ok || hello.Get("msg").kind != tip_json::Value::String ||
            hello.Get("msg").text != "ready" || !JsonInt(hello, "version", version) || version != 1) {
            closesocket(s); return INVALID_SOCKET;
        }
        ServerReply restored;
        bool mode = InterlockedCompareExchange(&direct, 0, 0) != 0;
        if (!Exchange(s, mode ? "{\"op\":\"set_direct\",\"direct\":true}" :
                               "{\"op\":\"set_direct\",\"direct\":false}", reply,
                      deadline) || !ParseReply(reply, restored) ||
            restored.direct != mode) {
            closesocket(s); return INVALID_SOCKET;
        }
        return s;
    }
    // The Python that runs the IME's Python side. An installed package
    // (see docs/INSTALL.md) carries its own runtime under the package
    // root: an embeddable runtime\python.exe or a venv at
    // runtime\Scripts\python.exe. Prefer either over a system Python,
    // so the install does not depend on one.
    bool ResolvePython(wchar_t* py) {
        std::wstring root = RepoDir();
        const wchar_t* bundled_paths[] = {
            L"\\runtime\\python.exe", L"\\runtime\\Scripts\\python.exe"};
        for (auto suffix : bundled_paths) {
            std::wstring bundled = root + suffix;
            if (GetFileAttributesW(bundled.c_str()) != INVALID_FILE_ATTRIBUTES) {
                wcsncpy_s(py, MAX_PATH, bundled.c_str(), _TRUNCATE);
                return true;
            }
        }
        return FindPython(py, MAX_PATH);
    }
    // The management window (oyayubi.ime.admin_ui), opened from the
    // language-bar button. Prefer pythonw.exe beside the resolved
    // python.exe (venv, embeddable and system installs all ship one),
    // so no console window flashes up.
    bool SpawnAdminUi() {
        wchar_t py[MAX_PATH];
        if (!ResolvePython(py)) { Log(L"admin ui: python.exe not found"); return false; }
        std::wstring exe = py;
        std::wstring pythonw = exe.substr(0, exe.find_last_of(L'\\') + 1) + L"pythonw.exe";
        if (GetFileAttributesW(pythonw.c_str()) != INVALID_FILE_ATTRIBUTES) exe = pythonw;
        std::wstring command = L"\"" + exe + L"\" -m oyayubi.ime.admin_ui";
        STARTUPINFOW si{}; si.cb = sizeof(si);
        PROCESS_INFORMATION pi{};
        std::wstring cwd = RepoDir();
        BOOL made = CreateProcessW(nullptr, &command[0], nullptr, nullptr, FALSE,
            DETACHED_PROCESS, nullptr, cwd.c_str(), &si, &pi);
        if (!made) { Log(L"admin ui spawn failed err=%lu", GetLastError()); return false; }
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        return true;
    }
    bool SpawnResident() {
        wchar_t py[MAX_PATH];
        if (!ResolvePython(py)) { Log(L"python.exe not found"); return false; }
        // Quote the runtime path according to Windows command-line rules.
        // Profile paths contain no quotes and this path has no trailing slash.
        std::wstring command = L"\"" + std::wstring(py) +
            L"\" -m oyayubi.ime.server --resident --runtime-dir \"" + runtime_dir + L"\"";
        SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
        HANDLE nul = CreateFileW(L"NUL", GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE, &sa, OPEN_EXISTING, 0, nullptr);
        if (nul == INVALID_HANDLE_VALUE) return false;
        // Equivalent to subprocess close_fds=True: inherit only DEVNULL,
        // never unrelated inheritable handles from the application hosting us.
        STARTUPINFOEXW si{}; si.StartupInfo.cb = sizeof(si);
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        si.StartupInfo.hStdInput = si.StartupInfo.hStdOutput = si.StartupInfo.hStdError = nul;
        SIZE_T bytes = 0;
        InitializeProcThreadAttributeList(nullptr, 1, 0, &bytes);
        si.lpAttributeList = (LPPROC_THREAD_ATTRIBUTE_LIST)HeapAlloc(GetProcessHeap(), 0, bytes);
        if (!si.lpAttributeList) { CloseHandle(nul); return false; }
        if (!InitializeProcThreadAttributeList(si.lpAttributeList, 1, 0, &bytes)) {
            HeapFree(GetProcessHeap(), 0, si.lpAttributeList); CloseHandle(nul); return false;
        }
        BOOL attributes = UpdateProcThreadAttribute(si.lpAttributeList, 0,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST, &nul, sizeof(nul), nullptr, nullptr);
        PROCESS_INFORMATION pi{};
        std::wstring cwd = RepoDir();
        BOOL made = attributes && CreateProcessW(nullptr, &command[0], nullptr, nullptr, TRUE,
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | EXTENDED_STARTUPINFO_PRESENT,
            nullptr, cwd.c_str(), &si.StartupInfo, &pi);
        DWORD error = made ? ERROR_SUCCESS : GetLastError();
        DeleteProcThreadAttributeList(si.lpAttributeList);
        HeapFree(GetProcessHeap(), 0, si.lpAttributeList);
        CloseHandle(nul);
        SetLastError(error);
        if (!made) { Log(L"resident spawn failed %lu", GetLastError()); return false; }
        CloseHandle(pi.hThread); CloseHandle(pi.hProcess);
        return true;
    }
    void Run() {
        settings.Load();
        runtime_dir = settings.runtime_dir;
        InterlockedExchange(&settings_loaded, 1);
        if (runtime_dir.empty()) return;
        while (!Stopping()) {
            if (Alive()) {
                // Detect an idle peer's death even in direct mode. Peek never
                // removes data or overlaps a key exchange on the stream.
                if (TryEnterCriticalSection(&lock)) {
                    if (Alive() && !busy) {
                        char byte;
                        int n = recv(connection, &byte, 1, MSG_PEEK);
                        if (n == 0 || (n == SOCKET_ERROR && WSAGetLastError() != WSAEWOULDBLOCK))
                            DropLocked();
                    }
                    LeaveCriticalSection(&lock);
                }
                WaitForSingleObject(stop_event, 1000);
                continue;
            }
            ULONGLONG deadline = GetTickCount64() + 15000;
            SOCKET s = Connect(deadline);
            if (s == INVALID_SOCKET && !Stopping() && SpawnResident()) {
                while (!Stopping() && GetTickCount64() < deadline) {
                    s = Connect(deadline);
                    if (s != INVALID_SOCKET) break;
                    WaitForSingleObject(stop_event, 50);
                }
            }
            if (s != INVALID_SOCKET) {
                EnterCriticalSection(&lock);
                if (!Stopping()) {
                    connection = s;
                    InterlockedIncrement(&generation);
                    InterlockedExchange(&ready, 1);
                    s = INVALID_SOCKET;
                }
                LeaveCriticalSection(&lock);
                if (s != INVALID_SOCKET) closesocket(s);
            }
            WaitForSingleObject(stop_event, 1000);
        }
    }
    static unsigned __stdcall Worker(void* self) { ((Server*)self)->Run(); return 0; }
    bool Start() {
        InterlockedExchange(&settings_loaded, 0);
        settings = TipConfig();
        WSADATA data{};
        if (WSAStartup(MAKEWORD(2, 2), &data)) return false;
        wsa = true;
        stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        if (!stop_event) { Stop(); return false; }
        worker = (HANDLE)_beginthreadex(nullptr, 0, Worker, this, 0, nullptr);
        if (!worker) { Stop(); return false; }
        return true;
    }
    void DropLocked() {
        InterlockedExchange(&ready, 0);
        if (connection != INVALID_SOCKET) closesocket(connection);
        connection = INVALID_SOCKET;
    }
    void Die(const wchar_t* why) {
        EnterCriticalSection(&lock);
        DropLocked();
        LeaveCriticalSection(&lock);
        Log(L"resident disconnected: %s", why);
    }
    void Stop() {
        if (stop_event) SetEvent(stop_event);
        Die(L"stop");
        if (worker) { WaitForSingleObject(worker, INFINITE); CloseHandle(worker); worker = nullptr; }
        if (stop_event) { CloseHandle(stop_event); stop_event = nullptr; }
        if (wsa) { WSACleanup(); wsa = false; }
    }
    bool Send(const std::string& json, std::string& reply, DWORD budget_ms, LONG expected_generation = -1) {
        if (!TryEnterCriticalSection(&lock)) return false;
        bool ok = false;
        if (!busy && Alive() && (expected_generation < 0 || Generation() == expected_generation)) {
            busy = true;
            ok = Exchange(connection, json, reply, GetTickCount64() + budget_ms);
            if (!ok) DropLocked();
            busy = false;
        }
        LeaveCriticalSection(&lock);
        return ok;
    }
};
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
class AttributeInfo : public ITfDisplayAttributeInfo {
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
        const wchar_t* names[] = {L"NicolaIME input", L"NicolaIME converted", L"NicolaIME focused segment"};
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
class AttributeEnum : public IEnumTfDisplayAttributeInfo {
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

// GetAppProperty/GetValue must run under a read cookie. No surrounding text is read.
static bool PasswordScopeWithCookie(ITfContext* ctx, TfEditCookie ec) {
    TF_SELECTION selection{}; ULONG fetched = 0;
    if (FAILED(ctx->GetSelection(ec, TF_DEFAULT_SELECTION, 1, &selection, &fetched)) || !fetched)
        return true;
    bool disabled = false;
    ITfReadOnlyProperty* prop = nullptr;
    if (SUCCEEDED(ctx->GetAppProperty(GUID_PROP_INPUTSCOPE, &prop)) && prop) {
        VARIANT v; VariantInit(&v);
        if (SUCCEEDED(prop->GetValue(ec, selection.range, &v)) && v.vt == VT_UNKNOWN && v.punkVal) {
            ITfInputScope* scope = nullptr;
            if (SUCCEEDED(v.punkVal->QueryInterface(IID_ITfInputScope, (void**)&scope))) {
                InputScope* scopes = nullptr; UINT count = 0;
                if (SUCCEEDED(scope->GetInputScopes(&scopes, &count))) {
                    for (UINT i = 0; i < count; ++i)
                        if (scopes[i] == IS_PASSWORD || scopes[i] == IS_NUMERIC_PASSWORD) disabled = true;
                } else disabled = true;
                CoTaskMemFree(scopes);
                scope->Release();
            }
        }
        VariantClear(&v); prop->Release();
    }
    selection.range->Release();
    return disabled;
}
class ScopeSession : public ITfEditSession {
    LONG refs_ = 1;
    ITfContext* ctx_;
public:
    bool disabled = true;
    explicit ScopeSession(ITfContext* ctx) : ctx_(ctx) { ctx_->AddRef(); }
    ~ScopeSession() { ctx_->Release(); }
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
        disabled = PasswordScopeWithCookie(ctx_, ec);
        return S_OK;
    }
};
static bool PasswordScope(ITfContext* ctx, TfClientId client) {
    auto* es = new ScopeSession(ctx);
    HRESULT result = E_FAIL;
    HRESULT hr = ctx->RequestEditSession(client, es, TF_ES_SYNC | TF_ES_READ, &result);
    bool disabled = FAILED(hr) || FAILED(result) || es->disabled;
    es->Release();
    return disabled;
}

struct CompositionState {
    ITfContext* ctx;
    Server* server;
    LONG connection_generation;
    ITfComposition* composition = nullptr;
    bool active = true, ending = false, position_pending = false, show_candidates = false;
    std::vector<ServerReply> pending;
    CompositionState(ITfContext* c, Server* s) : ctx(c), server(s), connection_generation(s->Generation()) {
        ctx->AddRef();
    }
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

static void PositionCandidate(TfEditCookie ec, ITfContext* ctx, ITfRange* caret, HWND window) {
    if (!window) return;
    ITfContextView* view = nullptr;
    bool positioned = false;
    if (SUCCEEDED(ctx->GetActiveView(&view))) {
        RECT rect{}; BOOL clipped = FALSE;
        // S_OK with an empty rectangle means the text is not visible.
        if (SUCCEEDED(view->GetTextExt(ec, caret, &rect, &clipped)) && !IsRectEmpty(&rect)) {
            SetWindowPos(window, HWND_TOPMOST, rect.left, rect.bottom + 4, 0, 0,
                         SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW);
            positioned = true;
        }
        view->Release();
    }
    if (!positioned) ShowWindow(window, SW_HIDE);
}

using RetiredStates = std::vector<std::shared_ptr<CompositionState>>;
class EditSession : public ITfEditSession {
    LONG refs_ = 1;
    std::shared_ptr<CompositionState> state_;
    ITfCompositionSink* sink_;
    TfGuidAtom atoms_[3];
    HWND* candidate_;
    bool discard_, position_only_;
    RetiredStates* retired_;
public:
    EditSession(std::shared_ptr<CompositionState> state, ITfCompositionSink* sink,
                const TfGuidAtom* atoms, HWND* candidate, RetiredStates* retired,
                bool discard = false, bool position_only = false)
        : state_(std::move(state)), sink_(sink), candidate_(candidate), discard_(discard),
          position_only_(position_only), retired_(retired) {
        sink_->AddRef();
        for (int i = 0; i < 3; ++i) atoms_[i] = atoms[i];
    }
    ~EditSession() {
        if (position_only_) state_->position_pending = false;
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
    void End(TfEditCookie ec, const std::shared_ptr<CompositionState>& state) {
        if (!state->composition) return;
        state->ending = true;
        ITfComposition* comp = state->composition;
        state->composition = nullptr;
        comp->EndComposition(ec);
        comp->Release();
        state->ending = false;
    }
    HRESULT Discard(TfEditCookie ec, const std::shared_ptr<CompositionState>& state) {
        if (!state->composition) return S_OK;
        ITfRange* range = nullptr;
        HRESULT hr = state->composition->GetRange(&range);
        if (FAILED(hr)) return hr;
        hr = range->SetText(ec, 0, L"", 0);
        ITfProperty* prop = nullptr;
        if (SUCCEEDED(state->ctx->GetProperty(GUID_PROP_ATTRIBUTE, &prop))) {
            prop->Clear(ec, range); prop->Release();
        }
        End(ec, state);
        range->Release();
        return hr;
    }
    void Attribute(TfEditCookie ec, ITfProperty* prop, ITfRange* range, TfGuidAtom atom) {
        if (atom == TF_INVALID_GUIDATOM) return;
        VARIANT v; VariantInit(&v); v.vt = VT_I4; v.lVal = (LONG)atom;
        prop->SetValue(ec, range, &v);
    }
    HRESULT Write(TfEditCookie ec, const std::wstring& text, bool commit, const ServerReply& reply) {
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
            if (candidate_ && *candidate_ && !commit && reply.converted && !reply.candidates.empty()) {
                PositionCandidate(ec, ctx, caret, *candidate_);
            }
            caret->Release();
        }
        if (commit || text.empty()) End(ec, state_);
        range->Release();
        return S_OK;
    }
    HRESULT DoEditSessionImpl(TfEditCookie ec) {
        if (position_only_) {
            state_->position_pending = false;
            if (!state_->active || !state_->server->Alive() ||
                state_->server->Generation() != state_->connection_generation ||
                !state_->composition || !state_->show_candidates || !candidate_ || !*candidate_ ||
                PasswordScopeWithCookie(state_->ctx, ec)) return S_OK;
            ITfRange* caret = nullptr;
            if (SUCCEEDED(state_->composition->GetRange(&caret))) {
                caret->Collapse(ec, TF_ANCHOR_END);
                PositionCandidate(ec, state_->ctx, caret, *candidate_);
                caret->Release();
            }
            return S_OK;
        }
        if (discard_) return Discard(ec, state_);
        if (!state_->active || !state_->server->Alive() ||
            state_->server->Generation() != state_->connection_generation) return S_OK;
        if (PasswordScopeWithCookie(state_->ctx, ec)) {
            state_->pending.clear();
            state_->active = false;
            return S_OK;
        }
        // Synchronous edits can overtake queued asynchronous cleanup. Finish
        // retired compositions in this context before inserting anything new;
        // their later cleanup callbacks then see no composition and do nothing.
        for (const auto& old : *retired_) {
            if (old->ctx == state_->ctx && old->composition) {
                HRESULT hr = Discard(ec, old);
                if (FAILED(hr)) return hr;
            }
        }
        retired_->erase(std::remove_if(retired_->begin(), retired_->end(),
            [](const std::shared_ptr<CompositionState>& old) { return !old->composition; }), retired_->end());
        std::vector<ServerReply> pending;
        pending.swap(state_->pending);
        for (const auto& r : pending) {
            if (!state_->active || !state_->server->Alive() ||
                state_->server->Generation() != state_->connection_generation) break;
            // A converted phrase can commit and start the next composition in
            // the same reply. Apply both in order under the same edit cookie.
            HRESULT hr = S_OK;
            if (!r.commit.empty()) hr = Write(ec, r.commit, true, r);
            if (SUCCEEDED(hr)) hr = Write(ec, r.composition, false, r);
            if (FAILED(hr)) return hr;
        }
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie ec) override {
        HRESULT hr = S_OK;
        __try { hr = DoEditSessionImpl(ec); }
        __except (EXCEPTION_EXECUTE_HANDLER) { state_->active = false; hr = E_FAIL; }
        // Queued edits report failures here, after RequestEditSession returned.
        // Let the next timer/key reset the session instead of accepting more text.
        if (FAILED(hr)) state_->active = false;
        return hr;
    }
};

class Tip : public ITfTextInputProcessorEx,
            public ITfKeyEventSink,
            public ITfThreadMgrEventSink,
            public ITfCompositionSink,
            public ITfDisplayAttributeProvider,
            public ITfLangBarItemButton {
public:
    LONG refs_ = 1;
    ITfThreadMgr* tm_ = nullptr;
    TfClientId client_ = TF_CLIENTID_NULL;
    DWORD cookie_tm_ = TF_INVALID_COOKIE;
    std::shared_ptr<CompositionState> state_;
    RetiredStates retired_;
    Server server_;
    TfGuidAtom atoms_[3] = {TF_INVALID_GUIDATOM, TF_INVALID_GUIDATOM, TF_INVALID_GUIDATOM};
    LONG connection_generation_ = 0;
    bool direct_ = false;
    bool scope_allowed_ = false;
    bool toggle_down_ = false, toggle_eaten_ = false;
    TF_PRESERVEDKEY preserved_toggle_{};
    unsigned preserved_mods_ = 0;
    bool preserve_attempted_ = false, toggle_preserved_ = false;
    TF_PRESERVEDKEY preserved_alias_{};
    bool alias_preserved_ = false;
    UINT toggle_active_vk_ = 0;  // the VK that set toggle_down_ (toggle_vk or its alias)
    bool langbar_ = false;
    bool test_alt_logged_ = false, down_alt_logged_ = false;
    int n_tokens_ = 0, focus_ = -1, page_ = 0;
    bool has_next_page_ = false;
    Tip() { InterlockedIncrement(&g_locks); }
    ~Tip() { server_.Stop(); InterlockedDecrement(&g_locks); }
    HWND timer_hwnd_ = nullptr;
    HWND cand_hwnd_ = nullptr;
    std::wstring last_comp_;
    bool composing_ = false;
    bool converted_ = false;
    bool pending_ = false;  // server: conversion awaiting the LM
    bool pending_timer_ = false;
    ULONGLONG timer_at_ = 0;
    bool down_eaten_[256]{};
    unsigned down_mods_[256]{};
    std::vector<std::wstring> cands_;
    int cand_index_ = 0;

    static LRESULT CALLBACK TimerWnd(HWND h, UINT m, WPARAM w, LPARAM l) {
        if (m == WM_TIMER) {
            auto* self = (Tip*)GetWindowLongPtrW(h, GWLP_USERDATA);
            if (self) self->OnTimer();
            return 0;
        }
        return DefWindowProcW(h, m, w, l);
    }

    void EnsureTimer() {
        if (timer_hwnd_) return;
        WNDCLASSW wc{};
        wc.lpfnWndProc = TimerWnd;
        wc.hInstance = g_hinst;
        wc.lpszClassName = L"OyayubiTipTimer";
        RegisterClassW(&wc);
        timer_hwnd_ = CreateWindowExW(0, wc.lpszClassName, L"", 0, 0, 0, 0, 0, HWND_MESSAGE,
                                      nullptr, g_hinst, nullptr);
        SetWindowLongPtrW(timer_hwnd_, GWLP_USERDATA, (LONG_PTR)this);
        SetTimer(timer_hwnd_, 1, 16, nullptr);
    }

    static LRESULT CALLBACK CandWnd(HWND h, UINT m, WPARAM w, LPARAM l) {
        if (m == WM_PAINT) {
            auto* self = (Tip*)GetWindowLongPtrW(h, GWLP_USERDATA);
            PAINTSTRUCT ps;
            HDC hdc = BeginPaint(h, &ps);
            RECT rc;
            GetClientRect(h, &rc);
            FillRect(hdc, &rc, (HBRUSH)GetStockObject(WHITE_BRUSH));
            if (self) {
                HFONT font = CreateFontW(18, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE,
                                         SHIFTJIS_CHARSET, 0, 0, 0, 0, L"Yu Gothic UI");
                HGDIOBJ old = SelectObject(hdc, font);
                int y = 2;
                for (size_t i = 0; i < self->cands_.size(); ++i) {
                    RECT line = {4, y, rc.right - 4, y + 22};
                    wchar_t buf[256];
                    swprintf_s(buf, L"%d. %s", (int)i + 1, self->cands_[i].c_str());
                    if ((int)i == self->cand_index_) {
                        SetBkColor(hdc, RGB(0x20, 0x60, 0xC0));
                        SetTextColor(hdc, RGB(255, 255, 255));
                    } else {
                        SetBkColor(hdc, RGB(255, 255, 255));
                        SetTextColor(hdc, RGB(0, 0, 0));
                    }
                    ExtTextOutW(hdc, 6, y + 2, ETO_OPAQUE, &line, buf, (UINT)wcslen(buf), nullptr);
                    y += 22;
                }
                SelectObject(hdc, old);
                DeleteObject(font);
            }
            EndPaint(h, &ps);
            return 0;
        }
        if (m == WM_MOUSEACTIVATE) return MA_NOACTIVATE;
        return DefWindowProcW(h, m, w, l);
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
        if (!cand_hwnd_) {
            WNDCLASSW wc{};
            wc.lpfnWndProc = CandWnd;
            wc.hInstance = g_hinst;
            wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
            wc.lpszClassName = L"OyayubiCand";
            RegisterClassW(&wc);
            cand_hwnd_ = CreateWindowExW(
                WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST, wc.lpszClassName, L"",
                WS_POPUP | WS_BORDER, 0, 0, 280, 24, nullptr, nullptr, g_hinst, nullptr);
            SetWindowLongPtrW(cand_hwnd_, GWLP_USERDATA, (LONG_PTR)this);
        }
        int rows = (int)cands_.size();
        int w = 280, h = 6 + rows * 22;
        // Placement/show happens under an edit cookie, using GetTextExt.
        SetWindowPos(cand_hwnd_, nullptr, 0, 0, w, h, SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE);
        InvalidateRect(cand_hwnd_, nullptr, TRUE);
    }

    ITfContext* FocusContext() {
        ITfDocumentMgr* dm = nullptr;
        if (!tm_ || FAILED(tm_->GetFocus(&dm)) || !dm) return nullptr;
        ITfContext* ctx = nullptr;
        dm->GetTop(&ctx);
        dm->Release();
        return ctx;
    }

    void RequestEdit(std::shared_ptr<CompositionState> state, bool sync, bool discard = false) {
        auto* es = new EditSession(state, this, atoms_, discard ? nullptr : &cand_hwnd_, &retired_, discard);
        HRESULT result = S_OK;
        DWORD flags = TF_ES_READWRITE | (sync ? TF_ES_SYNC : TF_ES_ASYNCDONTCARE);
        HRESULT hr = state->ctx->RequestEditSession(client_, es, flags, &result);
        if (sync && (hr == TF_E_SYNCHRONOUS || result == TF_E_SYNCHRONOUS))
            hr = state->ctx->RequestEditSession(client_, es, TF_ES_READWRITE | TF_ES_ASYNCDONTCARE, &result);
        if (FAILED(hr) || FAILED(result)) {
            state->pending.clear();
            if (!discard) server_.Die(L"edit rejected");
        }
        es->Release();
    }

    void ClearLocal() {
        // Detach the old generation before requesting cleanup; pending edits
        // retain that context and cannot write into a newly focused control.
        auto old = state_;
        state_.reset();
        if (old) {
            old->active = false;
            old->pending.clear();
            retired_.push_back(old);
            if (old->composition && client_ != TF_CLIENTID_NULL) RequestEdit(old, true, true);
        }
        retired_.erase(std::remove_if(retired_.begin(), retired_.end(),
            [](const std::shared_ptr<CompositionState>& old) { return !old->composition; }), retired_.end());
        composing_ = converted_ = pending_ = pending_timer_ = false;
        timer_at_ = 0; n_tokens_ = 0; focus_ = -1; page_ = 0; has_next_page_ = false;
        last_comp_.clear();
        for (bool& eaten : down_eaten_) eaten = false;
        toggle_down_ = toggle_eaten_ = false;
        HideCands();
    }

    void ResetFocus() {
        ClearLocal();
        if (server_.Alive()) {
            std::string bytes; ServerReply reply;
            if (!server_.Send("{\"op\":\"reset\"}", bytes, kKeyBudgetMs) || !ParseReply(bytes, reply))
                server_.Die(L"reset failed");
            // reset preserves the mode. Do not apply any text from a focus callback.
        }
        // Check on entry too. If the read lock is unavailable here, keep
        // timers disabled until a real key can obtain a synchronous read lock.
        scope_allowed_ = tm_ && !KeyboardDisabled(tm_, client_) && !KeyboardClosed(tm_);
    }

    // The VK the layout actually delivers for the toggle besides toggle_vk:
    // Alt+VK_OEM_3 becomes Alt+VK_KANJI on the Japanese 101 layout.
    static UINT ToggleAlias(UINT vk, unsigned mods) {
        return vk == VK_OEM_3 && (mods & 4) ? VK_KANJI : 0;
    }
    bool IsToggleVk(UINT vk) const {
        const auto& s = server_.settings;
        return vk == s.toggle_vk || (vk != 0 && vk == ToggleAlias(s.toggle_vk, s.toggle_mods));
    }

    void UnpreserveToggle(ITfKeystrokeMgr* km) {
        if (alias_preserved_) {
            HRESULT ahr = km->UnpreserveKey(GUID_OyayubiToggleKanji, &preserved_alias_);
            Log(L"UnpreserveKey toggle alias vk=%u modifiers=0x%x hr=%08lx",
                preserved_alias_.uVKey, preserved_alias_.uModifiers, ahr);
            alias_preserved_ = false;
        }
        if (!toggle_preserved_) return;
        HRESULT hr = km->UnpreserveKey(GUID_OyayubiToggle, &preserved_toggle_);
        Log(L"UnpreserveKey toggle vk=%u modifiers=0x%x hr=%08lx",
            preserved_toggle_.uVKey, preserved_toggle_.uModifiers, hr);
        toggle_preserved_ = false;
    }

    void UpdatePreservedKey() {
        // Activate must not wait for settings/IPC. Its timer completes the
        // registration on the TSF thread once settings are published, even
        // before the server connects. Never register a temporary default key.
        if (!server_.SettingsLoaded()) return;
        const auto& settings = server_.settings;
        if (preserve_attempted_ && preserved_toggle_.uVKey == settings.toggle_vk &&
            preserved_mods_ == settings.toggle_mods) return;
        ITfKeystrokeMgr* km = nullptr;
        HRESULT hr = tm_->QueryInterface(IID_ITfKeystrokeMgr, (void**)&km);
        if (FAILED(hr)) {
            Log(L"PreserveKey toggle QueryInterface hr=%08lx", hr);
            return;
        }
        UnpreserveToggle(km);
        preserved_toggle_ = {settings.toggle_vk, 0};
        preserved_mods_ = settings.toggle_mods;
        // tip.json uses shift=1/ctrl=2/alt=4, unlike TSF's modifier flags.
        if (preserved_mods_ & 1) preserved_toggle_.uModifiers |= TF_MOD_SHIFT;
        if (preserved_mods_ & 2) preserved_toggle_.uModifiers |= TF_MOD_CONTROL;
        if (preserved_mods_ & 4) preserved_toggle_.uModifiers |= TF_MOD_ALT;
        // TSF has no Win modifier flag; OnPreservedKey checks the full mask.
        preserve_attempted_ = true;
        hr = km->PreserveKey(client_, GUID_OyayubiToggle, &preserved_toggle_, nullptr, 0);
        toggle_preserved_ = SUCCEEDED(hr);
        Log(L"PreserveKey toggle vk=%u mods=0x%x modifiers=0x%x hr=%08lx",
            preserved_toggle_.uVKey, preserved_mods_, preserved_toggle_.uModifiers, hr);
        if (UINT alias = ToggleAlias(settings.toggle_vk, preserved_mods_)) {
            preserved_alias_ = {alias, preserved_toggle_.uModifiers};
            HRESULT ahr = km->PreserveKey(client_, GUID_OyayubiToggleKanji, &preserved_alias_, nullptr, 0);
            alias_preserved_ = SUCCEEDED(ahr);
            Log(L"PreserveKey toggle alias vk=%u modifiers=0x%x hr=%08lx",
                preserved_alias_.uVKey, preserved_alias_.uModifiers, ahr);
        }
        km->Release();
        test_alt_logged_ = down_alt_logged_ = false;
    }

    void TraceAltToggle(const wchar_t* source, UINT vk, bool& logged) {
        if (logged) return;
        TipConfig defaults;
        const auto& settings = server_.SettingsLoaded() ? server_.settings : defaults;
        unsigned mods = HeldMods();
        if ((vk != settings.toggle_vk && vk != ToggleAlias(settings.toggle_vk, settings.toggle_mods))
            || !(mods & 4)) return;
        logged = true;
        Log(L"%s observed Alt+toggle_vk vk=%u mods=0x%x ready=%u direct=%u",
            source, vk, mods, (unsigned)server_.Alive(), (unsigned)direct_);
    }

    void SyncConnection() {
        UpdatePreservedKey();
        bool alive = server_.Alive();
        LONG generation = server_.Generation();
        if (!alive || generation != connection_generation_) {
            ClearLocal();
            connection_generation_ = generation;
        } else if (state_ && !state_->active) {
            ResetFocus();
        }
    }

    // Returns whether the previous reply was pending.
    bool NoteReply(const ServerReply& r) {
        bool was = pending_;
        pending_ = r.pending;
        direct_ = r.direct;
        server_.RememberDirect(direct_);
        last_comp_ = r.composition;
        n_tokens_ = r.n_tokens; focus_ = r.focus; page_ = r.page; has_next_page_ = r.has_next_page;
        composing_ = !r.composition.empty() || r.n_tokens > 0 || r.has_timer;
        converted_ = r.converted || r.pending;
        pending_timer_ = r.has_timer && !r.direct;
        timer_at_ = r.timer;
        cands_ = r.candidates;
        cand_index_ = r.cand_index;
        if (!r.direct && r.converted && !r.candidates.empty()) ShowCands();
        else HideCands();
        return was;
    }

    bool ExchangeReply(const std::string& request, ServerReply& r, DWORD budget) {
        std::string bytes;
        if (!server_.Send(request, bytes, budget, connection_generation_) || !ParseReply(bytes, r)) {
            server_.Die(L"request failed (fail-open)");
            ClearLocal();
            return false;
        }
        return true;
    }

    void RequestCandidatePosition() {
        if (!state_ || !state_->active || !state_->show_candidates || state_->position_pending) return;
        auto state = state_;
        state->position_pending = true;
        auto* es = new EditSession(state, this, atoms_, &cand_hwnd_, &retired_, false, true);
        HRESULT result = S_OK;
        state->ctx->RequestEditSession(client_, es, TF_ES_READ | TF_ES_ASYNCDONTCARE, &result);
        es->Release();
    }

    void OnTimerImpl() {
        SyncConnection();
        // A preserved system key's release need not reach OnKeyUp. Use the
        // thread's queued key state so the repeat latch also clears in that case.
        if (toggle_down_ && !(GetKeyState(toggle_active_vk_) & 0x8000)) {
            down_eaten_[toggle_active_vk_] = false;
            toggle_down_ = toggle_eaten_ = false;
        }
        if (!server_.Alive()) return;
        // A timer cannot depend on a synchronous input-scope edit lock.
        // Scope was checked on the key that armed it; queued edits check again.
        if (!scope_allowed_ || KeyboardDisabled(tm_, client_, false) || KeyboardClosed(tm_)) {
            if (state_ || composing_ || toggle_down_) ResetFocus();
            return;
        }
        ITfContext* ctx = FocusContext();
        bool same = !state_ || (ctx && ctx == state_->ctx);
        if (ctx) ctx->Release();
        if (!same) { ResetFocus(); return; }
        // GetTextExt can yield TS_E_NOLAYOUT after SetText. Retry with a read-only
        // cookie on subsequent timer ticks, after the application lays out.
        RequestCandidatePosition();
        if (!pending_timer_ || NowMs() < timer_at_) return;
        char buf[80];
        sprintf_s(buf, "{\"op\":\"timeout\",\"t\":%llu}", (unsigned long long)NowMs());
        ServerReply r;
        if (!ExchangeReply(buf, r, kKeyBudgetMs)) return;
        bool was_pending = NoteReply(r);
        // Still pending: the display has not changed, do not rewrite it every tick.
        if (r.pending && was_pending && r.commit.empty()) return;
        Apply(r, false);
    }
    void OnTimer() {
        __try { OnTimerImpl(); }
        __except (EXCEPTION_EXECUTE_HANDLER) { Log(L"SEH in OnTimer"); }
    }

    void Apply(const ServerReply& r, bool sync) {
        ITfContext* ctx = FocusContext();
        if (!ctx) return;
        if (state_ && state_->ctx != ctx) { ctx->Release(); ResetFocus(); return; }
        if (!state_) state_ = std::make_shared<CompositionState>(ctx, &server_);
        state_->show_candidates = !r.direct && r.converted && !r.candidates.empty();
        ctx->Release();
        state_->pending.push_back(r);
        RequestEdit(state_, sync);
    }

    bool Eligible() {
        return tm_ && !KeyboardDisabled(tm_, client_) && !KeyboardClosed(tm_);
    }
    bool ToggleKey(UINT vk) const {
        return IsToggleVk(vk) && HeldMods() == server_.settings.toggle_mods;
    }
    bool CanEat(UINT vk) {
        if (!server_.Alive() || !Eligible()) return false;
        if (ToggleKey(vk) || (toggle_down_ && vk == toggle_active_vk_)) return true;
        if (direct_) return false;
        bool current = server_.Generation() == connection_generation_;
        // A pending conversion always resolves to converted with candidates.
        return WantVk(vk, current && composing_, current && converted_,
                      current && (!cands_.empty() || pending_));
    }

    bool HandleKeyImpl(bool down, UINT vk) {
        bool before_composing = composing_, before_converted = converted_;
        bool before_candidates = !cands_.empty() || pending_;
        bool before_timer = pending_timer_;
        int before_tokens = n_tokens_;
        char buf[128];
        sprintf_s(buf, "{\"op\":\"%s\",\"vk\":%u,\"t\":%llu,\"mods\":", down ? "down" : "up", vk,
                  (unsigned long long)NowMs());
        ServerReply r;
        if (!ExchangeReply(std::string(buf) + ModsJson(down ? HeldMods() : down_mods_[vk]) + "}", r, kKeyBudgetMs)) return false;
        bool eat = false;
        if (!r.direct && !r.pass_through) {
            // A false pass_through is not a consumed bit. Combine the role at
            // send time with the reply's input/candidate/timer mask.
            bool state_mask = before_composing || before_tokens > 0 || before_timer ||
                r.n_tokens > 0 || r.has_timer || r.converted || !r.candidates.empty() ||
                !r.composition.empty() || !r.commit.empty();
            if (!down) eat = true;  // only actual consumed downs send an up
            else if (CharacterVk(vk) || vk == VK_SPACE) eat = state_mask;
            else if (vk == VK_BACK || vk == VK_RETURN || vk == VK_ESCAPE) eat = before_composing && state_mask;
            else if (vk == VK_F7) eat = true;
            else if (vk == VK_F6) eat = before_composing || before_converted;
            else if (vk == VK_F8 || vk == VK_F9 || vk == VK_F10) eat = before_composing || before_converted;
            else if (vk == VK_TAB) eat = r.predicting;
            else eat = before_converted && WantVk(vk, before_composing, before_converted, before_candidates);
        }
        bool was_pending = NoteReply(r);
        if (server_.settings.log_text)
            Log(L"%s vk=%u comp=%s commit=%s", down ? L"down" : L"up", vk,
                r.composition.c_str(), r.commit.c_str());
        if (!(r.pending && was_pending && r.commit.empty())) Apply(r, true);
        return eat;
    }
    bool HandleKey(bool down, UINT vk) {
        bool eat = false;
        __try { eat = HandleKeyImpl(down, vk); }
        __except (EXCEPTION_EXECUTE_HANDLER) {
            Log(L"SEH in HandleKey vk=%u", vk);
            server_.Die(L"key exception");
            eat = false;
        }
        return eat;
    }

    bool ToggleImpl() {
        ServerReply r;
        if (!ExchangeReply("{\"op\":\"toggle_direct\"}", r, kKeyBudgetMs)) return false;
        ClearLocal();
        NoteReply(r);
        return !r.pass_through;
    }
    bool Toggle() {
        bool eaten = false;
        bool before = direct_;
        __try { eaten = ToggleImpl(); }
        __except (EXCEPTION_EXECUTE_HANDLER) {
            Log(L"SEH in toggle");
            server_.Die(L"toggle exception");
        }
        Log(L"Toggle result=%u direct_before=%u direct=%u",
            (unsigned)eaten, (unsigned)before, (unsigned)direct_);
        return eaten;
    }

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

    HRESULT STDMETHODCALLTYPE Activate(ITfThreadMgr* ptim, TfClientId tid) override {
        return ActivateEx(ptim, tid, 0);
    }
    HRESULT STDMETHODCALLTYPE Deactivate() override {
        ClearLocal();
        if (timer_hwnd_) {
            KillTimer(timer_hwnd_, 1);
            DestroyWindow(timer_hwnd_);
            timer_hwnd_ = nullptr;
        }
        if (cand_hwnd_) {
            DestroyWindow(cand_hwnd_);
            cand_hwnd_ = nullptr;
        }
        server_.Stop();
        composing_ = false;
        pending_timer_ = false;
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
        preserve_attempted_ = false;
        test_alt_logged_ = down_alt_logged_ = false;
        return S_OK;
    }
    // ITfLangBarItemButton: one language-bar button that opens the
    // management window. The DLL carries no icon resource, so GetIcon
    // hands back none and the bar shows the item's description.
    HRESULT STDMETHODCALLTYPE GetInfo(TF_LANGBARITEMINFO* info) override {
        if (!info) return E_INVALIDARG;
        info->clsidService = CLSID_OyayubiTip;
        info->guidItem = GUID_OyayubiLangBar;
        info->dwStyle = TF_LBI_STYLE_BTN_BUTTON | TF_LBI_STYLE_SHOWNINTRAY;
        info->ulSort = 0;
        wcsncpy_s(info->szDescription, ARRAYSIZE(info->szDescription),
                  L"oyayubi-ime 管理", _TRUNCATE);
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
        *tip = SysAllocString(L"oyayubi-ime の管理ウィンドウを開く");
        return *tip ? S_OK : E_OUTOFMEMORY;
    }
    HRESULT STDMETHODCALLTYPE OnClick(TfLBIClick, POINT, const RECT*) override {
        server_.SpawnAdminUi();
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE InitMenu(ITfMenu*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnMenuSelect(UINT) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE GetIcon(HICON* icon) override {
        if (!icon) return E_INVALIDARG;
        *icon = nullptr;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE GetText(BSTR* text) override {
        if (!text) return E_INVALIDARG;
        *text = SysAllocString(L"oyayubi-ime 管理");
        return *text ? S_OK : E_OUTOFMEMORY;
    }

    HRESULT STDMETHODCALLTYPE ActivateEx(ITfThreadMgr* ptim, TfClientId tid, DWORD) override {
        tm_ = ptim;
        tm_->AddRef();
        client_ = tid;
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
        EnsureTimer();
        SetKeyboardOpen(tm_, client_, TRUE);
        Log(L"ActivateEx client=%u (connector worker, no wait)", (unsigned)tid);
        if (!server_.Start()) Log(L"connector start failed — keys pass through");
        UpdatePreservedKey();
        ITfLangBarItemMgr* lbm = nullptr;
        if (SUCCEEDED(tm_->QueryInterface(IID_ITfLangBarItemMgr, (void**)&lbm))) {
            if (SUCCEEDED(lbm->AddItem(this))) langbar_ = true;
            lbm->Release();
        }
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE OnSetFocus(ITfDocumentMgr*, ITfDocumentMgr*) override {
        ResetFocus(); return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnInitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnUninitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPushContext(ITfContext*) override { ResetFocus(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPopContext(ITfContext*) override { ResetFocus(); return S_OK; }
    HRESULT STDMETHODCALLTYPE OnSetFocus(BOOL) override { ResetFocus(); return S_OK; }

    HRESULT STDMETHODCALLTYPE OnTestKeyDown(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        TraceAltToggle(L"OnTestKeyDown", VkOf(w), test_alt_logged_);
        *e = CanEat(VkOf(w)) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnTestKeyUp(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        UINT vk = VkOf(w);
        // Read-only candidate check. No IPC, reset or state mutation here.
        *e = server_.Alive() && Eligible() &&
             ((toggle_down_ && vk == toggle_active_vk_ && toggle_eaten_) ||
              (!direct_ && down_eaten_[vk])) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyDown(ITfContext*, WPARAM w, LPARAM l, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        *e = FALSE;
        UINT vk = VkOf(w);
        TraceAltToggle(L"OnKeyDown", vk, down_alt_logged_);
        SyncConnection();
        if (!Eligible()) { ResetFocus(); return S_OK; }
        scope_allowed_ = true;
        if (!server_.Alive()) return S_OK;
        ITfContext* ctx = FocusContext();
        bool changed = state_ && (!ctx || state_->ctx != ctx);
        if (ctx) ctx->Release();
        if (changed) { ResetFocus(); return S_OK; }
        if (toggle_down_ && vk == toggle_active_vk_) {
            *e = toggle_eaten_ ? TRUE : FALSE;
            return S_OK;
        }
        // Toggle precedes the ordinary Alt/Ctrl/Win exclusion. The repeat bit
        // also prevents a failed initial exchange from turning into a toggle.
        if (ToggleKey(vk)) {
            Log(L"OnKeyDown ToggleKey=true vk=%u repeat=%u", vk,
                (unsigned)((l & (LPARAM(1) << 30)) != 0));
            if (l & (LPARAM(1) << 30)) return S_OK;
            bool eaten = Toggle();
            toggle_down_ = true; toggle_eaten_ = eaten; toggle_active_vk_ = vk;
            down_eaten_[vk] = eaten;
            *e = eaten ? TRUE : FALSE;
            return S_OK;
        }
        if (!CanEat(vk)) return S_OK;
        bool eaten = HandleKey(true, vk);
        // Do not erase an earlier consumed down on a repeat; a connection
        // failure clears all down state in ClearLocal().
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
        SyncConnection();
        if (!Eligible()) { ResetFocus(); return S_OK; }
        scope_allowed_ = true;
        if (toggle_down_ && vk == toggle_active_vk_) {
            *e = server_.Alive() && toggle_eaten_ ? TRUE : FALSE;
            toggle_down_ = toggle_eaten_ = false;
            down_eaten_[vk] = false;
            return S_OK;
        }
        bool promised = down_eaten_[vk];
        down_eaten_[vk] = false;
        if (!promised || direct_ || !server_.Alive()) return S_OK;
        // Use the consumed down's modifiers on release so a newly pressed
        // Ctrl/Alt cannot hide the release from the FSM.
        // A successful paired release is consumed; a failed IPC is fail-open.
        bool handled = HandleKey(false, vk);
        *e = handled ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnPreservedKey(ITfContext*, REFGUID guid, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        *e = FALSE;
        bool alias = guid == GUID_OyayubiToggleKanji;
        if (guid != GUID_OyayubiToggle && !alias) return S_OK;
        SyncConnection();
        bool eligible = Eligible();
        Log(L"OnPreservedKey toggle alias=%u ready=%u eligible=%u direct=%u", (unsigned)alias,
            (unsigned)server_.Alive(), (unsigned)eligible, (unsigned)direct_);
        if (!eligible) { ResetFocus(); return S_OK; }
        scope_allowed_ = true;
        if (!server_.Alive()) return S_OK;
        ITfContext* ctx = FocusContext();
        bool changed = state_ && (!ctx || state_->ctx != ctx);
        if (ctx) ctx->Release();
        if (changed) { ResetFocus(); return S_OK; }
        UINT vk = alias ? preserved_alias_.uVKey : server_.settings.toggle_vk;
        if (!ToggleKey(vk)) {
            Log(L"OnPreservedKey toggle modifiers mismatch mods=0x%x expected=0x%x",
                HeldMods(), server_.settings.toggle_mods);
            return S_OK;
        }
        // Share the latch with the fallback so repeats (or both callbacks for
        // one press) cannot toggle twice. Key-up or the timer releases it.
        if (!toggle_down_) {
            bool eaten = Toggle();
            toggle_down_ = true; toggle_eaten_ = eaten; toggle_active_vk_ = vk;
            down_eaten_[vk] = eaten;
        }
        *e = toggle_eaten_ ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnCompositionTerminated(TfEditCookie, ITfComposition* c) override {
        if (state_ && !state_->ending && state_->composition == c) {
            state_->composition->Release();
            state_->composition = nullptr;
            ResetFocus();
        }
        return S_OK;
    }
};

class Factory : public IClassFactory {
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
        if (lock)
            InterlockedIncrement(&g_locks);
        else
            InterlockedDecrement(&g_locks);
        return S_OK;
    }
};

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

static HRESULT RegisterComRoot(HKEY root) {
    std::wstring clsid = L"Software\\Classes\\CLSID\\" + GuidStr(CLSID_OyayubiTip);
    HRESULT hr = SetSz(root, clsid.c_str(), nullptr, L"NicolaIME");
    if (FAILED(hr)) return hr;
    std::wstring inproc = clsid + L"\\InprocServer32";
    hr = SetSz(root, inproc.c_str(), nullptr, DllPath().c_str());
    if (FAILED(hr)) return hr;
    return SetSz(root, inproc.c_str(), L"ThreadingModel", L"Apartment");
}

static HRESULT RegisterCom() {
    // CorvusSKK / SampleIME: HKCR (elevated → HKLM). Also HKCU so a
    // non-admin DllRegisterServer still finds the InprocServer32.
    HRESULT hr = RegisterComRoot(HKEY_CLASSES_ROOT);
    Log(L"RegisterCom HKCR %08lx path=%s", hr, DllPath().c_str());
    RegisterComRoot(HKEY_CURRENT_USER);
    return hr;
}

static HRESULT RegisterCategories(BOOL add) {
    ITfCategoryMgr* cat = nullptr;
    if (FAILED(CoCreateInstance(CLSID_TF_CategoryMgr, nullptr, CLSCTX_INPROC_SERVER,
                                IID_ITfCategoryMgr, (void**)&cat)))
        return E_FAIL;
    // Re-registration must remove the category advertised by older DLLs.
    cat->UnregisterCategory(CLSID_OyayubiTip, GUID_TFCAT_TIPCAP_SECUREMODE, CLSID_OyayubiTip);
    for (const GUID& g : kCats) {
        if (add)
            cat->RegisterCategory(CLSID_OyayubiTip, g, CLSID_OyayubiTip);
        else
            cat->UnregisterCategory(CLSID_OyayubiTip, g, CLSID_OyayubiTip);
    }
    cat->Release();
    return S_OK;
}

static HRESULT RegisterProfile(BOOL add) {
    // CorvusSKK / SampleIME: ProfileMgr only. The old Register+AddLanguageProfile
    // pair plus this API together made Win11 attach the wrong CJK engine.
    ITfInputProcessorProfileMgr* mgr = nullptr;
    HRESULT hr = CoCreateInstance(CLSID_TF_InputProcessorProfiles, nullptr, CLSCTX_INPROC_SERVER,
                                  IID_ITfInputProcessorProfileMgr, (void**)&mgr);
    Log(L"CoCreate ProfileMgr %08lx", hr);
    if (FAILED(hr) || !mgr) return FAILED(hr) ? hr : E_FAIL;
    LANGID ja = MAKELANGID(LANG_JAPANESE, SUBLANG_JAPANESE_JAPAN);
    if (add) {
        std::wstring path = DllPath();
        hr = mgr->RegisterProfile(CLSID_OyayubiTip, ja, GUID_OyayubiProfile, L"NicolaIME",
                                  (ULONG)wcslen(L"NicolaIME"), path.c_str(), (ULONG)path.size(), 0,
                                  nullptr, 0, TRUE, 0);
        Log(L"RegisterProfile ja=%04x %08lx", (unsigned)ja, hr);
    } else {
        hr = mgr->UnregisterProfile(CLSID_OyayubiTip, ja, GUID_OyayubiProfile, TF_URP_ALLPROFILES);
        Log(L"UnregisterProfile %08lx", hr);
    }
    mgr->Release();
    RegisterCategories(add);
    return hr;
}

BOOL APIENTRY DllMain(HINSTANCE h, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_hinst = h;
        // /MT requires CRT thread attach/detach notifications.
    }
    return TRUE;
}

STDAPI DllCanUnloadNow() { return g_locks ? S_FALSE : S_OK; }

STDAPI DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv) {
    if (!IsEqualCLSID(rclsid, CLSID_OyayubiTip)) return CLASS_E_CLASSNOTAVAILABLE;
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

STDAPI DllUnregisterServer() {
    HRESULT co = CoInitialize(nullptr);
    RegisterProfile(FALSE);
    std::wstring clsid = L"Software\\Classes\\CLSID\\" + GuidStr(CLSID_OyayubiTip);
    RegDeleteTreeW(HKEY_CURRENT_USER, clsid.c_str());
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, clsid.c_str());
    std::wstring tip = L"Software\\Microsoft\\CTF\\TIP\\" + GuidStr(CLSID_OyayubiTip);
    RegDeleteTreeW(HKEY_CURRENT_USER, tip.c_str());
    RegDeleteTreeW(HKEY_LOCAL_MACHINE, tip.c_str());
    if (co == S_OK) CoUninitialize();
    return S_OK;
}
