// Syntax- and type-checking the TIP with llvm-mingw on Linux only. Never used
// to build the product: the Windows SDK has the real declarations. mingw-w64
// has no ctffunc.h and lacks several TSF declarations; they are declared here
// with the SDK's method signatures (interface IDs are not needed for
// -fsyntax-only and are left zero).
#pragma once
#include <msctf.h>
#include <ctfutb.h>

#ifndef __ITfDisplayAttributeInfo_INTERFACE_DEFINED__
#define __ITfDisplayAttributeInfo_INTERFACE_DEFINED__
DEFINE_GUID(IID_ITfDisplayAttributeInfo, 0x70528852, 0x2f26, 0x4aea, 0x8c, 0x96, 0x21, 0x51, 0x50, 0x57, 0x89, 0x32);
struct ITfDisplayAttributeInfo : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE GetGUID(GUID* pguid) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetDescription(BSTR* pbstrDesc) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetAttributeInfo(TF_DISPLAYATTRIBUTE* pda) = 0;
    virtual HRESULT STDMETHODCALLTYPE SetAttributeInfo(const TF_DISPLAYATTRIBUTE* pda) = 0;
    virtual HRESULT STDMETHODCALLTYPE Reset() = 0;
};
#endif

#ifndef __IEnumTfDisplayAttributeInfo_INTERFACE_DEFINED__
#define __IEnumTfDisplayAttributeInfo_INTERFACE_DEFINED__
DEFINE_GUID(IID_IEnumTfDisplayAttributeInfo, 0x7cef04d7, 0xcb75, 0x4e80, 0xa7, 0xab, 0x5f, 0x5b, 0xc7, 0xd3, 0x32, 0xde);
struct IEnumTfDisplayAttributeInfo : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE Clone(IEnumTfDisplayAttributeInfo** ppEnum) = 0;
    virtual HRESULT STDMETHODCALLTYPE Next(ULONG ulCount, ITfDisplayAttributeInfo** rgInfo, ULONG* pcFetched) = 0;
    virtual HRESULT STDMETHODCALLTYPE Reset() = 0;
    virtual HRESULT STDMETHODCALLTYPE Skip(ULONG ulCount) = 0;
};
#endif

#ifndef __ITfDisplayAttributeProvider_INTERFACE_DEFINED__
#define __ITfDisplayAttributeProvider_INTERFACE_DEFINED__
DEFINE_GUID(IID_ITfDisplayAttributeProvider, 0xfee47777, 0x163c, 0x4769, 0x99, 0x6a, 0x6e, 0x9c, 0x50, 0xad, 0x8f, 0x54);
struct ITfDisplayAttributeProvider : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE EnumDisplayAttributeInfo(IEnumTfDisplayAttributeInfo** ppEnum) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetDisplayAttributeInfo(REFGUID guid, ITfDisplayAttributeInfo** ppInfo) = 0;
};
#endif

#define SHIM_IID(name) DEFINE_GUID(name, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
SHIM_IID(GUID_TFCAT_TIPCAP_COMLESS);
SHIM_IID(GUID_TFCAT_TIPCAP_INPUTMODECOMPARTMENT);
SHIM_IID(GUID_TFCAT_TIPCAP_IMMERSIVESUPPORT);
SHIM_IID(GUID_TFCAT_TIPCAP_SYSTRAYSUPPORT);
SHIM_IID(GUID_LBI_INPUTMODE);
SHIM_IID(IID_ITfTextInputProcessorEx);
SHIM_IID(IID_ITfTextLayoutSink);
SHIM_IID(IID_ITfLangBarItemButton);
#define TF_CLIENTID_NULL ((TfClientId)0)
#define TF_INVALID_GUIDATOM ((TfGuidAtom)0)
#define TF_LBMENUF_GRAYED 0x4
#define TF_LBI_STYLE_SHOWNINTRAY 0x2
#define TF_LBI_STYLE_BTN_BUTTON 0x10000
#define TF_LBI_ICON 0x1
#define TF_LBI_TEXT 0x2
#define TF_LBI_TOOLTIP 0x4
#define TF_URP_ALLPROFILES 0x2

struct ITfTextInputProcessorEx : public ITfTextInputProcessor {
    virtual HRESULT STDMETHODCALLTYPE ActivateEx(ITfThreadMgr* ptim, TfClientId tid, DWORD dwFlags) = 0;
};
typedef enum { TF_LC_CREATE = 0, TF_LC_CHANGE = 1, TF_LC_DESTROY = 2 } TfLayoutCode;
struct ITfTextLayoutSink : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE OnLayoutChange(ITfContext* pic, TfLayoutCode lcode,
                                                     ITfContextView* pView) = 0;
};
typedef enum { TF_LBI_CLK_RIGHT = 1, TF_LBI_CLK_LEFT = 2 } TfLBIClick;
struct ITfMenu : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE AddMenuItem(UINT uId, DWORD dwFlags, HBITMAP hbmp,
                                                  HBITMAP hbmpMask, const WCHAR* pch, ULONG cch,
                                                  ITfMenu** ppMenu) = 0;
};
#ifndef __ITfLangBarItem_INTERFACE_DEFINED__
struct ITfLangBarItem : public IUnknown {
    virtual HRESULT STDMETHODCALLTYPE GetInfo(TF_LANGBARITEMINFO* pInfo) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetStatus(DWORD* pdwStatus) = 0;
    virtual HRESULT STDMETHODCALLTYPE Show(BOOL fShow) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetTooltipString(BSTR* pbstrToolTip) = 0;
};
#endif
struct ITfLangBarItemButton : public ITfLangBarItem {
    virtual HRESULT STDMETHODCALLTYPE OnClick(TfLBIClick click, POINT pt, const RECT* prcArea) = 0;
    virtual HRESULT STDMETHODCALLTYPE InitMenu(ITfMenu* pMenu) = 0;
    virtual HRESULT STDMETHODCALLTYPE OnMenuSelect(UINT wID) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetIcon(HICON* phIcon) = 0;
    virtual HRESULT STDMETHODCALLTYPE GetText(BSTR* pbstrText) = 0;
};
