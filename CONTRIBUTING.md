# Contributing to Openmd · 参与 Openmd

English · [简体中文](#简体中文)

Openmd turns what you can see but can't take out (links, local files, AI conversations) into the original text as Markdown. It does not summarize, rewrite or polish. A contribution is valuable when more sources come out complete on the first try, and when anything missing is stated plainly.

## The three most useful contributions

| You ran into | Do this | Please include |
|---|---|---|
| A link or file gave no text, the wrong text or partial text | Open a [link failure](.github/ISSUE_TEMPLATE/link-failure.yml) issue | The link or file type, the line after `提 issue 请附上：` printed in the terminal, and what you expected |
| You want a new platform or tool | Read [Adding a platform](docs/adding-a-platform.md) first; usually a **tool card** is enough | Real sample links and a snippet of the actual output |
| You want to change core behavior (output format, permission switches, outbound network, credentials) | Open an issue to discuss, then send a PR | Motivation, scope of impact, compatibility |

### Help wanted: the browser extension on macOS and Linux

The CLI, MCP server and right-click menu run on all three systems, but the browser extension currently works only on Windows. If you use a Mac or Linux, you can port it yourself: [docs/browser-extension-platforms.md](docs/browser-extension-platforms.md) lists every Windows-only place, the per-OS host locations, the tests to add, and the on-machine checks to paste into your PR. No maintainer hardware is needed to merge it.

## The iteration loop

Following yetone/magpie, every fix goes through the same chain:

```
feedback (issue) → reproduce → failing test first → fix → verify with real links → merge and release → retrospective → promote the rule
```

- **Reproduce first.** A maintainer (or an AI agent) reproduces with the link from the issue. If it can't be reproduced, we ask for more information instead of closing the issue.
- **Red before green.** Every bug fix comes with a test in `tests/` that fails without the fix.
- **Real verification.** Unit tests and mocks only guard against regressions. A claim that something works must come from running the real CLI on real links and reading the real output. The PR states which links were used and **what was not verified**.
- **Promote lessons.** Rework and misjudgments go into [LESSONS.md](LESSONS.md). When the same kind of lesson appears on 3 different dates, it becomes a hard rule in [docs/code-standards.md](docs/code-standards.md), and every PR is checked against it.

## Sending a PR

1. Fork, then branch from `main` with a name like `fix/weixin-error-page` or `card/xxx-tool`.
2. Local development (one command on Windows, macOS and Linux):

   ```bash
   python scripts/dev_setup.py      # create .venv, install openmd-cli[dev], check ffmpeg and print the install command for your OS
   # activate: Windows  .venv\Scripts\activate    macOS/Linux  source .venv/bin/activate
   python -m pytest
   ```

   - ffmpeg is the only system tool you install yourself.
   - Node.js comes with the `nodejs-wheel-binaries` dependency, so you don't install it separately.
   - OpenCLI is a managed component: on first need Openmd installs its pinned version into the Openmd data directory, and ignores any other `opencli` on `PATH`. **Don't `npm install -g` it.** Set `OPENMD_NPM_REGISTRY` to use a mirror, or `OPENMD_AUTO_INSTALL_TOOLS=0` to disable the automatic install.
   - You can also open the repository in VS Code or GitHub Codespaces: `.devcontainer/` installs Python, Node and ffmpeg.
   - To run the full test suite before every `git push`: `python scripts/dev_setup.py --hooks`.

3. Fill in the [PR template](.github/PULL_REQUEST_TEMPLATE.md): what changed, the test that failed first, real sample evidence, and what was not verified.
4. CI runs on Windows, macOS and Linux × Python 3.10 / 3.13. It runs the full `pytest`, checks generated files, builds the wheel and uses it outside the checkout, replays the external OpenCLI protocol offline, and runs a CLI smoke test. CI does not fetch real platforms, so paste your real sample evidence into the PR description.

Tests automatically use a temporary config, home directory, runtime and model cache, both at collection time and in every test. They never read the checkout's `out/local-config.json`. A test that needs configuration creates a valid `openmd.local/v1` JSON under `tmp_path` and selects it explicitly with `monkeypatch.setenv("OPENMD_CONFIG", str(path))`. Don't point tests at a real user config or runtime directory.

### Generated files: regenerate, don't hand-edit

CI fails when a generated file is out of date. Commit the regenerated output together with your change.

| You changed | Run | Generated output |
|---|---|---|
| `src/openmd/routing/declarations.json` (routes, tool cards, capabilities, defaults) | `python -m openmd.routing.generate` | `routes.json`, `cards/`, `docs/routes.generated.md` and the other projections |
| Local document formats or engine chains | `python -m openmd.engines.formats` | `docs/subsystems/document-formats.generated.md` |
| The OpenCLI Browser Bridge inside the extension | `python scripts/vendor_opencli_bridge.py` | `src/openmd/browser_extension/vendor/opencli-bridge/` (never edit by hand) |

Before pushing, each command accepts `--check` to confirm nothing is stale. When you bump OpenCLI, change the pins in `vendor_opencli_bridge.py` and `OPENCLI_VERSION` in `src/openmd/runtime/managed_tools.py` together.

### Cross-platform conventions

- Line endings are LF (configured in `.gitattributes`; only `.ps1/.cmd/.bat` use CRLF). Editor settings are in `.editorconfig`.
- Build paths with `pathlib`; don't join `\` or `/` by hand, and don't assume fixed locations such as `C:\` or `~/Library`.
- Design file names for **255 bytes**, not characters: one Chinese character is 3 bytes in UTF-8.
- Code that only holds on one OS gets an explicit branch, and its tests get a `skipif` with the reason.
- Don't rely on standard-library behavior of a particular Python minor version (for example, `html.parser` handles malformed HTML differently in 3.10, 3.12 and 3.14).

### Changes that need an issue first

These change the trust boundary for every user, so align in an issue before writing code:

- Permissions that turn on by default (network, browser reading, media download, local transcription). **Cloud services may never be on by default.**
- Anything that sends user content, links or credentials to a third-party service.
- The Markdown output format, front matter fields, and the meaning of `status` / `source_level`.
- How browser login state, cookies or account sessions are read.

### Never commit

- Any key, cookie, token or login state. Keys only come from environment variables.
- Other people's copyrighted text: test fixtures use your own writing or very short illustrative snippets.
- Local absolute paths such as `C:\Users\…` or `E:\…`.

## Contributing with AI agents

Claude Code, Codex, Cursor and similar tools are welcome. [AGENTS.md](AGENTS.md) is the routing table for agents and points to each subsystem's documentation. The requirements are the same as for people: a failing test first, real sample evidence, and a clear statement of what was not verified. Mark agent-generated PRs as such in the description; the submitter is responsible for the content.

### Generating a sanitized problem report

When a save is incomplete or fails, you can build a reviewable problem report from local HTML:

```bash
openmd report feedback page.html --url https://example.org/page -o out/problem-report
```

An extension job id also works as the input. The command only writes a sanitized `structure.html`, `manifest.json`, `receipt.json`, `doctor.json` and review notes; the original page, cookies and account information never enter the report. Check each file before attaching it to an issue.

## Code of conduct

Stick to the issue at hand and be kind to people. Maintainers may close issues or PRs that go unanswered for a long time, after a reminder.

## Maintainer releases

`main` in this repository is the only public code trunk: code, README, assets and CI are maintained here directly. Independent platform packages are installed on top through the [platform plugin contract](docs/adding-a-platform.md#5-independently-installed-platform-packages-contract-v1). Personal workspaces, historical run evidence and credentials are not release inputs.

Before a release, complete the contributor install path, the full `python -m pytest`, a check of the built package contents, and real sample readback within the authorized scope; then merge the reviewed branch into `main`. After explicit maintainer approval, tag that commit `vX.Y.Z` to match the package version; `.github/workflows/release.yml` builds, verifies and publishes. Releases never go through a private-repo export or a relay repository. Never rewrite a published tag; if a release fails, keep the receipt, fix it and publish a new version.

---

## 简体中文

Openmd 把看得到、拿不出的内容（链接、本地文件、AI 对话）变成 Markdown 原文。不总结、不改写、不润色。贡献的价值在于让更多内容"一次就拿到完整原文，缺了什么说清楚"。

## 最有用的三种贡献

| 你遇到了 | 去做 | 需要你提供 |
|---|---|---|
| 某个链接或文件没取到正文、取错了、取不全 | 提 [链接提取失败](.github/ISSUE_TEMPLATE/link-failure.yml) issue | 链接或文件类型、终端里 `提 issue 请附上：` 那一行、期望看到什么 |
| 想支持一个新平台 / 接一个新工具 | 先看 [接入新平台](docs/adding-a-platform.md)，大多数情况只需加一张**工具卡** | 真实样本链接 + 实际产物片段 |
| 想改核心行为（输出格式、权限开关、网络外发、凭证） | 先开 issue 讨论，再提 PR | 动机、影响范围、兼容性 |

### 欢迎贡献：macOS 和 Linux 上的浏览器扩展

命令行、MCP 和右键菜单支持三个系统，但浏览器扩展目前只支持 Windows。如果你用 Mac 或 Linux，可以自己完成移植：[docs/browser-extension-platforms.md](docs/browser-extension-platforms.md) 列出了所有只在 Windows 上实现的地方、各系统的宿主位置、需要补的测试，以及要贴进 PR 的本机验收步骤。合并不需要维护者有对应的机器。

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
   python scripts/dev_setup.py      # 建 .venv、装 openmd-cli[dev]，检查 ffmpeg，缺了就打印本系统的安装命令
   # 激活：Windows  .venv\Scripts\activate    macOS/Linux  source .venv/bin/activate
   python -m pytest
   ```

   - 需要你自己装的系统工具只有 ffmpeg。
   - Node.js 随 `nodejs-wheel-binaries` 依赖一起安装，不用单独装。
   - OpenCLI 是托管组件：首次需要时，Openmd 会把固定版本装进 Openmd 数据目录，并忽略 `PATH` 上的其他 `opencli`。**不要 `npm install -g`。** 需要镜像就设置 `OPENMD_NPM_REGISTRY`；想禁用自动安装就设置 `OPENMD_AUTO_INSTALL_TOOLS=0`。
   - 也可以直接用 VS Code / GitHub Codespaces 打开仓库，`.devcontainer/` 会自动装好 Python、Node 和 ffmpeg。
   - 想在每次 `git push` 前自动跑全量测试：`python scripts/dev_setup.py --hooks`。

3. 按 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md) 填写：改了什么、先失败的测试、真实样本证据、没验证什么。
4. CI 会在 Windows、macOS、Linux × Python 3.10 / 3.13 上跑：全量 `pytest`、生成文件检查、构建 wheel 并在 checkout 之外安装使用、离线回放 OpenCLI 外部协议、CLI 冒烟。CI 不联网抓取真实平台，所以真实样本证据需要你在 PR 描述里贴出来。

