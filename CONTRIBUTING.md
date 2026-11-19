# 参与 Openmd

Openmd 只做一件事：**把链接变成干净的 Markdown 原文 / 逐字稿**，不总结、不改写、不润色。贡献的价值在于让更多链接"一次就拿到完整原文，缺了什么说清楚"。

## 最有用的三种贡献

| 你遇到了 | 去做 | 需要你提供 |
|---|---|---|
| 某个链接没取到正文、取错了、取不全 | 提 [链接提取失败](.github/ISSUE_TEMPLATE/link-failure.yml) issue | 链接、终端里 `提 issue 请附上：` 那一行、期望看到什么 |
| 想支持一个新平台 / 接一个新工具 | 先看 [接入新平台](docs/adding-a-platform.md)，大多数情况只需加一张**工具卡** | 真实样本链接 + 实际产物片段 |
| 想改核心行为（输出格式、权限开关、网络外发、凭证） | 先开 issue 讨论，再提 PR | 动机、影响范围、兼容性 |

## 迭代闭环

我们参照 yetone/magpie 的做法，所有修复都走同一条链：

```
反馈（issue） → 复现 → 先写一个会失败的测试 → 修复 → 真实链接验证 → 合并发布 → 复盘 → 规则晋升
```

- **复现优先**：维护者（或 AI Agent）先用 issue 里的链接复现。复现不了会请你补信息，不会直接关。
- **先红后绿**：每个 bug 修复都带一个"不修就失败"的测试，放在 `tests/`。
- **真实验证**：单元测试和 mock 只防回归。"能用"的结论必须来自真实链接跑真实 CLI、读真实产物。PR 里要写清用了哪些链接，以及**没验证什么**。
- **经验晋升**：返工和误判记进 [LESSONS.md](LESSONS.md)；同类教训在 3 个不同日期出现，就晋升为 [docs/code-standards.md](docs/code-standards.md) 里的硬规则，之后每个 PR 都按它检查。

## 提 PR

1. Fork 后从 `main` 开分支，名字类似 `fix/weixin-error-page`、`card/xxx-tool`。
2. 本地开发（Windows / macOS / Linux 同一条命令）：

   ```bash
   python scripts/dev_setup.py      # 建 .venv、装 openmd-cli[dev]，并检查 ffmpeg / Node / opencli，缺什么打印本系统的安装命令
   # 激活：Windows  .venv\Scripts\activate    macOS/Linux  source .venv/bin/activate
   python -m pytest
   ```

   也可以直接用 VS Code / GitHub Codespaces 打开仓库，`.devcontainer/` 会自动装好 Python、Node 和 ffmpeg。
   想在每次 `git push` 前自动跑全量测试：`python scripts/dev_setup.py --hooks`。

3. 按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 填写：改了什么、先失败的测试、真实样本证据、没验证什么。
4. CI 会在 Windows、macOS、Linux × Python 3.10 / 3.13 上跑全量 `pytest`。CI 不联网抓取真实平台，所以真实样本证据需要你在 PR 描述里贴出来。

测试在收集阶段和每个测试中自动使用临时配置、用户目录、运行时与模型缓存，
不会读取 checkout 的 `out/local-config.json`。需要配置的测试应在 `tmp_path` 下
创建合法的 `openmd.local/v1` JSON，再用 `monkeypatch.setenv("OPENMD_CONFIG", str(path))`
显式选择它；不要把测试指向真实用户配置或运行时目录。

### 跨平台约定

- 换行统一 LF（`.gitattributes` 已配置；只有 `.ps1/.cmd/.bat` 是 CRLF），编辑器设置见 `.editorconfig`。
- 路径用 `pathlib`，不拼接 `\` 或 `/`；不要假设 `C:\`、`~/Library` 之类的固定位置。
- 文件名长度按 **255 字节**（不是字符）设计：一个汉字在 UTF-8 下占 3 字节。
- 只在某个系统上成立的代码要显式分支，并给测试加 `skipif`，注明原因。
- 不依赖某个 Python 小版本的标准库行为（例如 `html.parser` 对畸形 HTML 的处理在 3.10 / 3.12 / 3.14 不同）。

### 哪些改动先开 issue

这几类改动影响所有用户的信任边界，请先开 issue 对齐，再写代码：

- 默认会自动开启的权限（网络、浏览器读取、媒体下载、本机转录）；**云服务永远不允许默认开启**。
- 任何把用户内容、链接或凭证发往第三方服务的行为。
- Markdown 输出格式、front matter 字段、`status` / `source_level` 口径。
- 读取浏览器登录态、cookie、账号会话的方式。

### 绝对不要提交

- 任何密钥、cookie、token、登录态。密钥只走环境变量。
- 受版权保护的他人正文：测试 fixture 只用自己写的或极短的示意片段。
- 本机绝对路径，比如 `C:\Users\…`、`E:\…`。

## 用 AI Agent 贡献

欢迎用 Claude Code、Codex、Cursor 等工具贡献。仓库里的 [AGENTS.md](AGENTS.md) 是给 Agent 的路由表，指向各子系统文档。要求和人一样：先失败的测试、真实样本证据、写清没验证什么。PR 由 Agent 生成时请在描述里注明，提交者对内容负责。

### 生成脱敏问题包

保存结果不完整或失败时，可以从本地 HTML 生成可检查的问题包：

```bash
openmd report feedback page.html --url https://example.org/page -o out/problem-report
```

也可以把扩展任务的 job id 作为输入。命令只写入脱敏的 `structure.html`、
`manifest.json`、`receipt.json`、`doctor.json` 和检查说明；原始页面、cookie、
账号信息不会进入问题包。附加到 issue 前请先逐项检查这些文件。

## 行为准则

就事论事，对人友善。维护者有权关闭长期无回应的 issue / PR（会先提醒）。

## 维护者发布

本仓库 `main` 是唯一公开代码主干；直接在这里维护代码、README、资源和 CI。
独立平台包通过 [平台插件合同](docs/adding-a-platform.md#5-independently-installed-platform-packages-contract-v1)
叠加安装。个人工作区、历史运行证据与凭证不属于发布输入。

发布前完成贡献者安装路径、全量 `python -m pytest`、构建包内容检查和本次授权范围内的真实样本回读，
再将已审核分支合并到 `main`。维护者明确批准后，从该提交打与包版本匹配的 `vX.Y.Z` tag，
由 `.github/workflows/release.yml` 构建、验证并发布。发布不经过私仓导出或中转仓库。
不要重写已发布 tag；失败时保留回执、修复后发布新版本。
