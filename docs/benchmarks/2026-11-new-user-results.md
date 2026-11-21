# Openmd 新用户环境实测报告（2026-11-22）

[English](#english) · 简体中文

这份报告只写真实运行出来的数字，Openmd 落后的项目也照写。

## 怎么测的

- **新用户环境**：从源码构建 wheel，装进一个全新的 Python 虚拟环境（依赖经清华 PyPI 镜像安装）；`HOME`、`USERPROFILE`、`OPENMD_DATA_DIR`、`OPENMD_CONFIG`、HuggingFace / ModelScope 缓存全部指向空目录；`PATH` 只保留虚拟环境和 Windows 系统目录；在源码目录之外启动。这样维护者电脑上的私有工具、手放的令牌和已下载的模型都不会参与。
- **机器**：Windows 11，16 GB 内存（测试时可用约 2 GB），RTX 4060 Laptop。网络经本地代理，国内网络条件。
- **对照**：同一时段、同一环境里交替运行旧版和新版（A/B/A/B），结果不一致的样本再各复测一次。
- **评分规则先冻结**：网页、PDF、OCR 的样本集与计分脚本沿用 2026-11-20 那一轮冻结的版本，跑之前不改。
- **竞品分数**：来自 2026-11-20 同一批样本、同一计分规则的运行，**今晚没有重跑**；当时 Openmd 的网页分数是在维护者电脑上测的（装有私有 Defuddle），不代表新用户，下表单独注明。

## 平台：新用户装完能不能用

| 平台 | 改之前（新用户） | 现在（新用户） | 说明 |
|---|---|---|---|
| X 帖子 | 首次 128 秒失败，第二次 17 秒成功；标题是“作者：正文前几十字” | 5 条公开样本 5/5 成功，每条 6–23 秒，标题取自帖子本身 | 不登录：X 官方内嵌接口 → FxEmbed 公开接口 → 浏览器扩展 |
| 小宇宙 | 失败（需要 App 令牌） | 扫码登录一次后 3 期均取得官方逐字稿；令牌过期后连续 2 轮自动续期成功 | 账号有被风控的风险，建议用小号 |
| 小鹅通（未登录） | 把“验证码登录 / 拖动拼图”页存成正文 | 拒收，并提示先在浏览器登录、打开课程 | |
| 小红书 | 命令行读取最长卡 15 分钟 | 约 60 秒内给出结论并引导浏览器扩展；Agent 浏览器快照路线实测成功 | 命令行后台读取本身仍读不出（维护者电脑上一样） |
| 公众号 | 经浏览器扩展约 95 秒 | 本地读取 5–20 秒 | 只用 1 篇公开文章验证 |
| 语音转写 | 不可用（没有模型） | 首次使用询问后下载；HuggingFace 不通时自动换 ModelScope，空缓存实测下载与 SHA256 校验通过 | 真实转写因测试机可用内存不足未在本轮验证 |

（收尾的全平台新用户验收结果见文末“附：发布前验收”。）

## 网页正文完整度（英文网页与文档，14 个样本、112 个探针）

| 工具 | 分数 | 环境 |
|---|---:|---|
| Firecrawl | 100 | 2026-11-20 |
| MarkItDown | 93 | 2026-11-20 |
| Defuddle | 92 | 2026-11-20 |
| Openmd（维护者电脑，含私有 Defuddle） | 90 | 2026-11-20，**不代表新用户** |
| **Openmd 新版（新用户环境，首次使用自动安装 Defuddle）** | **63** | 2026-11-22，同时段 A/B 均值 |
| Openmd 旧版（新用户环境） | 58 | 2026-11-22，同时段 A/B 均值 |

新用户环境下 Openmd 在英文网页上**明显落后**。部分样本（GitHub README、Python 博客）在两个版本中都会随机取页失败，看起来是经代理访问时的网络波动，因此这个分数本身也不稳定。中文网站与内容平台上，2026-11-20 那一轮 Openmd 领先（72% 探针命中，Firecrawl 54%、Defuddle 47%、Jina 37%），但那是在维护者电脑上测的（含私有 Defuddle），还没有在新用户环境重测。

## PDF

| 项目 | Openmd | 对照 |
|---|---|---|
| 原生文字论文处理速度（10 篇平均，同环境 A/B） | 旧版 133 s → **新版 69 s** | pdfplumber 约 6.5 s（2026-11-20） |
| 表格结构（10 份带 LaTeX 源的表格 PDF，单元格 F1） | **0**（两种新方法都没成功，未发布；宁可输出正确的纯文字） | Docling 0.042、MarkItDown 0.002、pdfplumber 0（2026-11-20） |
| 没有表格的 PDF 误造表格 | 0/3 | Docling 在同样 3 份上造出 13 张表 |
| 隐藏文字注入（10 类合成样本） | 泄露 0/10 | MarkItDown 10/10、pdfplumber 10/10（2026-11-20） |

## 扫描件与复杂版面（OmniDocBench 12 页，2026-11-20，本轮未改）

| 工具 | 中文 | 英文 |
|---|---:|---:|
| MinerU basic | 86.38 | 87.67 |
| MiniCPM（Openmd 可选模型） | 84.89 | 88.26 |
| Docling | 74.10 | 74.17 |
| **Openmd 默认（Ovis3）** | **73.14** | **80.33** |

这一项 Openmd 默认配置**明显落后**。在 Openmd 自己的合成测试集上结论相反（Ovis3 97.71、MiniCPM 84.23、MinerU 78.24），所以本轮没有更换默认模型；需要在内存充足时用新用户环境重测后再决定。

## 安装与卸载（隔离环境实测）

| 方式 | 需要预装 Python | 装 `[all]` 首次耗时 | 卸载后程序残留 |
|---|---|---:|---|
| uv（`scripts/install.ps1` 采用） | 否 | 约 408 s | 工具环境与命令入口清零；数据默认保留 |
| pipx | 是 | 约 565 s | 应用环境与命令入口清零；数据默认保留 |
| pip | 是 | — | 只删 Openmd 本身，约 100 个共享依赖（约 1.18 GB）保留 |

## 复现

评分脚本、样本清单和逐样本回执在维护者工作区保存；公开样本链接与计分规则可按需提供。欢迎用真实失败链接提 issue。

---

<a id="english"></a>
# Openmd new-user benchmark (2026-11-22)

Only numbers from real runs are reported, including where Openmd falls behind.

**Method.** A wheel built from source is installed into a fresh virtual environment (dependencies via the Tsinghua PyPI mirror). `HOME`, `USERPROFILE`, `OPENMD_DATA_DIR`, `OPENMD_CONFIG` and the HuggingFace / ModelScope caches point to empty folders, `PATH` holds only the venv and Windows system folders, and Openmd starts outside the source tree, so no maintainer-only tools, hand-placed tokens or downloaded models take part. Machine: Windows 11, 16 GB RAM (about 2 GB free during tests), RTX 4060 Laptop, local proxy, China network. Old and new builds alternate in the same time window (A/B/A/B) and disagreeing samples are re-run. Sample sets and scorers are the ones frozen on 2026-11-20. Competitor scores come from that 2026-11-20 run and were **not** re-run tonight; Openmd's web score from that run was measured on the maintainer's machine (with a private Defuddle) and is listed separately.

**Platforms (new user).** X: 5/5 public posts in 6–23 s without login (official embed → FxEmbed → browser extension), titles from the post. Xiaoyuzhou: after one QR login, 3/3 episodes with official transcripts and two automatic token renewals (account risk; use a throwaway account). Xiaoe-tech logged out: the login/puzzle page is refused instead of saved. Xiaohongshu: a conclusion within about 60 s instead of up to 15 minutes, with the browser-extension and agent-snapshot routes; the background CLI read still fails, as it does on the maintainer machine. WeChat: 5–20 s locally instead of about 95 s via the extension (one public article verified). Speech: models are offered on first use; a clean-cache download with ModelScope fallback and SHA256 check passed; real transcription was not verified because of low free memory.

**English web completeness (14 samples, 112 probes).** Firecrawl 100, MarkItDown 93, Defuddle 92 (2026-11-20); Openmd on the maintainer machine 90 (not a new-user figure); **Openmd new build, new user 63**; old build, new user 58 (same-window A/B means). Openmd clearly trails here, and some pages (GitHub READMEs, the Python blog) fail intermittently in both builds through the proxy. On Chinese sites the 2026-11-20 run had Openmd ahead (72% of probes vs Firecrawl 54%, Defuddle 47%, Jina 37%), but that was on the maintainer's machine with a private Defuddle and has not been repeated in a new-user environment.

**PDF.** Paper speed (10-paper mean, same-environment A/B): 133 s → **69 s**; pdfplumber about 6.5 s. Table structure cell F1: **0** (two new methods failed and were not shipped; correct plain text is kept instead) vs Docling 0.042, MarkItDown 0.002, pdfplumber 0. Invented tables on 3 table-free PDFs: 0 (Docling 13). Hidden-text injection leaks: 0/10 (MarkItDown 10/10, pdfplumber 10/10).

**Scanned and complex layouts (OmniDocBench, 12 pages, 2026-11-20, unchanged).** Chinese / English: MinerU basic 86.38 / 87.67, MiniCPM (an optional Openmd model) 84.89 / 88.26, Docling 74.10 / 74.17, **Openmd default (Ovis3) 73.14 / 80.33** — clearly behind. Openmd's own synthetic set points the other way (Ovis3 97.71, MiniCPM 84.23, MinerU 78.24), so the default was not changed this round.

**Install / uninstall (isolated).** uv (used by `scripts/install.ps1`): no Python needed, `[all]` first install about 408 s, program removed cleanly, data kept by default. pipx: needs Python, about 565 s, program removed cleanly. pip: removes only Openmd itself; about 100 shared dependencies (about 1.18 GB) stay.

Scorers, sample lists and per-sample receipts are kept in the maintainer workspace; public sample links and scoring rules are available on request. Real failing links are welcome as issues.
