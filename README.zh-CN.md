<div align="center">

# Openmd

**把任何链接存成干净的 Markdown：原文和完整逐字稿。**

[![PyPI](https://img.shields.io/pypi/v/openmd-cli?color=3b5bdb)](https://pypi.org/project/openmd-cli/)
[![Python](https://img.shields.io/pypi/pyversions/openmd-cli)](https://pypi.org/project/openmd-cli/)
[![CI](https://github.com/baigao417/openmd/actions/workflows/ci.yml/badge.svg)](https://github.com/baigao417/openmd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/baigao417/openmd/blob/main/LICENSE)

[English](https://github.com/baigao417/openmd/blob/main/README.md) · 简体中文

</div>

网页文章、公众号、B 站和 YouTube 视频、播客等一切形式……Openmd 把它们统一存成一种人和 AI agent 都好读的 Markdown。它只保留原作者的原话：不总结、不改写。拿不全的时候，它会说清缺了什么，而不是存下一页错误内容。

```bash
pip install openmd-cli
openmd "https://www.bilibili.com/video/BV1GJ411x7h7"
```

## 亮点

- **一条命令，多种来源**：网页文章、公众号文章、X 帖子、B 站和 YouTube 视频、小宇宙播客、PDF 和 Office 文件。
- **原文优先**：字幕和逐字稿按时间整理成带时间戳的段落，方便阅读，但一个字都不改。
- **结果诚实**：错误页、登录墙、"请验证你是真人"之类的页面直接拒收，不会存成正文；只拿到一部分时会明确标注。
- **装完就能用**：不用手动装 Node.js 或其他工具，需要借用浏览器读取时，Openmd 第一次用到会自动准备好。
- **必要时借用你的浏览器**：一个浏览器插件，让 Openmd 用你已经登录的 Chrome / Edge 读取页面，读取在最小化的后台窗口里进行。
- **默认完全本地**：所有处理都在你的电脑上完成，云服务除非你主动开启，否则永远不用。
- **也为 agent 设计**：`openmd "<链接>" -o <目录> --json` 全程不提问，返回机器可读的回执。

## 安装

需要 Python 3.10 及以上，支持 Windows、macOS、Linux。

```bash
pip install openmd-cli          # 或 pipx install openmd-cli / uv tool install openmd-cli
openmd doctor                   # 看看现在能处理哪些内容、缺什么怎么装
```

安装包叫 `openmd-cli`，装好后命令和 Python 模块都叫 `openmd`。

## 用法

```bash
openmd                                   # 粘贴链接，回车，选保存位置
openmd "https://sspai.com/post/73145"    # 处理这个链接，再问存到哪里
openmd "https://..." -o ~/notes          # 直接存进这个文件夹，不提问
openmd "https://..." -o ~/notes --json   # 同上，并输出 JSON 回执，适合脚本和 agent
openmd batch links.txt --out ~/notes     # 一行一个链接，批量处理
```

每个链接存成所选文件夹里的 `标题.md`。运行日志放在 `~/.openmd/runs/`，不会混进你的笔记。

## 能处理什么

| 内容 | 装完即可用 | 还需要 |
|---|:---:|---|
| 网页文章 | ✓ | — |
| YouTube 有字幕的视频 | ✓ | — |
| 小宇宙播客逐字稿 | ✓ | —（第一次使用时自动准备约 20MB 的读取组件） |
| 公众号文章、X 帖子、B 站字幕 | ✓ | Openmd 浏览器插件；B 站 AI 字幕还需要你已登录账号 |
| 没有字幕的视频和播客 | | `pip install "openmd-cli[asr]"` 加本机语音模型（[说明](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)） |
| PDF、Word、PPT、Excel、图片 | | `pip install "openmd-cli[document]"` |
| 抖音、小红书 | 实验性 | 浏览器插件并已登录你自己账号 |

运行 `openmd doctor` 可以看到你这台机器上哪些已经就绪。链接处理失败时，Openmd 会说明原因，并打印一行信息，直接贴进[问题反馈](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml)就行。

## 产物长什么样

```markdown
---
title: "台灯是怎么做出来的"
author: "示例工作室"
url: "https://www.bilibili.com/video/BV..."
published: "2026-05-24"
platform: "bilibili"
---

# 台灯是怎么做出来的

**00:00:00**

今天想给大家看看我们花了三个月做的这盏台灯 以及前两个版本为什么都失败了

**00:00:31**

第一个原型晚上太亮 所以我们在灯罩下面又加了一个光源

*来源：自动字幕；识别错误保持原样。*
```

- 开头是 YAML 元数据，Obsidian、Logseq 等笔记软件和脚本都能直接读。
- 逐字稿按约 30 秒、约 450 字或自然停顿分段，每段开头是时间戳：人能快速扫读，agent 能按时间点引用。
- 最后一行注明正文来源（原文、平台字幕、自动字幕、语音识别……）以及是否完整。

## 浏览器插件

有些网站只对已登录的浏览器显示内容。Openmd 插件（Chrome、Edge，目前支持 Windows）让 Openmd 借用你自己的浏览器读取这些页面。

1. 运行 `openmd browser-setup`，它会注册本机连接，并打开插件所在文件夹。
2. 打开 `chrome://extensions`（Edge 为 `edge://extensions`），打开右上角的**开发者模式**，点**加载已解压的扩展程序**，选择那个文件夹。
3. 在任意页面点 Openmd 图标即可保存。按住 Ctrl 多选几个标签页可以一次全存；在页面上选中一段内容后右键，可以保存其中所有链接。

读取过程中，Chrome 顶部可能出现"Openmd 已开始调试此浏览器"的提示条。这是 Chrome 对所有使用调试接口的插件统一显示的提示，插件自己无法隐藏；读取本身在一个单独的最小化窗口里进行。

## 配置

| 环境变量 | 作用 |
|---|---|
| `OPENMD_AUTO_INSTALL_TOOLS=0` | 不自动下载浏览器读取组件 |
| `OPENMD_NPM_REGISTRY` | 下载那个组件时使用的 npm 镜像 |
| `OPENMD_SHOW_BROWSER=1` | 让后台浏览器窗口保持可见，方便排查问题 |
| `OPENMD_DATA_DIR` | Openmd 存放设置、日志和工具的位置（默认 `~/.openmd`） |

更多：[工具接入规范](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)、[当前路由](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md)、[全景图](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html)。

## 常见问题

**Openmd 会总结或润色内容吗？**
不会。它只调整空白和分段。总结、翻译、润色交给你后续用的工具。

**我的数据会去哪里？**
哪里都不去。Openmd 在本机运行，用你自己的网络和浏览器读取页面，文件写进你选的文件夹，不保存任何 cookie 或密码。

**有个链接处理不了怎么办？**
加上 `--verbose` 再跑一次，看看尝试了哪些读取方式，然后带着 Openmd 打印的那行信息提一个[链接提取失败](https://github.com/baigao417/openmd/issues/new?template=link-failure.yml)的反馈。失败的链接是最有价值的贡献。

## 参与贡献

欢迎带着真实链接提问题、接入新平台、修 bug。大多数新平台只需要加一张小小的 JSON "工具卡"，见[接入新平台](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md)。Windows、macOS、Linux 的开发步骤都一样：

```bash
git clone https://github.com/baigao417/openmd && cd openmd
python scripts/dev_setup.py     # 建 .venv、装开发依赖、检查 ffmpeg / Node
python -m pytest
```

每个修复都要附一个修复前会失败的测试；每个"能用了"的结论都要附一条真实跑过的链接。提 PR 前请先读 [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md) 和[代码规范](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md)。

## 致谢

Openmd 建立在这些项目之上：[opencli](https://github.com/jackwener/opencli)（浏览器读取）、[trafilatura](https://github.com/adbar/trafilatura)、[youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)、Bilibili-Harvest（Lanbin07）、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[MarkItDown](https://github.com/microsoft/markitdown)。详情与许可证见 [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md)。

## 许可证

[MIT](https://github.com/baigao417/openmd/blob/main/LICENSE)。请尊重你所保存内容的版权；Openmd 面向个人存档与研究用途。