测试在收集阶段和每个测试中自动使用临时配置、用户目录、运行时与模型缓存，不会读取 checkout 的 `out/local-config.json`。需要配置的测试应在 `tmp_path` 下创建合法的 `openmd.local/v1` JSON，再用 `monkeypatch.setenv("OPENMD_CONFIG", str(path))` 显式选择它；不要把测试指向真实用户配置或运行时目录。

### 生成文件：重新生成，不要手改

生成文件过期时 CI 会失败。请把重新生成的产物和你的改动一起提交。

| 你改了 | 运行 | 生成的产物 |
|---|---|---|
| `src/openmd/routing/declarations.json`（路由、工具卡、能力、默认值） | `python -m openmd.routing.generate` | `routes.json`、`cards/`、`docs/routes.generated.md` 等投影 |
| 本地文档格式或引擎链 | `python -m openmd.engines.formats` | `docs/subsystems/document-formats.generated.md` |
| 扩展里的 OpenCLI Browser Bridge | `python scripts/vendor_opencli_bridge.py` | `src/openmd/browser_extension/vendor/opencli-bridge/`（禁止手改） |

推送前，以上命令都可以加 `--check`，确认没有过期的文件。升级 OpenCLI 时，`vendor_opencli_bridge.py` 里的版本钉和 `src/openmd/runtime/managed_tools.py` 的 `OPENCLI_VERSION` 要一起改。

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

也可以把扩展任务的 job id 作为输入。命令只写入脱敏的 `structure.html`、`manifest.json`、`receipt.json`、`doctor.json` 和检查说明；原始页面、cookie、账号信息不会进入问题包。附加到 issue 前请先逐项检查这些文件。

## 行为准则

就事论事，对人友善。维护者有权关闭长期无回应的 issue / PR（会先提醒）。

## 维护者发布

本仓库 `main` 是唯一公开代码主干；直接在这里维护代码、README、资源和 CI。独立平台包通过 [平台插件合同](docs/adding-a-platform.md#5-independently-installed-platform-packages-contract-v1) 叠加安装。个人工作区、历史运行证据与凭证不属于发布输入。

发布前完成贡献者安装路径、全量 `python -m pytest`、构建包内容检查和本次授权范围内的真实样本回读，再将已审核分支合并到 `main`。维护者明确批准后，从该提交打与包版本匹配的 `vX.Y.Z` tag，由 `.github/workflows/release.yml` 构建、验证并发布。发布不经过私仓导出或中转仓库。不要重写已发布 tag；失败时保留回执、修复后发布新版本。
