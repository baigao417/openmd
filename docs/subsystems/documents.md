# 本地文档与容器

## 入口与合同

`engines/base.py` 的 `DocumentEngine` 提供 `id`、`can_handle(path, sniff)`、
`available()` 与 `convert(path, limits)`。可用性返回 `ok/reason/version`。
转换返回既有 `TranscriptResult`：`text` 是 Markdown，`components` 描述独立条目，
`source_level` 表示来源，`metadata` 保存证据，`error_code` 表示失败。

`engines/registry.py` 按后缀给出有序引擎链；先检查魔数和容器结构，再检测依赖。
结果包含 `selected_engine`、`engine_selection_reason`、`engine_attempts`。
同样的信息写入 source notes，供既有 package warnings 回执消费。
格式与分档见[生成清单](document-formats.generated.md)。重新生成：

```powershell
python -m openmd.routing.generate
python -m openmd.engines.formats
```

PDF 的 `pdf.markitdown`、`pdf.textlayer`、`ocr.chain` 顺序保留；缺少 MarkItDown 时
兼容入口使用 pdfminer.six 读取原生文字。DOCX/PPTX/XLSX 及新增格式进入
`document.engines`，使用 Openmd 自有胶水。底层分别使用 mammoth、python-pptx、
openpyxl、xlrd、olefile；其余文本/ODF/电子书读取器由 Openmd 实现。
remote HTML/JSON URL 继续走网站路线，QQ 导出识别优先。

执行器会在尝试 provider 之前按本地文档后缀过滤链：新增格式只进入
`document.engines`，不生成旧 PDF/Office/OCR 的失败噪音；禁用 fallback 时同样
先过滤再选首项。PDF 保留原来的引擎顺序。
plan 按格式检查实际引擎链，DOC/PPT 优先 LibreOffice，缺失时自写回退始终标记
partial（不认证 95% 召回与版式）。XLS/ODF/FB2 可直接解析；MOBI/AZW3 检查 Calibre；
缺少时 `execution_ready=false`，`blocked_reason=engine_unavailable:<engine>`，
并给出 `next_action`。该检查不读取输入正文，不执行转换。
文档产物的 front matter 与 Source 小节均显示原始 `format`；车道 ID 和路径仍为 `pdf`。

## 可选工具

- MarkItDown：已安装时保留基线回退，只允许文档后缀，显式禁用插件；`[document]`
  使用自有引擎，不再依赖 MarkItDown、magika 或 onnxruntime。
  CSV/TSV 默认最多 10000 个数据行；`coverage` 保存总行数、包含行数、截断原因。
  JSON/XML 保留源文本，Notebook 只读取内容，不执行单元格。
- Defuddle：复用工具卡的 `tools.defuddle.root/node`。仅调用库解析本地 HTML；
  删除脚本和活动资源节点，禁用 fetch/http/https，不启动浏览器或加载页面资源。
- Pandoc：PATH、`OPENMD_PANDOC` 或 `tools.pandoc.executable`；Windows 额外探测当前
  用户的 LocalAppData/Pandoc；`pip install "openmd-cli[pandoc]"` 可安装本地二进制。
  使用 sandbox，无过滤器与 Lua；输出采用
  `gfm-raw_html`，清除 HTML 包装并移除与文件名重复的起始书名标题。
- LibreOffice：沿用 `soffice` / `OPENMD_SOFFICE` 和 PATH。每次使用独立 profile，
  禁用宏与外部更新；Writer 转 DOCX，Calc 转 XLSX，Impress 转 PPTX。
  Windows 检测到同目录 `soffice.com` 时使用控制台入口；版本探测和转换均使用
  临时 profile、`--headless`、`--nologo`，避免 GUI launcher 与启动画面。
  Calc/Impress 不支持有意义的 DOCX 导出，保留其对应 OOXML 格式。
- Calibre：PATH 的 `ebook-convert`、`OPENMD_CALIBRE` 或 `tools.calibre.executable`。
  未安装返回 `engine_unavailable` 和安装提示。检测到 DRM 返回
  `ebook_drm_protected`；不解密、不尝试去除 DRM。
- MinerU：用户自行安装的本地 CLI；配置 `OPENMD_MINERU` 或
  `tools.mineru.executable`。PDF 可显式使用 `--ocr-engine mineru`，默认 `auto`
  不改变既有路线；`tools.mineru.default` 可设为 `mineru`，`tools.mineru.tier`
  可设 `basic` 或 `standard`。Openmd 不下载
  权重或分发 MinerU；用户须遵守 AGPL 及独立模型权重许可。

上述工具都不会自动安装。新文档解析子进程使用已有 owned-process 合同，Windows
不显示控制台，超时或中断会终止本次创建的进程树。错误不回显工具原始 stderr。

DOCX/PPTX/XLSX 的原始嵌图以 data URI 保留字节和哈希。只有已允许本机转录时
才调用本机 OCR；不可用时逐图片标记缺口。PPTX 保留备注、类别图表与表格；
XLSX 按工作表顺序显示保存的计算值并注明含公式，缺少公式缓存时 partial。
单页、工作表、幻灯片失败保留其他可读部分。`openmd doctor` 按格式列出当前可用、
缺少的引擎和对应安装命令；只检测依赖，不打开用户文档、不执行转换。

## ZIP

`engines/archive.py` 是 Openmd 自己的容器适配器，禁止使用 MarkItDown ZipConverter。
默认上限：嵌套深度 2、累计展开 500 MiB、压缩比 100、累计 1000 条目录项、
单文件 100 MiB、单次总超时 120 秒；此外限制中央目录大小和聚合输出大小。

先校验整个目录，再流式写到 owned 临时目录。拒收路径穿越、绝对路径、Windows
盘符/ADS/保留名、大小写重复路径、链接、特殊文件和加密 ZIP。嵌套容器共享预算和
截止时间。临时文件在正常退出、失败或中断时按既有临时目录合同回收。

每个条目重新调用 detector。文档进入引擎链，QQ 导出进入既有导出 parser；音视频
逐条返回 `asr_required`，图片返回 `ocr_required`，未知格式返回
`archive_entry_unsupported`。ZIP 不隐式加载 ASR/OCR 模型或上传内容。
汇总 Markdown 列出逐条状态，package components 保存逐条失败码；存在任何缺口即
`partial`。API 元数据另含 `archive_entries`、嵌套回执与 coverage。

## 验证

```powershell
python -m pytest tests/engines/test_document_engines.py
python -m pytest tests/engines/test_signatures.py
python -m pytest tests/adapters/pdf/test_pdf_adapter.py
python -m pytest tests/adapters/pdf/test_office_adapter.py
python -m openmd.routing.generate --check
python -m openmd.engines.formats --check
```

最后按 CONTRIBUTING 运行完整 pytest。单元测试只证明边界与回归；能否提取某种真实
文件仍须真实 CLI、落盘产物与读回回执，不把依赖检测当成提取成功。
