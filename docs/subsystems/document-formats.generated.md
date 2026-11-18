# 本地文档格式能力

由 `routing/declarations.json` 生成；`python -m openmd.engines.formats` 更新。

分档描述实现与依赖合同，不保证任意文件都能完整提取。真实样本、工具版本和未验证项应保存在运行回执中。

| 后缀 | 有序引擎 | 分档 | 范围与限制 |
|---|---|---|---|
| pdf、docx、pptx、xlsx | markitdown | 可用 | Native text; legacy PDF page/OCR route retained. |
| html、htm | defuddle → markitdown | 依赖就绪才可用 | Local HTML only; QQ exports retain priority; scripts never execute. |
| epub | pandoc → markitdown | 依赖就绪才可用 | EPUB spine order; encrypted ebooks rejected. |
| rtf、odt | pandoc → soffice | 依赖就绪才可用 | Pandoc first; LibreOffice fallback. |
| odp、ods | pandoc → soffice | 依赖就绪才可用 | No Pandoc reader for ODP/ODS; use LibreOffice PPTX/XLSX conversion. |
| doc、xls、ppt | soffice | 依赖就绪才可用 | Requires LibreOffice; Writer to DOCX, Calc to XLSX, Impress to PPTX. |
| csv、tsv、json、xml、ipynb、msg | markitdown | 依赖就绪才可用 | CSV/TSV row coverage; XML/JSON remain original text. |
| mobi、azw3、fb2 | calibre | 实验 | Requires ebook-convert; DRM rejected; no decryption. |
| zip | openmd.zip | 可用 | Depth 2; 500 MiB expanded; ratio 100; 1000 entries; 100 MiB/file; total timeout 120 s; media gated for Openmd ASR. |

ZIP 是 Openmd 容器，不是 MarkItDown ZipConverter。ZIP 中媒体默认逐条返回 `asr_required`，图片返回 `ocr_required`；不会隐式上传或加载模型。

附加引擎位于旧 PDF/Office/OCR provider 后；已有 PDF、DOCX、PPTX、XLSX 的正文和优先级保持原合同。
