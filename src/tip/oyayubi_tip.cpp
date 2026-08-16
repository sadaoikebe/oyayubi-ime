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

#include <windows.h>
#include <oleauto.h>
#include <initguid.h>
#include <msctf.h>
#include <cstdio>
#include <string>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "oleaut32.lib")
#pragma comment(lib, "uuid.lib")

// {A7C4E201-0B3A-4F11-9E61-0C1A0B7E0A01}
static const CLSID CLSID_OyayubiTip = {
    0xa7c4e201, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};
// {A7C4E202-0B3A-4F11-9E61-0C1A0B7E0A01}
static const GUID GUID_OyayubiProfile = {
    0xa7c4e202, 0x0b3a, 0x4f11, {0x9e, 0x61, 0x0c, 0x1a, 0x0b, 0x7e, 0x0a, 0x01}};

static const GUID kCats[] = {
    GUID_TFCAT_TIP_KEYBOARD,
    GUID_TFCAT_TIPCAP_SECUREMODE,
    GUID_TFCAT_TIPCAP_COMLESS,
    GUID_TFCAT_TIPCAP_INPUTMODECOMPARTMENT,
    GUID_TFCAT_TIPCAP_UIELEMENTENABLED,
    GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT,
    GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT,
};

static LONG g_locks = 0;
static HINSTANCE g_hinst = nullptr;

static const DWORD kKeyBudgetMs = 150;  // hard cap on the TSF key thread
static const DWORD kPeekMs = 0;         // timer / ready pump: never sleep
static const int kMaxSoftFails = 8;     // Mozc keeps the server; only die if wedged

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

// NICOLA keys. Edit keys only while composing (SampleIME IsVirtualKeyNeed).
static bool WantVk(UINT vk, bool composing) {
    if (ModHeld()) return false;
    if (vk >= 'A' && vk <= 'Z') return true;
    if (vk == VK_SPACE) return true;
    if (vk == VK_RETURN || vk == VK_BACK || vk == VK_ESCAPE) return composing;
    switch (vk) {
        case VK_OEM_1:
        case VK_OEM_COMMA:
        case VK_OEM_PERIOD:
        case VK_OEM_2:
            return true;
        default:
            return false;
    }
}

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
    // dist\NicolaIME2.dll → repo root
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

struct ServerReply {
    std::wstring composition;
    std::wstring commit;
    bool converted = false;
    int n_tokens = 0;
    bool has_timer = false;
    ULONGLONG timer = 0;
};

static std::string GrabJsonString(const std::string& s, const char* key) {
    std::string k = std::string("\"") + key + "\":";
    auto p = s.find(k);
    if (p == std::string::npos) return {};
    p += k.size();
    while (p < s.size() && (s[p] == ' ')) p++;
    if (p < s.size() && s[p] == '"') {
        p++;
        std::string o;
        for (; p < s.size() && s[p] != '"'; ++p) {
            if (s[p] == '\\' && p + 1 < s.size()) {
                ++p;
                o.push_back(s[p]);
            } else {
                o.push_back(s[p]);
            }
        }
        return o;
    }
    return {};
}

static bool GrabJsonInt(const std::string& s, const char* key, long long& out) {
    std::string k = std::string("\"") + key + "\":";
    auto p = s.find(k);
    if (p == std::string::npos) return false;
    p += k.size();
    while (p < s.size() && s[p] == ' ') p++;
    if (p + 3 < s.size() && s.compare(p, 4, "null") == 0) return false;
    char* end = nullptr;
    out = _strtoi64(s.c_str() + p, &end, 10);
    return end != s.c_str() + p;
}

static ServerReply ParseReply(const std::string& s) {
    ServerReply r;
    r.composition = Utf8ToWide(GrabJsonString(s, "composition"));
    r.commit = Utf8ToWide(GrabJsonString(s, "commit"));
    r.converted = s.find("\"converted\": true") != std::string::npos ||
                  s.find("\"converted\":true") != std::string::npos;
    long long n = 0;
    if (GrabJsonInt(s, "n_tokens", n)) r.n_tokens = (int)n;
    if (GrabJsonInt(s, "timer", n) && n > 0) {
        r.has_timer = true;
        r.timer = (ULONGLONG)n;
    }
    return r;
}

