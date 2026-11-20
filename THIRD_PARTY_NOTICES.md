# Third-Party Notices

Openmd is MIT-licensed (see `LICENSE`). It builds on the open-source work below.
Their licenses apply to their code; Openmd's license does not relicense them,
model weights, external services, or content you retrieve.

## Included in this repository

### OpenCLI Browser Bridge (Apache-2.0)

- Upstream: https://github.com/jackwener/opencli, Browser Bridge extension 1.0.27 (release v2.0.3).
- Location: `src/openmd/browser_extension/vendor/opencli-bridge/background.js`, loaded by the Openmd
  browser extension so users install a single extension.
- **Modified by Openmd**: fixed bridge profile id, bridge version reporting, background windows
  minimized, tab-group names; the idle page is Openmd's own. Every change is listed in
  `vendor/opencli-bridge/VENDOR.md` and reproduced by `scripts/vendor_opencli_bridge.py`.
- License text: `LICENSES/opencli-Apache-2.0.txt` (also next to the vendored file).

### Bilibili-Harvest WBI signing core (MIT)

`src/openmd/backends/bilibili_wbi.py` adapts the request-signing core represented by
Bilibili-Harvest's `bili_wbi.py`, including the protocol permutation table.
`Copyright (c) 2023 Lanbin07`; license text in `LICENSES/Bilibili-Harvest-MIT.txt`. The rest of the
upstream repository is not included. A backend name containing `bilibili-harvest` is a
compatibility identifier, not a claim that the upstream pipeline is integrated.

## Installed at runtime (not redistributed here)

### Document parsing libraries

Openmd's document glue is original code; no MarkItDown or anydoc source is
copied. These optional libraries are installed by pip and retain their licenses:

| Library | License | Purpose |
|---|---|---|
| mammoth (mike.zwobble.org/projects/mammoth/) | BSD-2-Clause | DOCX semantic HTML |
| python-pptx (github.com/scanny/python-pptx) | MIT | PPTX slides, charts, notes |
| openpyxl (openpyxl.readthedocs.io) | MIT | XLSX saved values and formulas |
| xlrd (github.com/python-excel/xlrd) | BSD-3-Clause | XLS saved cell values |
| olefile (github.com/decalage2/olefile) | BSD-2-Clause | MSG/DOC/PPT local OLE streams |
| pdfminer.six (github.com/pdfminer/pdfminer.six) | MIT | Native PDF text and page boundaries |

The original legacy Word text reader follows Microsoft's public MS-DOC
FibBase, FibRgLw97, FibRgFcLcb97, Clx, PlcPcd and Pcd structures
(learn.microsoft.com/openspecs/office_file_formats/ms-doc/).
It extracts text and basic table delimiters; it does not certify full layout.
The original legacy PowerPoint reader follows public MS-PPT RecordHeader,
TextCharsAtom and TextBytesAtom structures
(learn.microsoft.com/openspecs/office_file_formats/ms-ppt/). Active-slide
persistence, drawing geometry and layout are not certified; results are partial.

| Component | License | How it arrives |
|---|---|---|
| opencli (`@jackwener/opencli` 2.0.3) | Apache-2.0 | Downloaded from npm on first need into `~/.openmd/tools/opencli` |
| Node.js via `nodejs-wheel-binaries` | MIT | `pip` dependency |
| trafilatura | Apache-2.0 | `pip` dependency |
| youtube-transcript-api, beautifulsoup4, markdownify | MIT | `pip` dependencies |
| faster-whisper, RapidOCR, Pillow, PyMuPDF … | see each project | optional ASR/OCR extras or parser dependencies |
| MarkItDown | MIT | optional `[pdf]` compatibility fallback; excluded from `[document]` |
| pypandoc-binary / Pandoc | see wrapper and bundled executable licenses | optional `[pandoc]` structure fallback; no bundled copy in this repository |
| MinerU | AGPL; model weights have separate terms | user-installed external CLI only; Openmd does not bundle its code or weights |

FFmpeg, yt-dlp and other command-line tools are used only if you install them; their own licenses
apply.

## Content

Committed fixtures and mock content are synthetic. No ownership or redistribution right is claimed
over platform data or anything Openmd retrieves for you.
