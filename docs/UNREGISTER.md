# アンインストールが regsvr32 の終了コード 5 で止まる — 2026-11-14

## 症状

`packaging\uninstall.ps1` が、管理者権限の段で毎回止まる。

```text
中止: 登録解除に失敗しました: unregistering ...\NicolaIME3-b.dll failed (regsvr32 exit code 5)
```

ただし止まった時点で HKLM の COM 登録（`Classes\CLSID\{A7C4E201-…}`）と TSF の登録
（`CTF\TIP\{A7C4E201-…}`）は消えている。そのため、もう一度実行すると「登録されていない」扱いで
最後まで進む（`%LOCALAPPDATA%\oyayubi-ime` の削除などは 2 回目で行われる）。

## 切り分け

`DllUnregisterServer` の各手順の HRESULT を一時的にファイルへ書き出して、`dev-register.ps1` と
同じ手順で登録し、`uninstall.ps1` をそのまま実行した。

```text
UnregisterProfile 0 hr=00000000
UnregisterCategory 0 hr=80004005   （6 つのカテゴリすべて同じ）
...
RegDeleteTree 0 hr=00000000
DllUnregisterServer 0 hr=80004005
```

regsvr32 の終了コード 5 は「DllUnregisterServer が失敗を返した」。

## 原因

`RegisterProfile(FALSE)` は `UnregisterProfile` のあとで `UnregisterCategory` を呼んでいた。
`UnregisterProfile` が TIP のキー（`HKLM\SOFTWARE\Microsoft\CTF\TIP\{clsid}`）をカテゴリごと
消すので、その後の `UnregisterCategory` は消す相手がなく E_FAIL を返す。`DllUnregisterServer` は
最初の失敗を返す作り（I-6）なので、実際には全部消えているのに失敗として報告していた。
消し残しはなかった（HKLM の `CTF` 以下に CLSID を含むキーなし）。

## 直し方

登録解除の順番を、カテゴリ → プロファイルにした（SampleIME と同じ順）。登録の順番は変えていない。
直したあと同じ手順で、すべての手順が S_OK、`uninstall.ps1` が 1 回で「登録解除 OK」まで進むことを確かめた。

## HKCU の残り（同日に対応）

上を直したあとも、`uninstall.ps1` が成功したあとに次の 2 つが HKCU に残っていた。

- `HKCU\SOFTWARE\Microsoft\CTF\TIP\{A7C4E201-…}\LanguageProfile\0x00000411\{A7C4E202-…}`
  の `Enable = 0`（段 2 で言語一覧から外したときに Windows が書く、利用者ごとの設定）
- 空の `HKCU\Software\oyayubi-ime`（段 2 は Tag の値だけを消し、キーは残していた）

`uninstall.ps1` の段 5（ブローカーの終了と登録解除のあと）で、両方のキーを消すようにした。
`Software\oyayubi-ime` に入るのは Tag と揮発キー Session だけで、利用者のデータはない。
開いたままのアプリに残った古い DLL は、Tag が無いのでブローカーを起動せず、Session も作らない。
登録 → `uninstall.ps1` で、HKLM・HKCU・`%LOCALAPPDATA%` に何も残らないことを確かめた。

他の利用者の HKCU に書かれた分は、この利用者のスクリプトからは消さない。