// --- Python server via anonymous pipes. Never sleep on the TSF thread. ---
struct Server {
    HANDLE in_w = nullptr, out_r = nullptr;
    PROCESS_INFORMATION pi{};
    bool spawned = false;
    bool ok = false;
    int soft_fails = 0;
    std::string acc;

    bool Alive() const {
        if (!spawned || !pi.hProcess) return false;
        return WaitForSingleObject(pi.hProcess, 0) == WAIT_TIMEOUT;
    }

    void Die(const wchar_t* why) {
        Log(L"server die: %s", why);
        ok = false;
        spawned = false;
        if (pi.hProcess) {
            TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hProcess);
            CloseHandle(pi.hThread);
            pi.hProcess = pi.hThread = nullptr;
        }
        if (in_w) {
            CloseHandle(in_w);
            in_w = nullptr;
        }
        if (out_r) {
            CloseHandle(out_r);
            out_r = nullptr;
        }
        acc.clear();
        soft_fails = 0;
    }

    // ActivateEx: spawn only. Do not wait for the dictionary.
    bool Spawn() {
        if (spawned && Alive()) return true;
        Stop();
        SECURITY_ATTRIBUTES sa{sizeof(sa), nullptr, TRUE};
        HANDLE in_r = nullptr, out_w = nullptr;
        if (!CreatePipe(&in_r, &in_w, &sa, 0)) return false;
        if (!CreatePipe(&out_r, &out_w, &sa, 0)) {
            CloseHandle(in_r);
            CloseHandle(in_w);
            in_w = nullptr;
            return false;
        }
        SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);

        STARTUPINFOW si{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = in_r;
        si.hStdOutput = out_w;
        wchar_t errlog[MAX_PATH];
        GetTempPathW(MAX_PATH, errlog);
        wcscat_s(errlog, L"oyayubi_server.err.log");
        SECURITY_ATTRIBUTES sa2{sizeof(sa2), nullptr, TRUE};
        HANDLE err = CreateFileW(errlog, GENERIC_WRITE, FILE_SHARE_READ, &sa2, CREATE_ALWAYS,
                                 FILE_ATTRIBUTE_NORMAL, nullptr);
        si.hStdError = err ? err : GetStdHandle(STD_ERROR_HANDLE);

        wchar_t py[MAX_PATH];
        if (!FindPython(py, MAX_PATH)) {
            Log(L"python.exe not found");
            CloseHandle(in_r);
            CloseHandle(out_w);
            if (err) CloseHandle(err);
            CloseHandle(in_w);
            CloseHandle(out_r);
            in_w = out_r = nullptr;
            return false;
        }
        wchar_t cmd[1024];
        swprintf_s(cmd, L"\"%s\" -u -m oyayubi.ime.server", py);
        std::wstring cwd = RepoDir();
        SetEnvironmentVariableW(L"PYTHONIOENCODING", L"utf-8");
        SetEnvironmentVariableW(L"PYTHONUTF8", L"1");

        BOOL created = CreateProcessW(nullptr, cmd, nullptr, nullptr, TRUE, CREATE_NO_WINDOW,
                                      nullptr, cwd.c_str(), &si, &pi);
        CloseHandle(in_r);
        CloseHandle(out_w);
        if (err) CloseHandle(err);
        if (!created) {
            Log(L"CreateProcess failed %lu", GetLastError());
            CloseHandle(in_w);
            CloseHandle(out_r);
            in_w = out_r = nullptr;
            return false;
        }
        spawned = true;
        ok = false;
        soft_fails = 0;
        acc.clear();
        Log(L"server spawned pid=%lu cwd=%s", pi.dwProcessId, cwd.c_str());
        return true;
    }

    void Stop() {
        if (in_w) {
            const char q[] = "{\"op\":\"quit\"}\n";
            DWORD w = 0;
            WriteFile(in_w, q, sizeof(q) - 1, &w, nullptr);
        }
        if (pi.hProcess) {
            if (WaitForSingleObject(pi.hProcess, 500) == WAIT_TIMEOUT)
                TerminateProcess(pi.hProcess, 1);
            CloseHandle(pi.hProcess);
            CloseHandle(pi.hThread);
            pi.hProcess = pi.hThread = nullptr;
        }
        if (in_w) CloseHandle(in_w);
        if (out_r) CloseHandle(out_r);
        in_w = out_r = nullptr;
        spawned = false;
        ok = false;
        soft_fails = 0;
        acc.clear();
    }

    // budget_ms==0: one non-blocking peek. No Sleep.
    bool ReadLine(std::string& out, DWORD budget_ms) {
        out.clear();
        ULONGLONG start = GetTickCount64();
        for (;;) {
            auto nl = acc.find('\n');
            if (nl != std::string::npos) {
                out = acc.substr(0, nl);
                if (!out.empty() && out.back() == '\r') out.pop_back();
                acc.erase(0, nl + 1);
                return true;
            }
            if (!out_r) return false;
            DWORD avail = 0;
            if (!PeekNamedPipe(out_r, nullptr, 0, nullptr, &avail, nullptr)) return false;
            if (avail == 0) {
                if (budget_ms == 0) return false;
                if (GetTickCount64() - start > budget_ms) return false;
                Sleep(1);
                continue;
            }
            char buf[512];
            DWORD n = 0;
            DWORD chunk = avail > sizeof(buf) ? (DWORD)sizeof(buf) : avail;
            if (!ReadFile(out_r, buf, chunk, &n, nullptr) || n == 0) return false;
            acc.append(buf, buf + n);
        }
    }

    void PumpReady() {
        if (ok) return;
        if (!spawned) return;
        if (!Alive()) {
            Die(L"exited before ready");
            return;
        }
        std::string line;
        while (ReadLine(line, kPeekMs)) {
            Log(L"server stdout: %hs", line.c_str());
            if (line.find("ready") != std::string::npos ||
                line.find("\"ok\": true") != std::string::npos ||
                line.find("\"ok\":true") != std::string::npos) {
                ok = true;
                Log(L"server ready");
                return;
            }
        }
    }

    void DrainStale() {
        std::string junk;
        while (ReadLine(junk, kPeekMs)) Log(L"drain stale: %hs", junk.c_str());
    }

    bool Send(const std::string& json, std::string& reply, DWORD budget_ms) {
        reply.clear();
        if (!ok || !Alive() || !in_w) return false;
        DrainStale();
        std::string line = json + "\n";
        DWORD w = 0;
        if (!WriteFile(in_w, line.data(), (DWORD)line.size(), &w, nullptr)) {
            Die(L"write failed");
            return false;
        }
        if (!ReadLine(reply, budget_ms)) {
            if (!Alive()) {
                Die(L"died mid-send");
                return false;
            }
            ++soft_fails;
            Log(L"reply timeout (soft %d) — keep server (Mozc)", soft_fails);
            if (soft_fails >= kMaxSoftFails) Die(L"too many timeouts");
            return false;
        }
        soft_fails = 0;
        return true;
    }
};

