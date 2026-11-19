<div align="center">

# Openmd

**Everything to Markdown — just beginning.**

**把你看得到、却拿不出来的内容，变成 Agent 能用的 Markdown。**

[![PyPI](https://img.shields.io/pypi/v/openmd-cli?color=3b5bdb)](https://pypi.org/project/openmd-cli/)
[![Python](https://img.shields.io/pypi/pyversions/openmd-cli)](https://pypi.org/project/openmd-cli/)
[![CI](https://github.com/baigao417/openmd/actions/workflows/ci.yml/badge.svg)](https://github.com/baigao417/openmd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/baigao417/openmd/blob/main/LICENSE)

[English](https://github.com/baigao417/openmd/blob/main/README.md) · 简体中文 · [日本語](https://github.com/baigao417/openmd/blob/main/README.ja.md) · [한국어](https://github.com/baigao417/openmd/blob/main/README.ko.md)

</div>

AI 时代，模型越来越聪明，但它能读到的东西，取决于你能喂给它什么。

大量真正有价值的信息困在各个平台里：要登录的文章、视频里的原话、播客里的对谈、和 AI 聊了一下午的对话、群聊里的讨论。你看得到，却拿不出来。

Openmd 想做的是一块基石：把这些内容变成开放、可携带、**人与 Agent 都能直接阅读和使用**的 Markdown。网页文章、视频平台、办公文件、聊天对话，一切都可以成为你专业知识库的原料。

我们首先面向 AI native：保留原文不改写，附上来源与完整度回执，遇到错误页和登录墙直接拒收。因为对 Agent 来说，“这份材料能不能信”和“有没有这份材料”同样重要。

万物 Markdown，这只是开端。

---

## 快速开始

Openmd 提供命令行工具与 Python 模块 `openmd`（包名 `openmd-cli`，需要 Python 3.10 或更新版本，支持 Windows、macOS、Linux）。

```bash
# 安装基础包（也可以用 pipx install openmd-cli 或 uv tool install openmd-cli）
pip install openmd-cli

# 检查本地运行环境与可选组件状态
openmd doctor

# 粘贴一个链接，回车后选择保存位置
openmd

# 将单篇网页内容直接保存到指定目录
openmd "https://example.com/article" -o ./notes

# 供 Agent 或自动化脚本调用，不再询问，输出机器可读的 JSON 运行回执
openmd "https://example.com/article" -o ./notes --json

# 批量处理链接清单（每行一个链接）；批量命令用 --out 指定目录
openmd batch links.txt --out ./notes
```

每个链接会在你选择的目录里生成一份 `标题.md`。运行日志写在 `~/.openmd/runs/`，不会混进你的笔记。

当你需要处理本地办公文档或启用本地音视频转写时，可按需安装扩展依赖：

```bash
# 安装文档与图片解析依赖
pip install "openmd-cli[document]"

# 安装图片与扫描件 OCR 依赖
pip install "openmd-cli[ocr]"

# 安装可选的 Pandoc 转换回退
pip install "openmd-cli[pandoc]"

# 安装本地音视频转写（ASR）依赖
pip install "openmd-cli[asr]"
```

> 注：文档解析、OCR 与本地 ASR 除安装 Python 依赖外，还可能需要准备 FFmpeg 或对应本地模型，可通过 `openmd doctor` 查看环境就绪情况。

如果你想把 Openmd 接入自己的 Agent 或自动化工具链，可以阅读 [工具集成指南](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)。

## Agent 的 MCP 接口

有 shell 时用 CLI + SKILL.md；需要工具发现、类型化参数与客户端逐工具授权时用 MCP。安装可选 extra，再启动 stdio 服务。Claude Code：

```bash
pip install "openmd-cli[mcp]"
claude mcp add --transport stdio openmd -- openmd mcp
```

一行 `uvx` 配置（无需预先安装）：

```bash
uvx --from "openmd-cli[mcp]" openmd mcp
```

Cursor：在 **Cursor Settings → MCP → Add new MCP Server** 中加入同一个 stdio 服务：

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

Windows：使用可执行文件绝对路径；必要时设置 UTF-8 输出：

```json
{
  "mcpServers": {
    "openmd": {"command": "C:\\path\\to\\openmd.exe", "args": ["mcp"], "env": {"PYTHONIOENCODING": "utf-8"}}
  }
}
```

Claude Desktop：在 MCP 配置中加入以下内容；必要时把可执行文件改成绝对路径。

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

四个工具为 `openmd_save`、`openmd_save_page`、`openmd_job`、`openmd_doctor`。`openmd_save` 可接收一个 source 或最多 20 个 `sources`；`openmd_job(action="cancel")` 可取消排队或运行中的任务。客户端提供 `progressToken` 时会收到队列进度，也可以继续轮询。已完成 Markdown 产物通过 `openmd://jobs` 和 `openmd://job/{job_id}` resources 暴露，读取有上限。输出目录必须显式提供绝对路径。HTML 快照接收字符串或绝对文件路径，UTF-8 上限 2 MiB。五个能力参数默认关闭，需显式开启 `network`、`browser`、`media`、`asr` 或 `cloud`。复用 CLI 的能力开关；MCP 不因给出输出目录推断日常 CLI 的自动授权，也不记住该目录。stdio 配置需要客户端可启动本地进程；远程 connector 需要另接传输桥。

---

## 为什么适合作为 AI 与知识库的前置入口

Openmd 本身不是知识库或 RAG 检索系统，它专注于做好“把信息拿出来、理清楚”这一步基石工作：

1. **保留原文，把理解权留给后续环节**
   默认目标是完整保留原文、字幕或逐字稿，仅整理分段和空白，不自动总结、翻译或改写。这样既方便人核对原话，也能避免在进入 AI 上下文前丢失关键细节。

2. **附带来源、元数据与时间定位**
   生成的 Markdown 可包含标题、作者、原始链接等元数据（取决于来源实际提供的信息），音视频逐字稿支持保留时间定位，并附带来源与完整性说明。同时支持可选的结构化 package 输出，方便下游工具按需消费。

3. **主动拒收无效页面**
   遇到已识别的错误页、登录墙等情况时，Openmd 会直接拒收并通过回执反馈，而不是把错误页面静默保存成看似正常的笔记（受限于网页形态多变，仍无法承诺识别所有异常页面）。

---

## 当前能力

| 类别 | 来源 | 状态 | 运行说明 |
| :--- | :--- | :--- | :--- |
| **网页与社区** | 普通网页文章 | 可用 | 安装即用 |
| | 微信公众号文章、X 帖子 | 可用 | 需配合 Openmd 浏览器扩展及你已有的访问权限 |
| | 保存当前页、登录提醒 | 可用 | 需浏览器扩展 |
| **音视频与逐字稿** | YouTube（有字幕）、小宇宙 | 可用 | 小宇宙首次使用时会自动准备一个约 20 MB 的读取组件 |
| | B 站、小鹅通 | 可用 | 需浏览器扩展；B 站 AI 字幕还需要登录 |
| | 无字幕音视频、本地音视频 | 需扩展依赖 | 需 `[asr]` 依赖、FFmpeg 和本地语音模型 |
| **本地文档与图片** | PDF、DOCX、PPTX、XLSX、图片 | 需扩展依赖 | 需 `[document]` 依赖；复杂版面与扫描件可能进入额外的 OCR 路线 |
| **办公协作** | 飞书文档 | 部分可用 | 需本机已安装并登录 `lark-cli`，命令中加 `--enable-network-backends` 显式允许联网读取 |
| **AI 对话** | 豆包、Gemini、千问、元宝、Grok、DeepSeek（已打开的对话页） | 部分可用 | 需浏览器扩展：在对话页点击即保存已加载的内容，也可由 Agent 用 `--as-snapshot` 交给 Openmd；只覆盖当前可见分支，默认省略思考过程 |
| | ChatGPT 对话页；ChatGPT / Claude 官方导出 ZIP 或 JSON | 实验 | ChatGPT 页面未显示用户提问时保存为 partial；官方导出在本地导入，无需浏览器 |
| **聊天记录** | QQ 群导出文件（JSON / HTML / TXT） | 暂停 | 导出文件解析已实现，暂未作为稳定能力开放；Openmd 不读取或解密聊天数据库 |
| **实验性支持** | 抖音、小红书 | 实验 | 需浏览器扩展与登录；这些网站经常拦截自动读取，暂不承诺稳定或全量可用 |

### 本地文档格式

`[document]` 是本地文档解析的基础依赖。下表来自全新安装实测，描述原生文字，并如实标出 partial。图片或扫描件请安装 `[ocr]`；可选的转换回退请安装 `[pandoc]`。详见[格式链与限制](https://github.com/baigao417/openmd/blob/main/docs/subsystems/document-formats.generated.md)。

| 格式 | 只装 `[document]` 的状态 | 可选增强 / 限制 |
|---|---|---|
| 含原生文字的 PDF | 可用 | 扫描件另需本机 OCR；不认证表格几何结构 |
| DOCX | 部分可用 | 可取得文字；并非所有样本都认证表格形状 |
| PPTX、XLSX、XLS | 可用 | 嵌图、备注、公式缓存和二进制图片限制会明确报告 |
| HTML、HTM、EPUB、RTF、ODT、ODS、ODP、FB2 | 可用或部分可用 | 本地文件，不执行脚本；复杂版面可能 partial |
| CSV、TSV、JSON、XML、IPYNB、MSG | 可用 | 大表可能截断；不执行 Notebook；MSG 附件标为缺口 |
| DOC、PPT | partial 文字回退 | 安装 LibreOffice 使用首选转换；不认证版式和旧 PPT 活跃幻灯片顺序 |
| ZIP | 部分可用，逐条报告状态 | 音视频 / 图片另需 ASR / OCR；受容器资源上限约束 |
| MOBI、AZW3 | 需安装 Calibre | 缺工具返回 `engine_unavailable`；拒收加密电子书，不解密 |

在你的电脑上运行 `openmd doctor` 可以看到哪些已经就绪。链接处理失败时，Openmd 会打印原因，以及一行可以直接贴进 [问题反馈](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml) 的信息。

---

## 输出长什么样

```markdown
---
title: "我们是怎么做出这盏阅读灯的"
author: "Example Studio"
url: "https://www.bilibili.com/video/BV..."
published: "2026-05-24"
platform: "bilibili"
---

# 我们是怎么做出这盏阅读灯的

**00:00:00**

今天想给大家看看我们花了三个月做的这盏灯以及前两版为什么失败了

**00:00:31**

第一版原型晚上太亮了所以我们在灯罩下面加了第二个光源

*来源：自动字幕；识别错误保持原样。*
```

- 开头是 YAML front matter，Obsidian、Logseq 等笔记软件和脚本都能直接读取。
- 逐字稿按约 30 秒、约 450 字或自然停顿分段，每段以时间戳开头：人可以快速浏览，Agent 可以引用到具体时刻。
- 最后一行说明文字来源（原文、平台字幕、自动字幕、语音识别……）以及内容是否完整。

---

## 浏览器扩展与本地运行

- **复用已有浏览器访问权限**：微信公众号、X、B 站等部分读取路线需要配合 Openmd 浏览器扩展使用，部分内容需要你已在浏览器中登录。浏览器扩展目前支持 **Windows 系统的 Chrome / Edge**。Openmd 仅使用你已有的合法访问权限，不承诺绕过登录限制、付费墙、DRM 或其他访问控制。
- **本地优先与联网边界**：Openmd 坚持本地优先，但读取在线内容时必然会访问原网站，部分运行组件在首次使用时可能触发下载；涉及云服务的功能则需要你明确开启。

安装扩展：

1. 运行 `openmd browser-setup`。它会注册本地连接，并打开扩展所在的文件夹。
2. 打开 `chrome://extensions`（或 `edge://extensions`），开启 **开发者模式**，点击 **加载已解压的扩展程序**，选择刚才那个文件夹。
3. 在任意页面点击 Openmd 图标即可保存。选中多个标签页可以一次全部保存；选中一段文字后右键，可以保存其中的所有链接。

也可以从 [Releases](https://github.com/baigao417/openmd/releases) 下载扩展 ZIP，解压后在运行 `openmd browser-setup` 之后加载这个文件夹。

- **默认浏览器行为**：读取不弹窗，默认在现有窗口的不激活后台标签中进行；任务创建的标签默认静音，任务结束后自动关闭。

Chrome 可能显示“Openmd 已开始调试此浏览器”的浏览器提示栏；这是浏览器提供的提示，扩展无法隐藏。

---

## 托管 OpenCLI

OpenCLI 是 Openmd 托管的浏览器与平台组件。`openmd opencli list` 和
`openmd opencli <site> <command> [args]` 会透传只读命令，并保留其输出与退出码。
写操作和未知命令默认 fail closed；只有显式加 `--allow-write` 才能放行。Openmd 不会打印凭证内容。

---

## 配置

| 环境变量 | 作用 |
|---|---|
| `OPENMD_AUTO_INSTALL_TOOLS=0` | 永不自动下载浏览器读取组件 |
| `OPENMD_NPM_REGISTRY` | 首次下载读取组件时使用的 npm 镜像 |
| `OPENMD_SHOW_BROWSER=1` | 让后台浏览器窗口保持可见，便于排查问题 |
| `OPENMD_DATA_DIR` | Openmd 存放设置、日志和工具的位置（默认 `~/.openmd`） |

更多：[工具集成](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)、[当前路由](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md)、[架构地图](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html)。

---

## 扩展方向

**Everything to Markdown** 是一个长期愿景，我们正在继续探索以下尚未交付的方向：

- **网页 AI 对话导出**读取你已经打开的对话页（扩展点击，或 Agent 用自己的浏览器经 `--as-snapshot` 交给 Openmd），不会在后台重新打开链接。目前为部分可用：只保存已加载的当前可见分支。ChatGPT / Claude 官方导出支持离线导入：`openmd go export.zip -o archive --chatgpt-export` / `--claude-export`（也接受 JSON，可自动识别格式）。Claude 可用 `--claude-title`、`--claude-since`、`--claude-until` 筛选；非文本块与附件只保留占位并计数。重复导入会保护用户修改。
- **电子书与更多文本格式**：支持 EPUB、RTF、MOBI、AZW3 等格式转换。
- **更广的文档解析能力**：持续提升复杂版面文档的提取质量。

以上方向在完成真实样本验证之前，不作为已交付功能。

---

## 常见问题

**Openmd 会总结或润色文字吗？**
不会。它只调整空白和分段。总结、翻译和润色交给你接下来使用的工具。

**我的数据会去哪里？**
哪里都不去。Openmd 在本地运行，用你自己的网络和浏览器读取页面，把文件写进你选择的目录。它从不保存 cookie 或密码。

**为什么包名叫 `openmd-cli`？**
PyPI 上的 `openmd` 已被一个无关项目占用。安装之后，命令仍然是 `openmd`。

**某个链接处理失败了。**
加上 `--verbose` 再运行一次，可以看到尝试了哪些读取路线；然后附上 Openmd 打印的那一行，提交一份 [链接失败报告](https://github.com/baigao417/openmd/issues/new?template=link-failure.yml)。失败的真实链接是最有价值的贡献。

---

## 参与贡献

欢迎附带真实链接的问题反馈、新平台接入和修复。大多数新平台只需要一张很小的 JSON “工具卡”，见 [接入新平台](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md)。在 Windows、macOS、Linux 上开发的方式相同：

```bash
git clone https://github.com/baigao417/openmd && cd openmd
python scripts/dev_setup.py     # 创建 .venv、安装开发依赖、检查 ffmpeg / Node
python -m pytest
```

每个修复都要附带一个修复前会失败的测试；每个“能用了”的结论都要附带一条真实运行过的链接。提交 Pull Request 前请先阅读 [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md) 和 [代码规范](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md)。

---

## 致谢与许可

Openmd 建立在 [OpenCLI](https://github.com/jackwener/opencli)（浏览器读取，已内置）、[trafilatura](https://github.com/adbar/trafilatura)、[youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)、Bilibili-Harvest（Lanbin07）、[faster-whisper](https://github.com/SYSTRAN/faster-whisper) 和 [MarkItDown](https://github.com/microsoft/markitdown) 等开源项目之上。详细信息与许可证见 [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md)。

本项目采用 [MIT](https://github.com/baigao417/openmd/blob/main/LICENSE) 许可证开源。请尊重你所保存内容的版权；Openmd 面向个人存档与研究使用。

**Everything to Markdown — just beginning.**
