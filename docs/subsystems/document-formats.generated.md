# 本地文档格式能力

由 `routing/declarations.json` 生成；`python -m openmd.engines.formats` 更新。

分档描述实现与依赖合同，不保证任意文件都能完整提取。真实样本、工具版本和未验证项应保存在运行回执中。

| 后缀 | 有序引擎 | 分档 | 范围与限制 |
|---|---|---|---|
| pdf | markitdown → openmd.pdf | 可用 | Native PDF text via optional baseline then pdfminer; existing PDF/OCR provider order retained; scanned images require OCR. |
| docx | openmd.docx → markitdown | 可用 | Mammoth semantic text and GFM tables; original embedded images retained; missing OCR or parser warnings are partial. |
| pptx | openmd.pptx → markitdown | 可用 | Slide order, GFM tables, category charts and speaker notes; embedded images retained with optional local OCR. |
| xlsx | openmd.xlsx → markitdown | 可用 | Worksheet order and GFM tables; saved formula values without execution; missing caches or image OCR are partial. |
| xls | openmd.xls → soffice → markitdown | 可用 | xlrd cached cell values and worksheet order; bounded GFM tables; LibreOffice enhancement optional. |
| html | openmd.html → defuddle → markitdown | 可用 | Local HTML only; QQ exports retain priority; scripts/resources disabled. |
| htm | openmd.htm → defuddle → markitdown | 可用 | Local HTML only; QQ exports retain priority; scripts/resources disabled. |
| epub | openmd.epub → pandoc → markitdown | 可用 | EPUB spine order; cover and link-only navigation omitted; encrypted ebooks rejected. |
| rtf | openmd.rtf → pandoc → markitdown | 可用 | Local RTF text, Unicode and basic tables; embedded objects reported as gaps. |
| odt | openmd.odt → pandoc → soffice → markitdown | 可用 | ODF paragraphs, lists and tables; original image bytes retained; optional Pandoc/LibreOffice enhancement. |
| ods | openmd.ods → soffice → markitdown | 可用 | ODF worksheets, repeated cells, saved formula values and GFM tables; image gaps reported. |
| odp | openmd.odp → soffice → markitdown | 可用 | ODF slide order, text, tables and notes; image gaps reported. |
| fb2 | openmd.fb2 → markitdown | 可用 | FictionBook body order and tables; metadata, covers and link-only contents omitted. |
| csv | openmd.csv → markitdown | 可用 | UTF-8 CSV; bounded GFM tables with row coverage. |
| tsv | openmd.tsv → markitdown | 可用 | UTF-8 TSV; bounded GFM tables with row coverage. |
| json | openmd.json → markitdown | 可用 | Validated JSON with exact source lexemes and key order preserved; no execution. |
| xml | openmd.xml → markitdown | 可用 | Validated UTF-8 XML source preserved; DTD/entities rejected. |
| ipynb | openmd.ipynb → markitdown | 可用 | Notebook cells and saved outputs in order; no execution; image OCR optional. |
| msg | openmd.msg → markitdown | 可用 | Local MSG subject, sender, recipient and body; attachments reported as gaps; encrypted messages rejected. |
| doc | soffice → openmd.doc | 实验 | LibreOffice preferred; native main-story text/basic tables fallback always partial; 95% recall not certified. |
| ppt | soffice → openmd.ppt | 实验 | LibreOffice preferred; native record text fallback always partial; active slide order and 95% recall not certified. |
| zip | openmd.zip | 可用 | Depth 2; 500 MiB expanded; ratio 100; 1000 entries; 100 MiB/file; total timeout 120 s; media gated for Openmd ASR. |
| mobi | calibre | 依赖就绪才可用 | Calibre required; DRM rejected; cover/link-only navigation and intermediate filename heading omitted. |
| azw3 | calibre | 依赖就绪才可用 | Calibre required; DRM rejected; cover/link-only navigation and intermediate filename heading omitted. |

ZIP 是 Openmd 容器，不是 MarkItDown ZipConverter。ZIP 中媒体默认逐条返回 `asr_required`，图片返回 `ocr_required`；不会隐式上传或加载模型。

可用档表示安装 `[document]` 后可运行本地原生文本解析；图片 OCR 另需 `[ocr]` 或本机 OCR 配置。DOC/PPT 自写回退为实验档。PDF 保留既有 page/OCR 顺序，DOCX/PPTX/XLSX 使用自有引擎。
