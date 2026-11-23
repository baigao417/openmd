# Openmd 新用户环境实测报告（2026-11-23，0.3.1）

[English](#english) · 简体中文

这份报告只写真实运行出来的数字，Openmd 落后的项目也照写。

## 一页看懂

| 维度 | Openmd 0.3.1 | 同行最好成绩（工具名） | Openmd 排位/结论 |
|---|---|---|---|
| 网页正文完整度（13 样本 / 104 探针） | 81 分，噪音 0（沿用 0.3.0 实测） | MarkItDown 0.1.9：92 分，噪音 13（同场）；Defuddle 0.19.6：80 分，噪音 0（同场）；Firecrawl：100/112（历史参考） | 同场总分落后于 MarkItDown，但噪音 0 领先对方，与 Defuddle 基本持平；Firecrawl 仅作历史参考。 |
| PDF 原生文字速度（13 篇论文均值） | 18 秒，最慢 46 秒（沿用 0.3.0 实测） | pdfplumber：约 6.5 秒（历史参考） | 较 0.2.3（43 秒）明显提速；pdfplumber 为历史参考，非同场不直接排位。 |
| PDF 表格结构（10 篇表格论文，新口径） | 表召回 / 精确率 / 单元格 F1 / 结构准确率全 0（不输出表格，保留纯文字） | Docling 2.151.1：0.817 / 0.597 / 0.318 / 0.231（历史参考） | 落后：本版未发布 PDF 表格结构还原，同行成绩为历史产物复评参考。 |
| PDF 无表控制误造表格（3 篇无表论文） | 误造 0 | MarkItDown 0.1.9、pdfplumber 0.11.10：误造 0；Docling 2.151.1：误造 13（均为历史参考） | 零误造（本版不输出表格）；同行数据为历史产物复评参考，不直接排位。 |
| PDF 隐藏文字注入（10 类合成样本） | 泄露 0/10（沿用 0.3.0 实测） | MarkItDown：泄露 10/10；pdfplumber：泄露 10/10（均为历史参考） | Openmd 在新用户环境保持 0 泄露；同行成绩为历史参考，非同场不直接排位。 |
| 扫描件与复杂版面（OmniDocBench 12 页 + 合成集） | 中文 95.52 / 英文 92.28 / 合成集 85.20（0/12 不完整页；表与公式子分仍弱） | MinerU basic：中文 86.38 / 英文 87.67；Docling：中文 74.10 / 英文 74.17（均为历史参考） | 0.3.1 修复了 8 页误标不完整后恢复正常计分，但表/公式子分仍弱；同行成绩为历史参考，不直接排位。 |
| 借浏览器读取（Kaze） | 中文维基表格 55 行；GitHub 页面约 2.8 万字 | 无同行对比（对比 Openmd 直接抓取：31 行 / 2,279 字） | 经 Kaze 借浏览器读取较直接抓取明显更完整。 |
| 平台可用性 | X、小宇宙、小鹅通、公众号、语音/抖音、EPUB 可用；小红书命令行仍不可用（沿用 0.3.0 实测） | 无同行对比 | 多数平台新用户装完即可用，小红书命令行后台读取仍失败。 |
| 安装、升级与卸载（真实 Windows 11） | 一条命令安装约 15 分钟，升级 19 秒，卸载清理干净并保留数据（沿用 0.3.0 实测） | 无同行对比 | 全链路在真实 Windows 11 上跑通。 |
| 发布前验收（17 项真实来源） | 常规网页、视频、公众号、X、小宇宙、小鹅通、抖音、RFC、arXiv、语音 verified；PDF/EPUB 如实标 partial（沿用 0.3.0 实测） | 无同行对比 | 17 项真实来源逐一验证，异常样本按预期拒收并给出提示。 |

## 怎么测的

- **新用户环境**：从源码构建 wheel，装进一个全新的 Python 虚拟环境（依赖经清华 PyPI 镜像安装）；`HOME`、`USERPROFILE`、`OPENMD_DATA_DIR`、`OPENMD_CONFIG`、HuggingFace / ModelScope 缓存全部指向空目录；`PATH` 只保留虚拟环境和 Windows 系统目录；在源码目录之外启动。这样维护者电脑上的私有工具、手放的令牌和已下载的模型都不会参与。
- **机器与网络**：Windows 11，16 GB 内存，RTX 4060 Laptop（8 GB 显存）。网络经本地代理，国内网络条件；公开网站在测试窗口内时有取页失败，下文照实记录。
- **对照方式**：同一时段、同一环境里交替运行旧版和新版（A/B/A/B），结果不一致的样本再复测。
- **评分规则与环境标注**：网页、PDF、OCR 的样本集与计分脚本沿用 2026-11-20 冻结版本，跑之前不改；PDF 表格在 0.3.1 统一改用按内容匹配表格的新评分口径，对历史产物复评（未重跑）。下文对比表的“测量时间与环境”只用两类标注：`同场（…，YYYY-MM-DD）`（同一时段实测，括号内写明环境）与 `历史参考（YYYY-MM-DD，维护者环境/历史产物，未重跑）`。不同时间或环境的数字仅作参考，不作直接胜负比较。
- **复现**：评分脚本、样本清单和逐样本回执在维护者工作区保存；公开样本链接与计分规则可按需提供。欢迎用真实失败链接提 issue。

## 网页正文完整度

英文网页与文档（13 个网页样本、104 个探针，同场工具读取同一份 HTML）：

| 工具 | 版本 | 结果 | 测量时间与环境 |
|---|---|---|---|
| MarkItDown | 0.1.9 | 92 分，噪音命中 13 | 同场（新用户环境，2026-11-22） |
| **Openmd** | **0.3.0** | **81 分，噪音命中 0** | **同场（新用户环境，2026-11-22）** |
| Defuddle | 0.19.6 | 80 分，噪音命中 0 | 同场（新用户环境，2026-11-22） |
| Firecrawl | — | 100/112（14 样本口径；需付费 key，未重跑） | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |

同场对比下，Openmd（0.3.1 未重跑该项）总分落后于 MarkItDown，但保持 0 噪音（MarkItDown 噪音命中 13），与 Defuddle 基本持平；Firecrawl 仅作历史参考。在冻结 14 样本 / 112 探针口径（含 1 个非网页样本）中，Openmd 由 0.2.3 的 78 分升至 0.3.0 的 82 分（噪音 0），RFC 9110 由约 9 KB 片段变为完整正文（50 万字、657 个标题），arXiv 摘要页改为读取 HTML 全文但结构还原仍弱（1/8）；测试窗口内 RFC 与 arXiv 多次取页失败影响分数，且 2026-11-20 维护者电脑上的 90/112 含私有 Defuddle，不代表新用户。

## PDF

### 原生文字论文处理速度

13 份冻结 PDF，同环境 A/B 均值：

| 工具 | 版本 | 结果 | 测量时间与环境 |
|---|---|---|---|
| **Openmd** | **0.3.0** | **均值 18 秒，最慢一篇 46 秒（输出与 0.2.3 逐字节相同）** | **同场（新用户环境，2026-11-22）** |
| Openmd | 0.2.3 | 均值 43 秒，最慢一篇 179 秒 | 同场（新用户环境，2026-11-22） |
| pdfplumber | — | 约 6.5 秒 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |

同环境对比中，Openmd 0.3.0 较 0.2.3 提速显著且输出逐字节一致（0.3.1 未重跑该项）。pdfplumber 约 6.5 秒来自历史参考，非同场不直接比较。

### 表格结构与无表控制误造表格

10 篇带 LaTeX 源的表格论文 + 3 篇无表控制，统一使用按内容匹配表格的新评分口径，结果依次为`表召回 / 表精确率 / 单元格 F1 / 结构准确率 / 无表控制误造数`：

| 工具 | 版本 | 结果 | 测量时间与环境 |
|---|---|---|---|
| Docling | 2.151.1 | 0.817 / 0.597 / 0.318 / 0.231 / 误造 13 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |
| MarkItDown | 0.1.9 | 0.595 / 0.070 / 0.020 / 0.0002 / 误造 0 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |
| pdfplumber | 0.11.10 | 0 / 0 / 0 / 0 / 误造 0 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |
| **Openmd** | **0.3.1** | **0 / 0 / 0 / 0 / 误造 0（不输出表格结构，保留正确纯文字）** | **同场（新用户环境，2026-11-23）** |

Openmd 0.3.1 在 PDF 表格还原上落后：三篇试验召回率为 45% 且会误造表格，故本版不输出表格结构（全 0、无表控制误造 0），仅保留正确纯文字。同行数据为历史产物按新口径复评的参考结果（Docling 在 3 篇无表控制上误造 13 张表），且参照表尚未完整转录，所有工具都可能被低估。

### 隐藏文字注入

10 类合成样本：

| 工具 | 版本 | 结果 | 测量时间与环境 |
|---|---|---|---|
| **Openmd** | **0.3.0** | **泄露 0/10** | **同场（新用户环境，2026-11-22）** |
| MarkItDown | — | 泄露 10/10 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |
| pdfplumber | — | 泄露 10/10 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |

Openmd 在新用户环境保持 0/10 泄露（0.3.1 未重跑该项）。MarkItDown 与 pdfplumber 的 10/10 泄露为历史参考，非同场不直接比较。

## 扫描件与复杂版面

OmniDocBench 12 页 + Openmd 合成集（同一评分器）：

| 工具 | 版本 | 结果 | 测量时间与环境 |
|---|---|---|---|
| **Openmd** | **0.3.1** | **中文 95.52 / 英文 92.28 / 合成集 85.20（不完整页 0/12；表格子分 45.7 中 / 30.6 英，公式子分 26.5 中 / 23.2 英）** | **同场（新用户环境，2026-11-23）** |
| MinerU | basic | 中文 86.38 / 英文 87.67 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |
| Docling | — | 中文 74.10 / 英文 74.17 | 历史参考（2026-11-20，维护者环境/历史产物，未重跑） |

评分器把标为“不完整”的页记 0 分，0.3.0 有 8 页（正常重复内容，如表格行、页眉、公式编号）被误标，0.3.1 为 0 页，分数提升主要来自这 8 页被正常计分；扫描件内的表格（45.7 中 / 30.6 英）与公式（26.5 中 / 23.2 英）子分仍然偏弱。版面检测单页耗时在 RTX 4060 Laptop 上为 14.5 秒、CPU 上为 44.2 秒；MinerU basic 与 Docling 均为历史参考，不直接排位。

## 借浏览器读取（Kaze）

无同行对比，同场对比 Openmd 直接抓取与经 Kaze 读取：

| 工具 | 版本 | 结果 | 测量时间与环境 |
|---|---|---|---|
| **Openmd（直接抓取）** | **0.3.1** | **中文维基某页表格 31 行；某 GitHub 页面 2,279 字** | **同场（维护者电脑，真实 Kaze，2026-11-23）** |
| **Openmd（经 Kaze）** | **0.3.1** | **中文维基某页表格 55 行；某 GitHub 页面约 2.8 万字** | **同场（维护者电脑，真实 Kaze，2026-11-23）** |

同场实测中，经 Kaze 借浏览器读取比 Openmd 直接抓取显著更完整：中文维基表格由 31 行增至 55 行，GitHub 页面由 2,279 字增至约 2.8 万字。

## 平台可用性

无同行对比，沿用 2026-11-22 新用户环境实测结果（0.3.1 未重跑）：

| 平台 | 0.2.3（新用户） | 0.3.0（新用户） | 说明 |
|---|---|---|---|
| X 帖子 | 首次 128 秒失败，第二次 17 秒成功；标题是“作者：正文前几十字” | 长推文 10 秒、短推文 7 秒，标题取自帖子本身 | 不登录：X 官方内嵌接口 → FxEmbed 公开接口 → 浏览器扩展。长推文全文依赖 FxEmbed 托管接口；它不可用时退到浏览器，约 70 秒 |
| 小宇宙 | 失败（需要 App 令牌） | 扫码登录一次后取得官方逐字稿（2 秒），令牌自动续期 | 账号有被风控的风险，建议用小号 |
| 小鹅通（已登录浏览器） | 未登录时把登录页存成正文 | 课程正文 verified；全新环境首次 127 秒，已准备好的环境 7 秒 | 首次慢在浏览器读取组件的准备 |
| 小红书 | 命令行读取最长卡 15 分钟 | 缺分享参数的链接 0.6 秒给出下一步；Agent 浏览器快照路线可用 | 命令行后台读取仍读不出 |
| 公众号 | 经浏览器扩展约 95 秒 | 本地读取，9 个不同公众号的 9 篇文章全部成功，每篇 9–12 秒 | — |
| 语音转写 / 抖音 | 装完模型后仍转写失败（PyAV 19 与 faster-whisper 不兼容；回退写死未安装的模型） | 中文、英文合成语音转写正确；抖音视频 176 秒出逐字稿 | 首次使用询问下载 1.6 GB 模型；HuggingFace 慢或不通时按实测速度换 ModelScope，SHA256 校验 |
| EPUB | — | 两本公开 EPUB 3 样书 1–2 秒转完；损坏文件 0.6 秒拒收并提示重新下载 | 书内插图未做 OCR 时如实标 partial |

## 安装、升级与卸载

无同行对比，沿用 2026-11-22 在一台真实 Windows 11 上的实测结果：用 PowerShell 5.1 按 README 一条命令（`irm … | iex`）安装，不设环境变量代理，程序与数据目录放在测试目录里，结束后还原注册表。

| 步骤 | 结果 |
|---|---|
| 一条命令安装（uv 下载并校验 SHA256，uv 自带 Python 3.12，装 `[all]` 约 2.4 GB） | 成功，约 15 分钟（主要是下载依赖） |
| 新开终端输入 `openmd` | 找到命令，`openmd --version` 正常 |
| 真实网页 | 19 秒取得全文 |
| `openmd update` | 19 秒升级，已选组件保留 |
| `openmd uninstall` | 程序、命令入口、用户 PATH 项、浏览器原生宿主注册全部删除；数据保留 |

## 发布前验收

无同行对比，沿用 2026-11-22 16:40–16:54 实测：从 main 构建 wheel，经清华镜像装进全新虚拟环境的 `[all]`（177 秒），按上文新用户环境逐个运行真实链接并读回产物。

| 来源 | 结果 | 耗时 |
|---|---|---:|
| 普通网页 | verified | 32 s（含首次准备 Defuddle） |
| YouTube（有字幕） | verified | 58 s |
| B 站（自动字幕） | verified | 9 s |
| 公众号 | verified | 10 s |
| X 长推文 + 图片 | verified | 10 s |
| X 短推文 | verified | 7 s |
| 小宇宙（扫码登录后） | verified（官方逐字稿） | 2 s |
| 小鹅通课程（浏览器已登录） | verified | 128 s |
| 小红书（缺分享参数的链接） | 立即失败并给出下一步 | 0.6 s |
| 抖音（已装语音模型） | verified（本地转写） | 176 s |
| 本地论文 PDF | partial（少数页文字层无法核验，如实标注） | 11 s |
| 最慢的论文 PDF（2303.08774） | partial（同上） | 53 s |
| RFC 9110 / RFC 9112 | verified（全文） | 11 s / 7 s |
| arXiv 摘要页 | verified（HTML 全文） | 11 s |
| EPUB | partial（插图未做 OCR） | 2 s |
| 损坏的 EPUB | 拒收并提示重新下载 | 0.6 s |
| 中文语音文件 | verified | 28 s |

---

<a id="english"></a>
# Openmd new-user benchmark (2026-11-23, 0.3.1)

Only numbers from real runs are reported, including where Openmd falls behind.

## At a glance

| Dimension | Openmd 0.3.1 | Best peer result (tool) | Openmd standing / conclusion |
|---|---|---|---|
| Web content completeness (13 samples / 104 probes) | 81, 0 noise (carried over from 0.3.0 run) | MarkItDown 0.1.9: 92, 13 noise (same run); Defuddle 0.19.6: 80, 0 noise (same run); Firecrawl: 100/112 (historical ref) | Behind MarkItDown on total score in the same run, ahead on 0 noise, and roughly tied with Defuddle; Firecrawl is historical reference only. |
| PDF native-text speed (13 papers mean) | 18 s, slowest 46 s (carried over from 0.3.0 run) | pdfplumber: ~6.5 s (historical ref) | Much faster than 0.2.3 (43 s); pdfplumber is historical reference only and not directly ranked. |
| PDF table structure (10 table papers, new rubric) | 0 across table recall / precision / cell F1 / structure accuracy (no tables emitted; plain text kept) | Docling 2.151.1: 0.817 / 0.597 / 0.318 / 0.231 (historical ref) | Behind: native PDF table reconstruction is not shipped in 0.3.1; peer numbers are historical-output re-scores for reference. |
| PDF false tables on table-free controls (3 papers) | 0 false tables | MarkItDown 0.1.9, pdfplumber 0.11.10: 0; Docling 2.151.1: 13 (all historical ref) | Zero false tables (no table output in this release); peer numbers are historical-output re-scores for reference. |
| PDF hidden-text injection (10 synthetic categories) | 0/10 leaks (carried over from 0.3.0 run) | MarkItDown: 10/10 leaks; pdfplumber: 10/10 leaks (both historical ref) | Openmd leaks 0/10 in the new-user environment; peer numbers are historical reference only and not directly ranked. |
| Scanned documents & complex layouts (OmniDocBench 12 pages + synthetic) | ZH 95.52 / EN 92.28 / synthetic 85.20 (0/12 incomplete; table & formula sub-scores still weak) | MinerU basic: ZH 86.38 / EN 87.67; Docling: ZH 74.10 / EN 74.17 (both historical ref) | 0.3.1 stops mislabelling 8 pages as incomplete so they are now scored, while table/formula sub-scores remain weak; peer numbers are historical reference only. |
| Browser-assisted reading (Kaze) | Chinese Wikipedia table: 55 rows; GitHub page: ~28,000 chars | No peer comparison (vs. Openmd direct fetch: 31 rows / 2,279 chars) | Reading via Kaze recovers substantially more content than direct fetch. |
| Platform availability | X, Xiaoyuzhou, Xiaoe-tech, WeChat, speech/Douyin, EPUB work; Xiaohongshu CLI still fails (carried over from 0.3.0) | No peer comparison | Most platforms work out of the box for a new user; background CLI reading for Xiaohongshu still fails. |
| Install, update & uninstall (real Windows 11) | One-liner install ~15 min, update 19 s, uninstall cleans up and keeps data (carried over from 0.3.0) | No peer comparison | End-to-end lifecycle verified on a real Windows 11 machine. |
| Pre-release acceptance (17 real sources) | Standard web, video, WeChat, X, Xiaoyuzhou, Xiaoe-tech, Douyin, RFC, arXiv, speech verified; PDF/EPUB marked partial (carried over from 0.3.0) | No peer comparison | All 17 real sources verified; invalid/incomplete inputs fail or warn as expected. |

## Method

- **New-user environment**: A wheel built from source is installed into a fresh Python virtual environment (dependencies via the Tsinghua PyPI mirror). `HOME`, `USERPROFILE`, `OPENMD_DATA_DIR`, `OPENMD_CONFIG`, and the HuggingFace / ModelScope caches point to empty folders, `PATH` holds only the venv and Windows system folders, and Openmd starts outside the source tree so no maintainer-only tools, hand-placed tokens, or pre-downloaded models take part.
- **Machine and network**: Windows 11, 16 GB RAM, RTX 4060 Laptop (8 GB VRAM), local proxy, China network. Public sites failed intermittently during the test window and this is recorded as such.
- **Controls**: Old and new builds alternate in the same time window and environment (A/B/A/B); samples with inconsistent results are re-tested.
- **Frozen rules and environment labels**: Sample sets and scorers for web, PDF, and OCR remain the versions frozen on 2026-11-20, unchanged before runs; PDF tables in 0.3.1 switched to a content-matched table rubric re-scoring historical outputs without re-running. Every comparison table below uses one of two kinds of label in the `Measured` column: `Same run (…, YYYY-MM-DD)` (measured in the same window, environment named in brackets) or `Historical reference (YYYY-MM-DD, maintainer env / historical output, not re-run)`. Numbers from different dates or environments are reference only and are not used to claim one tool beat another.
- **Reproducibility**: Scorers, sample lists, and per-sample receipts are kept in the maintainer workspace; public sample links and scoring rules are available on request. Real failing links are welcome as issues.

## Web content completeness

English web pages and documentation (13 web samples, 104 probes; same-run tools read the identical HTML):

| Tool | Version | Result | Measured |
|---|---|---|---|
| MarkItDown | 0.1.9 | 92, 13 noise hits | Same run (new-user env, 2026-11-22) |
| **Openmd** | **0.3.0** | **81, 0 noise hits** | **Same run (new-user env, 2026-11-22)** |
| Defuddle | 0.19.6 | 80, 0 noise hits | Same run (new-user env, 2026-11-22) |
| Firecrawl | — | 100/112 (14-sample set; paid key, not re-run) | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |

In the same-run comparison (not re-run in 0.3.1), Openmd trails MarkItDown on total score but maintains 0 noise (vs. MarkItDown's 13 noise hits) and is roughly tied with Defuddle; Firecrawl is historical reference only. On the frozen 14-sample / 112-probe set (including 1 non-web sample), Openmd rose from 78 (0.2.3) to 82 (0.3.0) with 0 noise, RFC 9110 changed from a ~9 KB fragment to full text (~500k characters, 657 headings), and arXiv abstract pages now read HTML full text though structure recovery remains weak (1/8); repeated RFC and arXiv fetch failures added network noise, and the 2026-11-20 maintainer-machine 90/112 used a private Defuddle and is not a new-user figure.

## PDF

### Native-text paper speed

13 frozen PDFs, same-environment A/B mean:

| Tool | Version | Result | Measured |
|---|---|---|---|
| **Openmd** | **0.3.0** | **Mean 18 s, slowest 46 s (byte-identical output to 0.2.3)** | **Same run (new-user env, 2026-11-22)** |
| Openmd | 0.2.3 | Mean 43 s, slowest 179 s | Same run (new-user env, 2026-11-22) |
| pdfplumber | — | ~6.5 s | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |

In same-environment A/B testing, Openmd 0.3.0 is substantially faster than 0.2.3 with byte-identical output (not re-run in 0.3.1). pdfplumber's ~6.5 s is historical reference only and not directly compared.

### Table structure and false tables on table-free PDFs

10 papers with LaTeX table sources + 3 table-free control PDFs, scored under the new content-matched table rubric (`table recall / table precision / cell F1 / structure accuracy / false tables on table-free controls`):

| Tool | Version | Result | Measured |
|---|---|---|---|
| Docling | 2.151.1 | 0.817 / 0.597 / 0.318 / 0.231 / 13 false tables | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |
| MarkItDown | 0.1.9 | 0.595 / 0.070 / 0.020 / 0.0002 / 0 false tables | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |
| pdfplumber | 0.11.10 | 0 / 0 / 0 / 0 / 0 false tables | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |
| **Openmd** | **0.3.1** | **0 / 0 / 0 / 0 / 0 false tables (no table structure emitted; correct plain text kept)** | **Same run (new-user env, 2026-11-23)** |

Openmd 0.3.1 falls behind on PDF table reconstruction: a three-paper trial reached 45% recall while generating false tables, so table output is not shipped in this release (all zeros, 0 false tables, correct plain text preserved). Peer numbers come from re-scoring historical outputs without re-running (Docling generated 13 false tables on the 3 table-free controls), and because reference tables are not yet completely transcribed, every tool may be understated.

### Hidden-text injection

10 synthetic categories:

| Tool | Version | Result | Measured |
|---|---|---|---|
| **Openmd** | **0.3.0** | **0/10 leaks** | **Same run (new-user env, 2026-11-22)** |
| MarkItDown | — | 10/10 leaks | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |
| pdfplumber | — | 10/10 leaks | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |

Openmd leaks 0/10 in the new-user environment (not re-run in 0.3.1). MarkItDown and pdfplumber's 10/10 leak figures are historical reference only and not directly compared.

## Scanned documents and complex layouts

OmniDocBench 12 pages + Openmd synthetic set (same scorer):

| Tool | Version | Result | Measured |
|---|---|---|---|
| **Openmd** | **0.3.1** | **ZH 95.52 / EN 92.28 / synthetic 85.20 (0/12 incomplete pages; table sub-score 45.7 ZH / 30.6 EN, formula sub-score 26.5 ZH / 23.2 EN)** | **Same run (new-user env, 2026-11-23)** |
| MinerU | basic | ZH 86.38 / EN 87.67 | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |
| Docling | — | ZH 74.10 / EN 74.17 | Historical reference (2026-11-20, maintainer env / historical output, not re-run) |

The scorer counts any page marked "incomplete" as 0: 0.3.0 mislabelled 8 pages (due to normal repetition such as table rows, headers, and equation numbers) as incomplete while 0.3.1 has 0, so most of the gain comes from those 8 pages now being scored; table (45.7 ZH / 30.6 EN) and formula (26.5 ZH / 23.2 EN) sub-scores inside scanned pages remain weak. Per-page layout detection takes 14.5 s on an RTX 4060 Laptop and 44.2 s on CPU; MinerU basic and Docling are historical reference only and not directly ranked.

## Browser-assisted reading (Kaze)

No peer comparison; same-run comparison between Openmd direct fetch and reading via Kaze:

| Tool | Version | Result | Measured |
|---|---|---|---|
| **Openmd (direct fetch)** | **0.3.1** | **Chinese Wikipedia table: 31 rows; GitHub page: 2,279 chars** | **Same run (maintainer machine, real Kaze, 2026-11-23)** |
| **Openmd (via Kaze)** | **0.3.1** | **Chinese Wikipedia table: 55 rows; GitHub page: ~28,000 chars** | **Same run (maintainer machine, real Kaze, 2026-11-23)** |

In the same-run test, browser-assisted reading via Kaze recovers substantially more content than Openmd direct fetch (31 → 55 table rows on the Chinese Wikipedia page; 2,279 → ~28,000 characters on the GitHub page).

## Platform availability

No peer comparison; carried over from the 2026-11-22 new-user environment run (not re-run in 0.3.1):

| Platform | 0.2.3 (new user) | 0.3.0 (new user) | Notes |
|---|---|---|---|
| X posts | First attempt failed in 128 s, second succeeded in 17 s; title was "author: first few dozen chars" | Long post 10 s, short post 7 s; title taken from the post itself | Without login: official X embed API → public FxEmbed API → browser extension. Long-post full text relies on hosted FxEmbed; browser fallback takes ~70 s when unavailable |
| Xiaoyuzhou | Failed (required app token) | Official transcript in 2 s after one QR login; tokens renew automatically | Account risk-control applies; use a throwaway account |
| Xiaoe-tech (logged-in browser) | Saved login page as body text when not logged in | Course text verified; 127 s on first run in a fresh env, 7 s once prepared | First-run latency is from setting up the browser reading component |
| Xiaohongshu | CLI read hung up to 15 min | Link without share parameters returns next step in 0.6 s; agent browser-snapshot route works | Background CLI read still fails |
| WeChat | ~95 s via browser extension | Read locally; 9 articles from 9 different accounts all verified in 9–12 s each | — |
| Speech transcription / Douyin | Still failed after installing model (PyAV 19 incompatible with faster-whisper; fallback hardcoded an uninstalled model) | Chinese and English synthetic speech transcribed accurately; Douyin video transcript in 176 s | Prompts to download 1.6 GB model on first use; switches from HuggingFace to ModelScope by measured speed when slow or unreachable, SHA256-checked |
| EPUB | — | Two public EPUB 3 sample books converted in 1–2 s; damaged file refused in 0.6 s with a re-download hint | Marked partial when embedded illustrations do not undergo OCR |

## Install, update and uninstall

No peer comparison; carried over from the 2026-11-22 test on a real Windows 11 machine using PowerShell 5.1 with the README one-liner (`irm … | iex`), no proxy environment variables, program and data directories pointed to a test folder, and the registry restored afterwards.

| Step | Result |
|---|---|
| One-liner install (uv downloaded and SHA256-checked, uv-managed Python 3.12, `[all]` ~2.4 GB) | Succeeded in ~15 minutes (mostly dependency downloads) |
| Run `openmd` in a new terminal | Command found; `openmd --version` works |
| Real web page | Full text fetched in 19 s |
| `openmd update` | Upgraded in 19 s with selected extras kept |
| `openmd uninstall` | Program, launcher, user PATH entry, and browser native-host registration all removed; data kept |

## Pre-release acceptance

No peer comparison; carried over from 2026-11-22 16:40–16:54: a wheel built from main was installed with `[all]` into a fresh venv via the Tsinghua mirror (177 s) and run against real links in the new-user environment above, reading back each output.

| Source | Result | Duration |
|---|---|---:|
| Standard web page | verified | 32 s (incl. first-use Defuddle setup) |
| YouTube (with captions) | verified | 58 s |
| Bilibili (auto-captions) | verified | 9 s |
| WeChat article | verified | 10 s |
| X long post + images | verified | 10 s |
| X short post | verified | 7 s |
| Xiaoyuzhou (after QR login) | verified (official transcript) | 2 s |
| Xiaoe-tech course (logged-in browser) | verified | 128 s |
| Xiaohongshu (link missing share params) | failed immediately with next step | 0.6 s |
| Douyin (speech model installed) | verified (local transcription) | 176 s |
| Local paper PDF | partial (a few pages have unverifiable text layers, labelled as such) | 11 s |
| Slowest paper PDF (2303.08774) | partial (same as above) | 53 s |
| RFC 9110 / RFC 9112 | verified (full text) | 11 s / 7 s |
| arXiv abstract page | verified (HTML full text) | 11 s |
| EPUB | partial (illustrations without OCR) | 2 s |
| Damaged EPUB | refused with re-download hint | 0.6 s |
| Chinese speech file | verified | 28 s |
