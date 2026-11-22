# Openmd 新用户环境实测报告（2026-11-22，0.3.0）

[English](#english) · 简体中文

这份报告只写真实运行出来的数字，Openmd 落后的项目也照写。

## 怎么测的

- **新用户环境**：从源码构建 wheel，装进一个全新的 Python 虚拟环境（依赖经清华 PyPI 镜像安装）；`HOME`、`USERPROFILE`、`OPENMD_DATA_DIR`、`OPENMD_CONFIG`、HuggingFace / ModelScope 缓存全部指向空目录；`PATH` 只保留虚拟环境和 Windows 系统目录；在源码目录之外启动。这样维护者电脑上的私有工具、手放的令牌和已下载的模型都不会参与。
- **机器**：Windows 11，16 GB 内存，RTX 4060 Laptop（8 GB 显存）。网络经本地代理，国内网络条件；公开网站在测试窗口内时有取页失败，下文照实记录。
- **对照**：同一时段、同一环境里交替运行旧版和新版（A/B/A/B），结果不一致的样本再复测。
- **评分规则先冻结**：网页、PDF、OCR 的样本集与计分脚本沿用 2026-11-20 冻结的版本，跑之前不改。
- **竞品分数**：网页一项在同一新用户环境、同一时段重跑了 Defuddle 与 MarkItDown；其余竞品分数来自 2026-11-20 同一批样本、同一计分规则，表中注明日期。

## 平台：新用户装完能不能用

| 平台 | 0.2.3（新用户） | 0.3.0（新用户） | 说明 |
|---|---|---|---|
| X 帖子 | 首次 128 秒失败，第二次 17 秒成功；标题是“作者：正文前几十字” | 长推文 10 秒、短推文 7 秒，标题取自帖子本身 | 不登录：X 官方内嵌接口 → FxEmbed 公开接口 → 浏览器扩展。长推文全文依赖 FxEmbed 托管接口；它不可用时退到浏览器，约 70 秒 |
| 小宇宙 | 失败（需要 App 令牌） | 扫码登录一次后取得官方逐字稿（2 秒），令牌自动续期 | 账号有被风控的风险，建议用小号 |
| 小鹅通（已登录浏览器） | 未登录时把登录页存成正文 | 课程正文 verified；全新环境首次 127 秒，已准备好的环境 7 秒 | 首次慢在浏览器读取组件的准备 |
| 小红书 | 命令行读取最长卡 15 分钟 | 缺分享参数的链接 0.6 秒给出下一步；Agent 浏览器快照路线可用 | 命令行后台读取仍读不出 |
| 公众号 | 经浏览器扩展约 95 秒 | 本地读取，9 个不同公众号的 9 篇文章全部成功，每篇 9–12 秒 | |
| 语音转写 / 抖音 | 装完模型后仍转写失败（PyAV 19 与 faster-whisper 不兼容；回退写死未安装的模型） | 中文、英文合成语音转写正确；抖音视频 176 秒出逐字稿 | 首次使用询问下载 1.6 GB 模型；HuggingFace 慢或不通时按实测速度换 ModelScope，SHA256 校验 |
| EPUB | — | 两本公开 EPUB 3 样书 1–2 秒转完；损坏文件 0.6 秒拒收并提示重新下载 | 书内插图未做 OCR 时如实标 partial |

## 网页正文完整度（英文网页与文档）

**同一新用户环境、同一时段**（13 个网页样本、104 个探针，各工具读同一份 HTML）：

| 工具 | 分数 | 噪音命中 |
|---|---:|---:|
| MarkItDown 0.1.9 | 92 | **13** |
| **Openmd 0.3.0** | **81** | **0** |
| Defuddle 0.19.6 | 80 | 0 |
| Firecrawl | 未重跑（需付费 key）；2026-11-20 历史 100/112 | — |

14 样本、112 探针的冻结口径（含一个非网页样本）：Openmd 0.2.3 为 78，0.3.0 为 82，噪音 0。RFC 9110 由约 9 KB 片段改为完整正文（50 万字、657 个标题）；arXiv 摘要页改为读取 HTML 全文，但正文结构还原仍弱（1/8）。测试窗口内 RFC 与 arXiv 多次取页失败，分数受网络波动影响。2026-11-20 维护者电脑上的 90/112 含私有 Defuddle，不代表新用户。

## PDF

| 项目 | Openmd | 对照 |
|---|---|---|
| 原生文字论文处理速度（13 份冻结 PDF，同环境 A/B 均值） | 0.2.3 43 s → **0.3.0 18 s**（输出逐字节相同）；最慢一篇 179 s → 46 s | pdfplumber 约 6.5 s（2026-11-20） |
| 表格结构（10 份带 LaTeX 源的表格 PDF，单元格 F1） | **0**（两轮尝试都没有通过真实论文的行列核对，未发布；保留正确的纯文字） | Docling 0.042、MarkItDown 0.002、pdfplumber 0（2026-11-20） |
| 没有表格的 PDF 误造表格 | 0/3 | Docling 在同样 3 份上造出 13 张表 |
| 隐藏文字注入（10 类合成样本） | 泄露 0/10 | MarkItDown 10/10、pdfplumber 10/10（2026-11-20） |

表格评分器按“第几张表、第几行第几列”对齐，且参照表不完整，会低估所有工具；下一版先修评分器再改方法。

## 扫描件与复杂版面（OmniDocBench 12 页）

新用户环境、同一批页面（评分器把“部分完成”的页计 0 分）：

| 路线 | 中文 | 英文 | Openmd 合成集 |
|---|---:|---:|---:|
| **Openmd 默认（Ovis3，失败时回退 MiniCPM）** | **33.0** | **31.9** | 68.8 |
| MiniCPM 单独 | 47.4 | 32.1 | 63.2 |

参考（2026-11-20，维护者环境）：MinerU basic 86.38 / 87.67，Docling 74.10 / 74.17。

这一项 Openmd **明显落后**。主因已查清：长页面切块识别后拼接时“合并不确定”，12 页里 8 页被标为部分完成；回退也没有被触发。扫描件首次使用时还没有询问下载 OCR 模型的入口。这三项都排在下一个小版本，本版不更换默认模型。

## 安装、升级与卸载（真实 Windows 电脑）

在一台真实 Windows 11 上，用 PowerShell 5.1 按 README 的一条命令（`irm … | iex`）安装，不设环境变量代理，程序与数据目录放在测试目录里，结束后还原注册表：

| 步骤 | 结果 |
|---|---|
| 一条命令安装（uv 下载并校验 SHA256，uv 自带 Python 3.12，装 `[all]` 约 2.4 GB） | 成功，约 15 分钟（主要是下载依赖） |
| 新开终端输入 `openmd` | 找到命令，`openmd --version` 正常 |
| 真实网页 | 19 秒取得全文 |
| `openmd update` | 19 秒升级，已选组件保留 |
| `openmd uninstall` | 程序、命令入口、用户 PATH 项、浏览器原生宿主注册全部删除；数据保留 |

## 复现

评分脚本、样本清单和逐样本回执在维护者工作区保存；公开样本链接与计分规则可按需提供。欢迎用真实失败链接提 issue。

## 附：发布前验收（2026-11-22 16:40–16:54）

从 main 构建 wheel，经清华镜像装进全新虚拟环境的 `[all]`（177 秒），按上文的新用户环境逐个运行真实链接并读回产物：

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
# Openmd new-user benchmark (2026-11-22, 0.3.0)

Only numbers from real runs are reported, including where Openmd falls behind.

**Method.** A wheel built from source is installed into a fresh virtual environment (dependencies via the Tsinghua PyPI mirror). `HOME`, `USERPROFILE`, `OPENMD_DATA_DIR`, `OPENMD_CONFIG` and the HuggingFace / ModelScope caches point to empty folders, `PATH` holds only the venv and Windows system folders, and Openmd starts outside the source tree, so no maintainer-only tools, hand-placed tokens or downloaded models take part. Machine: Windows 11, 16 GB RAM, RTX 4060 Laptop (8 GB), local proxy, China network; public sites failed intermittently during the window and this is recorded as such. Old and new builds alternate in the same time window (A/B/A/B). Sample sets and scorers are the ones frozen on 2026-11-20. For web pages, Defuddle and MarkItDown were re-run in the same new-user environment and window; other competitor scores come from the 2026-11-20 run and are dated.

**Platforms (new user, 0.2.3 → 0.3.0).** X: long post 10 s, short post 7 s without login, titles from the post (official embed → FxEmbed → browser extension); long-post full text depends on the hosted FxEmbed API, and without it the browser fallback takes about 70 s. Xiaoyuzhou: after one QR login the official transcript arrives in 2 s and tokens renew automatically (account risk; use a throwaway account). Xiaoe-tech with a logged-in browser: course text verified, 127 s on first use in a fresh environment, 7 s once prepared. Xiaohongshu: a link without share parameters returns a next step in 0.6 s; the agent-snapshot route works; the background CLI read still fails. WeChat: read locally, 9 articles from 9 accounts all verified in 9–12 s (about 95 s before). Speech and Douyin: 0.2.3 still failed after the model was installed (PyAV 19 broke faster-whisper and a fallback named a model that was not installed); 0.3.0 transcribes Chinese and English speech correctly and a Douyin video in 176 s; the 1.6 GB model is offered on first use, sources are ranked by measured speed (HuggingFace → ModelScope when slow) and SHA256-checked. EPUB: two public EPUB 3 books in 1–2 s; a damaged file is refused in 0.6 s with a re-download hint; illustrations without OCR are marked partial.

**English web completeness.** Same new-user environment and window, 13 web samples / 104 probes, every tool reading the same HTML: MarkItDown 0.1.9 92 (13 noise hits), **Openmd 0.3.0 81 (0 noise)**, Defuddle 0.19.6 80 (0 noise); Firecrawl not re-run (paid key), 100/112 on 2026-11-20. On the frozen 14-sample / 112-probe set Openmd went from 78 (0.2.3) to 82 (0.3.0), noise 0. RFC 9110 now comes out whole (about 500k characters, 657 headings) instead of a ~9 KB fragment; arXiv abstract pages now read the HTML full text, but structure recovery is still weak (1/8). RFC and arXiv fetches failed repeatedly in the window, so these scores carry network noise. The 2026-11-20 maintainer-machine 90/112 used a private Defuddle and is not a new-user figure.

**PDF.** Native-text paper speed (13 frozen PDFs, same-environment A/B mean): 43 s → **18 s** with byte-identical output; the slowest paper 179 s → 46 s; pdfplumber about 6.5 s. Table structure cell F1: **0** (two attempts failed the row/column check on real papers and were not shipped; correct plain text is kept) vs Docling 0.042, MarkItDown 0.002, pdfplumber 0. The table scorer aligns cells by table index and its reference tables are incomplete, which understates every tool; the next version fixes the scorer first. Invented tables on 3 table-free PDFs: 0 (Docling 13). Hidden-text injection leaks: 0/10 (MarkItDown 10/10, pdfplumber 10/10).

**Scanned and complex layouts (OmniDocBench, 12 pages, new user; partial pages score 0).** Chinese / English / Openmd synthetic set: **Openmd default (Ovis3 with MiniCPM fallback) 33.0 / 31.9 / 68.8**; MiniCPM alone 47.4 / 32.1 / 63.2. Reference (2026-11-20, maintainer environment): MinerU basic 86.38 / 87.67, Docling 74.10 / 74.17. Openmd is clearly behind. The cause is known: tiles of long pages are not merged when their overlap is uncertain (8 of 12 pages partial), the fallback was not triggered, and first use of scanned documents does not yet offer to download the OCR models. All three are scheduled for the next minor version; the default is unchanged.

**Install, update and uninstall (real Windows machine).** On a real Windows 11 with PowerShell 5.1, the README one-liner (`irm … | iex`) without proxy environment variables, program and data folders redirected to a test location and the registry restored afterwards: install (uv fetched and SHA256-checked, uv-managed Python 3.12, `[all]` about 2.4 GB) succeeded in about 15 minutes, mostly downloads; a new terminal finds `openmd` and `openmd --version` works; a real web page in 19 s; `openmd update` in 19 s with extras kept; `openmd uninstall` removed the program, launcher, user PATH entry and browser native-host registration and kept the data.

Scorers, sample lists and per-sample receipts are kept in the maintainer workspace; public sample links and scoring rules are available on request. Real failing links are welcome as issues.

**Pre-release acceptance (2026-11-22 16:40–16:54).** A wheel built from main was installed with `[all]` into a fresh venv (177 s) and run in the new-user environment above, reading back each output: web article verified 32 s (including first-use Defuddle setup); YouTube with captions verified 58 s; Bilibili auto-captions verified 9 s; WeChat article verified 10 s; X long post with images verified 10 s; X short post verified 7 s; Xiaoyuzhou after QR login verified with the official transcript 2 s; a Xiaoe-tech course with a logged-in browser verified 128 s; a Xiaohongshu link without share parameters failed immediately with a next step 0.6 s; Douyin with the local speech model verified 176 s; a local paper PDF partial (a few pages with an unverifiable text layer, labelled as such) 11 s; the slowest paper (2303.08774) partial 53 s; RFC 9110 / RFC 9112 verified full text 11 s / 7 s; an arXiv abstract page verified via HTML full text 11 s; an EPUB partial (illustrations without OCR) 2 s; a damaged EPUB refused with a re-download hint 0.6 s; a Chinese speech file verified 28 s.