class EditSession : public ITfEditSession {
public:
    LONG refs_ = 1;
    ITfContext* ctx_;
    ITfComposition** comp_;
    ITfCompositionSink* sink_;
    std::wstring text_;
    bool commit_;

    EditSession(ITfContext* c, ITfComposition** comp, ITfCompositionSink* sink, std::wstring t,
                bool commit)
        : ctx_(c), comp_(comp), sink_(sink), text_(std::move(t)), commit_(commit) {
        ctx_->AddRef();
    }
    ~EditSession() { ctx_->Release(); }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void** pp) override {
        if (!pp) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ITfEditSession) {
            *pp = static_cast<ITfEditSession*>(this);
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

    HRESULT DoEditSessionImpl(TfEditCookie ec) {
        ITfRange* range = nullptr;
        if (*comp_) {
            (*comp_)->GetRange(&range);
        } else if (!text_.empty() && !commit_) {
            ITfInsertAtSelection* ins = nullptr;
            if (SUCCEEDED(ctx_->QueryInterface(IID_ITfInsertAtSelection, (void**)&ins))) {
                ins->InsertTextAtSelection(ec, 0, text_.c_str(), (LONG)text_.size(), &range);
                ins->Release();
            }
            ITfContextComposition* cc = nullptr;
            if (range && SUCCEEDED(ctx_->QueryInterface(IID_ITfContextComposition, (void**)&cc))) {
                cc->StartComposition(ec, range, sink_, comp_);
                cc->Release();
            }
        }
        if (range) {
            range->SetText(ec, 0, text_.c_str(), (LONG)text_.size());
            if ((commit_ || text_.empty()) && *comp_) {
                (*comp_)->EndComposition(ec);
                (*comp_)->Release();
                *comp_ = nullptr;
            }
            range->Release();
        }
        return S_OK;
    }

    // CorvusSKK KeyHandler.cpp: SEH so a bug does not kill the TSF host.
    HRESULT STDMETHODCALLTYPE DoEditSession(TfEditCookie ec) override {
        HRESULT hr = S_OK;
        __try {
            hr = DoEditSessionImpl(ec);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            hr = S_OK;
        }
        return hr;
    }
};

