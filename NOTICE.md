# Third-party notices

Original code in this repository is under the MIT License (`LICENSE`).
The data and software that the installer fetches (the dictionary, the Python
runtime and its packages, the language model) are under their own terms.
Those terms do **not** relicense the IME source. They apply to the named
items only. None of them is stored in this repository or attached to a
release: `setup.ps1` downloads each one on the user's PC from the source
named below.

This project does **not** include GPL code. The QMK NICOLA implementation
(`nicola.c`) was used only as a specification of behavior; it is not copied
here. RIME/Weasel and similar GPL IME hosts are not linked.

## Mozc open-source dictionary

Location: `%LOCALAPPDATA%\yuragi-ime\shared\dict\<commit>\` when installed;
`third_party/mozc_oss_dict/` in a development checkout.

All files (`dictionary*.txt`, `connection_single_column.txt`, `suffix.txt`,
`id.def`, `evaluation.tsv`, `README.txt`, `README.md`) are fetched from
`src/data/dictionary_oss/` at the upstream commit pinned in
`data/dictionary-manifest.json` by `python -m tools.wp0.fetch_mozc_dict`
and checked against the SHA-256 in that manifest. None of them is stored
in git. Runtime conversion uses this dictionary.

Upstream: https://github.com/google/mozc  
The Mozc project itself is BSD-3-Clause (Google Inc.). Dictionary sources
inside Mozc additionally include IPAdic (NAIST / ICOT) and the Okinawa
dictionary (public domain). The IPAdic / ICOT and Okinawa terms are in the
fetched `README.txt` (kept next to the dictionary). The Google
BSD-3-Clause text is Mozc's own license, not part of the fetched files.
All of them are reproduced below as required.

### Google / Mozc (BSD-3-Clause)

Copyright 2010-2018, Google Inc.  
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are
met:

* Redistributions of source code must retain the above copyright
  notice, this list of conditions and the following disclaimer.
* Redistributions in binary form must reproduce the above
  copyright notice, this list of conditions and the following disclaimer
  in the documentation and/or other materials provided with the
  distribution.
* Neither the name of Google Inc. nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
"AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

### IPAdic (NAIST) and ICOT Free Software

Applies to Mozc `src/data/dictionary*` (including the files this project
fetches or copies from that tree).

Copyright 2000, 2001, 2002, 2003 Nara Institute of Science
and Technology.  All Rights Reserved.

Use, reproduction, and distribution of this software is permitted.
Any copy of this software, whether in its original form or modified,
must include both the above copyright notice and the following
paragraphs.

Nara Institute of Science and Technology (NAIST),
the copyright holders, disclaims all warranties with regard to this
software, including all implied warranties of merchantability and
fitness, in no event shall NAIST be liable for
any special, indirect or consequential damages or any damages
whatsoever resulting from loss of use, data or profits, whether in an
action of contract, negligence or other tortuous action, arising out
of or in connection with the use or performance of this software.

A large portion of the dictionary entries
originate from ICOT Free Software.  The following conditions for ICOT
Free Software applies to the current dictionary as well.

Each User may also freely distribute the Program, whether in its
original form or modified, to any third party or parties, PROVIDED
that the provisions of Section 3 ("NO WARRANTY") will ALWAYS appear
on, or be attached to, the Program, which is distributed substantially
in the same form as set out herein and that such intended
distribution, if actually made, will neither violate or otherwise
contravene any of the laws and regulations of the countries having
jurisdiction over the User or the intended distribution itself.

NO WARRANTY

The program was produced on an experimental basis in the course of the
research and development conducted during the project and is provided
to users as so produced on an experimental basis.  Accordingly, the
program is provided without any warranty whatsoever, whether express,
implied, statutory or otherwise.  The term "warranty" used herein
includes, but is not limited to, any warranty of the quality,
performance, merchantability and fitness for a particular purpose of
the program and the nonexistence of any infringement or violation of
any right of any third party.

Each user of the program will agree and understand, and be deemed to
have agreed and understood, that there is no warranty whatsoever for
the program and, accordingly, the entire risk arising from or
otherwise connected with the program is assumed by the user.

Therefore, neither ICOT, the copyright holder, or any other
organization that participated in or was otherwise related to the
development of the program and their respective officials, directors,
officers and other employees shall be held liable for any and all
damages, including, without limitation, general, special, incidental
and consequential damages, arising out of or otherwise in connection
with the use or inability to use the program or any product, material
or result produced or otherwise obtained by using the program,
regardless of whether they have been advised of, or otherwise had
knowledge of, the possibility of such damages at any time during the
project or thereafter.  Each user will be deemed to have agreed to the
foregoing by his or her commencement of use of the program.  The term
"use" as used herein includes, but is not limited to, the use,
modification, copying and distribution of the program and the
production of secondary products from the program.

In the case where the program, whether in its original form or
modified, was distributed or delivered to or received by a user from
any person, organization or entity other than ICOT, unless it makes or
grants independently of ICOT any specific warranty to the user in
writing, such person, organization or entity, will also be exempted
from and not be held liable to the user for any such damages as noted
above as far as the program is concerned.

### Okinawa dictionary

Public Domain Dataです。使用・変更・配布に関しては一切の制限をつけません。
商品などに組み込むことも自由に行なってください。すでにいくつかの辞書には沖縄辞書が採用されています。
勝手ながら、沖縄辞書に寄贈された辞書も in the Public Domain' 扱いとさせていただきます。

## Python runtime and packages (fetched at install time)

Location when installed: `%LOCALAPPDATA%\yuragi-ime\shared\runtime\<id>\`.
`setup.ps1` builds this runtime on the user's PC from the official sources
below. Each package keeps the license files its own wheel carries, in
`Lib\site-packages\<name>-<version>.dist-info\`.

| Component | Version | License | Source |
| --- | --- | --- | --- |
| Python (Windows embeddable package) | 3.13.7 | PSF License Agreement (Python Software Foundation License Version 2), together with the licenses of the components Python bundles | https://www.python.org/ftp/python/3.13.7/ |
| pip | latest at install time (not pinned) | MIT | https://bootstrap.pypa.io/get-pip.py, which installs pip from PyPI |
| PyTorch (`torch`, CPU build) | 2.9.1 | BSD-3-Clause; the third-party components it bundles are listed with their licenses in its `LICENSE` and `NOTICE` | https://download.pytorch.org/whl/cpu |
| transformers | 4.57.1 | Apache-2.0 | PyPI |
| sentencepiece | 0.2.1 | not stated in the wheel's metadata; see https://github.com/google/sentencepiece | PyPI |

The packages above are pinned (`$TorchVersion` in `packaging/setup.ps1`,
`packaging/requirements.txt`). pip resolves their dependencies at install
time; those versions are not pinned. For reference, a Linux install of the
same pins on 2026-11-14 resolved these, with the licenses their wheels
declare:

| Package | License declared by the wheel |
| --- | --- |
| huggingface_hub, tokenizers, safetensors, hf-xet, requests | Apache-2.0 |
| regex | Apache-2.0 AND CNRI-Python |
| packaging | Apache-2.0 OR BSD-2-Clause |
| numpy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| fsspec, idna, markupsafe, networkx | BSD-3-Clause |
| jinja2, mpmath, sympy | BSD |
| certifi | MPL-2.0 |
| tqdm | MPL-2.0 AND MIT |
| filelock, urllib3, charset-normalizer, PyYAML, setuptools | MIT |
| typing_extensions | PSF-2.0 |

On Windows, tqdm additionally depends on colorama (not in the list above).

## Language model rinna/japanese-gpt2-small (fetched at install time)

Location when installed: `%LOCALAPPDATA%\yuragi-ime\shared\model\<revision>\`.

| | |
| --- | --- |
| Model | `rinna/japanese-gpt2-small` (rinna Co., Ltd.) |
| Revision | `f7fdefe2941d9629a7b2894564435e0e035df6a6` (pinned in `packaging/fetch_model.py`) |
| License | MIT (`license: mit` in the model card) |
| Source | https://huggingface.co/rinna/japanese-gpt2-small |

`packaging/fetch_model.py` downloads the whole repository at that revision,
so the model card (`README.md`, with its license statement) is kept next
to the model files.

## Product icon (hand-drawn)

Location: `assets/icon/`

The product icon is a hand-drawn hiragana 「あ」. A pencil drawing was taken
from a photo and traced into an outline (`assets/icon/src/glyph.json`); the
`.svg` / `.ico` files are made from that outline and are also compiled into
`yuragi_tip.dll` and `yuragi-broker.exe`. No font outline is used.

## Other references (not copied)

| 何 | 扱い |
| --- | --- |
| SampleIME (Windows-classic-samples) | MIT。TSF の読み物。ソースは同梱していない |
| CorvusSKK | MIT ほか。TSF の読み物。ソースは同梱していない |
| Mozc win32 tip | BSD-3-Clause。作法の参照。ソースは同梱していない |
| `qmk_userspace` NICOLA (`nicola.c`) | GPL-2.0-or-later。**コピーしていない。** 挙動は `NICOLA-SPEC.md` から再実装 |
| NICOLA 配列のかな割当 | 規格上の文字配置。GPL の C ソースではない |

`data/nicola_a.json` is a hiragana face table written from the NICOLA
assignment, not a copy of GPL C sources.
