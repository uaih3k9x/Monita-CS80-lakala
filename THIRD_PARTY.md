# 第三方组件

本仓库主体以 [PolyForm Noncommercial 1.0.0](LICENSE) 发布；下列文件例外，按各自原授权。

## EDK2 UEFI 8×19 窄字体

- 用途：固件开机版权页的字体（fw v7）。
- 来源：[tianocore/edk2](https://github.com/tianocore/edk2)
  `MdeModulePkg/Universal/Console/GraphicsConsoleDxe/LaffStd.c`（`gUsStdNarrowGlyphData`）。
- 本仓库中的文件：
  - `tools/edk2font/LaffStd.c`：原文件，未修改。
  - `fw/lcd/font_uefi.h`：由 `tools/gen_font.py` 从上面的文件生成，只取 0x20–0x7E，保留原版权与 SPDX 声明。
  - 编进固件 `app.bin` 的字形表即来自 `font_uefi.h`。
- 原文件版权：`Copyright (c) 2006 - 2008, Intel Corporation. All rights reserved.`
- 授权：BSD-2-Clause-Patent（SPDX: `BSD-2-Clause-Patent`），
  见 <https://opensource.org/license/bsdpluspatent> 和 EDK2 的 [License.txt](https://github.com/tianocore/edk2/blob/master/License.txt)。全文如下：

```
Copyright (c) 2019, TianoCore and contributors.  All rights reserved.

SPDX-License-Identifier: BSD-2-Clause-Patent

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice,
   this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

Subject to the terms and conditions of this license, each copyright holder
and contributor hereby grants to those receiving rights under this license
a perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable
(except for failure to satisfy the conditions of this license) patent
license to make, have made, use, offer to sell, sell, import, and otherwise
transfer this software, where such license applies only to those patent
claims, already acquired or hereafter acquired, licensable by such copyright
holder or contributor that are necessarily infringed by:

(a) their Contribution(s) (the licensed copyrights of copyright holders and
    non-copyrightable additions of contributors, in source or binary form)
    alone; or

(b) combination of their Contribution(s) with the work of authorship to
    which such Contribution(s) was added by such copyright holder or
    contributor, if, at the time the Contribution is added, such addition
    causes such combination to be necessarily infringed. The patent license
    shall not apply to any other combinations which include the
    Contribution.

Except as expressly stated above, no rights or licenses from any copyright
holder or contributor is granted under this license, whether expressly, by
implication, estoppel or otherwise.

DISCLAIMER

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```