class Tip : public ITfTextInputProcessorEx,
            public ITfKeyEventSink,
            public ITfThreadMgrEventSink,
            public ITfCompositionSink {
public:
    LONG refs_ = 1;
    ITfThreadMgr* tm_ = nullptr;
    TfClientId client_ = TF_CLIENTID_NULL;
    DWORD cookie_tm_ = TF_INVALID_COOKIE;
    ITfComposition* composition_ = nullptr;
    Server server_;
    HWND timer_hwnd_ = nullptr;
    std::wstring last_comp_;
    bool composing_ = false;
    bool pending_timer_ = false;
    ULONGLONG timer_at_ = 0;
    bool down_eaten_[256]{};

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

    void NoteReply(const ServerReply& r) {
        last_comp_ = r.composition;
        composing_ = !r.composition.empty() || r.n_tokens > 0;
        pending_timer_ = r.has_timer;
        timer_at_ = r.timer;
    }

    void OnTimerImpl() {
        server_.PumpReady();
        if (!server_.ok) return;
        if (!server_.Alive()) {
            server_.Die(L"died on timer");
            return;
        }
        if (!pending_timer_ || NowMs() < timer_at_) return;
        char buf[80];
        sprintf_s(buf, "{\"op\":\"timeout\",\"t\":%llu}", (unsigned long long)NowMs());
        std::string reply;
        if (!server_.Send(buf, reply, 40)) return;
        auto r = ParseReply(reply);
        if (r.composition == last_comp_ && r.commit.empty()) {
            NoteReply(r);
            return;
        }
        NoteReply(r);
        Apply(r, /*sync=*/false);
    }

    void OnTimer() {
        __try {
            OnTimerImpl();
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            Log(L"SEH in OnTimer");
        }
    }

    void Apply(const ServerReply& r, bool sync) {
        ITfDocumentMgr* dm = nullptr;
        if (!tm_ || FAILED(tm_->GetFocus(&dm)) || !dm) return;
        ITfContext* ctx = nullptr;
        if (FAILED(dm->GetTop(&ctx)) || !ctx) {
            dm->Release();
            return;
        }
        std::wstring text = r.commit.empty() ? r.composition : r.commit;
        bool commit = !r.commit.empty();
        if (text.empty() && !composition_) {
            ctx->Release();
            dm->Release();
            return;
        }
        auto* es = new EditSession(ctx, &composition_, this, text, commit);
        HRESULT hrSession = S_OK;
        // SampleIME / Mozc prefer SYNC on the key path. CorvusSKK uses
        // ASYNCDONTCARE to avoid TF_E_SYNCHRONOUS. Try SYNC, then fall back.
        DWORD flags = TF_ES_READWRITE | (sync ? TF_ES_SYNC : TF_ES_ASYNCDONTCARE);
        HRESULT hr = ctx->RequestEditSession(client_, es, flags, &hrSession);
        if (sync && (hr == TF_E_SYNCHRONOUS || hrSession == TF_E_SYNCHRONOUS)) {
            ctx->RequestEditSession(client_, es, TF_ES_READWRITE | TF_ES_ASYNCDONTCARE,
                                    &hrSession);
        }
        es->Release();
        ctx->Release();
        dm->Release();
    }

    bool CanEat(UINT vk) {
        if (!server_.ok || !server_.Alive()) return false;
        if (KeyboardDisabled(tm_)) return false;
        if (KeyboardClosed(tm_)) return false;
        return WantVk(vk, composing_);
    }

    bool HandleKeyImpl(bool down, UINT vk) {
        char buf[96];
        sprintf_s(buf, "{\"op\":\"%s\",\"vk\":%u,\"t\":%llu}", down ? "down" : "up", vk,
                  (unsigned long long)NowMs());
        std::string reply;
        if (!server_.Send(buf, reply, kKeyBudgetMs)) {
            Log(L"%s send failed vk=%u (fail-open)", down ? L"down" : L"up", vk);
            return false;
        }
        auto r = ParseReply(reply);
        NoteReply(r);
        Log(L"%s vk=%u comp=%s commit=%s", down ? L"down" : L"up", vk, r.composition.c_str(),
            r.commit.c_str());
        Apply(r, /*sync=*/true);
        return true;
    }

    bool HandleKey(bool down, UINT vk) {
        bool ok = false;
        __try {
            ok = HandleKeyImpl(down, vk);
        } __except (EXCEPTION_EXECUTE_HANDLER) {
            Log(L"SEH in HandleKey vk=%u", vk);
            composing_ = false;
            pending_timer_ = false;
            ok = false;
        }
        return ok;
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
        if (timer_hwnd_) {
            KillTimer(timer_hwnd_, 1);
            DestroyWindow(timer_hwnd_);
            timer_hwnd_ = nullptr;
        }
        server_.Stop();
        composing_ = false;
        pending_timer_ = false;
        if (tm_) {
            ITfKeystrokeMgr* km = nullptr;
            if (SUCCEEDED(tm_->QueryInterface(IID_ITfKeystrokeMgr, (void**)&km))) {
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
        return S_OK;
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
        EnsureTimer();
        SetKeyboardOpen(tm_, client_, TRUE);
        Log(L"ActivateEx client=%u (spawn, no wait)", (unsigned)tid);
        // Must return immediately. Dictionary load happens in the child.
        if (!server_.Spawn()) Log(L"server spawn failed — keys pass through");
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE OnSetFocus(ITfDocumentMgr*, ITfDocumentMgr*) override {
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnInitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnUninitDocumentMgr(ITfDocumentMgr*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPushContext(ITfContext*) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE OnPopContext(ITfContext*) override { return S_OK; }

    HRESULT STDMETHODCALLTYPE OnSetFocus(BOOL) override { return S_OK; }

    HRESULT STDMETHODCALLTYPE OnTestKeyDown(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        server_.PumpReady();
        *e = CanEat(VkOf(w)) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnTestKeyUp(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        UINT vk = VkOf(w);
        *e = (down_eaten_[vk] || CanEat(vk)) ? TRUE : FALSE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyDown(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        UINT vk = VkOf(w);
        if (!CanEat(vk)) {
            *e = FALSE;
            return S_OK;
        }
        // Mozc: a failed engine call is that key only. Do not keep eating.
        if (!HandleKey(true, vk)) {
            *e = FALSE;
            return S_OK;
        }
        down_eaten_[vk] = true;
        *e = TRUE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnKeyUp(ITfContext*, WPARAM w, LPARAM, BOOL* e) override {
        if (!e) return E_INVALIDARG;
        UINT vk = VkOf(w);
        bool promised = down_eaten_[vk];
        down_eaten_[vk] = false;
        if (!promised && !CanEat(vk)) {
            *e = FALSE;
            return S_OK;
        }
        if (promised || server_.ok) HandleKey(false, vk);
        // If we ate the down, swallow the up even when the engine missed.
        *e = TRUE;
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE OnPreservedKey(ITfContext*, REFGUID, BOOL* e) override {
        if (e) *e = FALSE;
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE OnCompositionTerminated(TfEditCookie, ITfComposition* c) override {
        if (composition_ == c) {
            composition_->Release();
            composition_ = nullptr;
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
        DisableThreadLibraryCalls(h);
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
