# Openmd

`Openmd` 是一个多平台内容摄取工具：把平台 URL、作者主页、合集、课程目录、playlist、网页文章或通用可访问视频转成统一 Markdown。

它不是漂亮笔记生成器，也不是完整 Agent 工作站。它只是 Markdown-first 工作流中的内容摄取节点，目标是 **高效、可靠地取得原始正文 / 逐字稿**。

```text
platform url / author / playlist / catalog / web page / generic media
  -> router
  -> backend capability registry
  -> platform adapter
  -> native text / transcript / media fallback / ASR
  -> normalized raw Markdown
  -> local archive / truth-source review / downstream agent workflow
```

## 30 秒上手

```powershell
pip install openmd-cli            # 或 pipx install openmd-cli；需要 Python 3.10+
openmd "https://sspai.com/post/73145"        # 粘贴链接 → 回车选保存位置 → 得到 标题.md
openmd "https://..." -o D:\notes             # 脚本 / AI Agent：给了目录就直接跑完
openmd doctor                                # 看看现在能处理哪些内容、缺什么怎么装
```

| 内容 | 装完即可用 | 需要额外安装 |
|---|---|---|
| 网页文章、YouTube 有字幕的视频 | ✓ | — |
| B 站字幕、公众号、X 等需要浏览器读取的平台 | 首次自动准备浏览器读取组件（约 20MB，一次） | Openmd 浏览器插件（`openmd browser-setup`），借用你已登录的浏览器在后台读取，窗口自动最小化 |
| 无字幕视频、播客（本机转录） | | `pip install "openmd-cli[asr]"` + 模型，见 [工具接入规范](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md) |
| 图片、PDF、Office | | `pip install "openmd-cli[document]"` |
| 浏览器一键保存 | | `openmd browser-setup`（目前仅 Windows，见下文） |

所需运行环境随 `pip install openmd-cli` 一起装好，不需要另装 Node.js 或其他工具。不想自动下载可设 `OPENMD_AUTO_INSTALL_TOOLS=0`，npm 慢可设 `OPENMD_NPM_REGISTRY` 指向镜像；想看到后台读取窗口（排查问题时）可设 `OPENMD_SHOW_BROWSER=1`。第三方组件与许可证见 [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md)。

取不到时 Openmd 不会存一份垃圾文件：它会说明原因，并打印一行 `提 issue 请附上：…`，复制到 [链接提取失败](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml) 即可。想参与开发见 [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md)。

## 输入链接后保存

在终端执行 `openmd` 后粘贴链接，或直接执行 `openmd "https://example.com/article"`。粘贴后直接开始提取，不做任何确认：公开读取、浏览器读取（可能使用现有登录会话）、必要时下载媒体和本机转录按路线需要自动开启；云服务从不自动开启。非交互运行（脚本 / Agent）只要给出保存目录（`-o` / `--save-to`）就按同一档自动开启；显式传入能力开关时只开这些，`--yes` 仅为兼容保留。提取成功后选择保存目录，回车沿用上次成功保存的位置，输入 `n` 取消。默认直接保存 `标题.md`，同名时才加后缀；运行日志放在 `~/.openmd/runs/`，不混入所选文件夹。终端显示“已取得正文”和“已保存”，`--verbose` 显示工具路线，`--json` 输出机器回执。

带视频的帖子（目前 X）：默认只查视频时长，在正文后注明“本帖含视频（约 N 分钟），未转文字”；`--video text` 临时下载视频、本机转写并把逐字稿接在正文后（下载文件用完即删）；`--video file` 另把视频文件存进同名 assets 文件夹并在 Markdown 里嵌入。视频这一步失败不影响正文保存，只在文件里写明原因。

默认 Markdown 按说话人、章节、停顿或约 30 秒合并字幕段落，保留原字词和错误，不补写标点或总结。`--raw` 保留原字幕逐行/表格排版。YouTube/B 站标题作者缺失时，在公开网络许可内补取元数据，失败仍保存正文并说明缺失；取得正文不等于已核对全文准确性与完整性。

```powershell
openmd
openmd go "https://example.com/article"
openmd "https://example.com/video" --raw
# 自动化：给了目录即可；只想开公开网络时显式写 --enable-network-backends 收窄
openmd "https://example.com/article" -o "D:\notes" --json
openmd doctor --profile media
openmd doctor --profile tools
openmd probe --enable-network-backends --enable-browser-backends # 本机 out/probe-config.json 配样本，模板见 docs/examples/probe-config.example.json；--platform bilibili 只测一个平台，报告在 out/probe/<date>/，不下载媒体或跑 ASR
openmd routes --format markdown
```

`openmd --help` 只列日常命令（`go`、`batch`），`openmd go --help` 只列 `--raw / --save-to / --video / --json / --verbose`；其余子命令和参数照常可用，只是不在帮助里显示（`openmd <命令> -h` 查看）。

### 浏览器插件（Chrome / Edge）

1. 终端运行一次 `openmd browser-setup`（只写当前用户注册表和 `~/.openmd/browser/`；`--uninstall` 撤销）。
2. 打开 `chrome://extensions`，开启“开发者模式”，“加载已解压的扩展程序”，选 `browser-setup` 打印并自动打开的文件夹（`~/.openmd/browser/extension`，升级 openmd 后重跑一次 `browser-setup` 即同步）。插件 ID 固定为 `lceckiadcfloncbklcojdiimbpollngh`。
3. 点工具栏的 Openmd（仓鼠）图标：直接保存当前页到默认位置，图标出现 ✓ 即完成；失败才弹系统通知说明原因。
   - 多选标签页：按住 Ctrl / Shift 点选多个标签，再点图标，一次全部保存。
   - 保存一片链接：在课程目录、合集列表等页面拖选一段内容，右键“用 Openmd 保存选中的链接”。
   - 右键“用 Openmd 保存此页 / 此链接”同样可用。
   - X 帖子带视频：直接点图标只注明有视频；在 X 页面（或帖子链接、视频上）右键“用 Openmd 保存（含视频文字版）”附上逐字稿，“（含视频文字版和视频文件）”再留下视频。
4. 设置页（右键图标 → 选项）：添加多个保存位置（名字 + 文件夹，可用“选择…”弹出文件夹对话框），选默认位置；“点击图标时”可切换为“先选择保存位置”；“帖子里带视频时”可改成默认附上逐字稿。

借用浏览器读取（B 站、公众号、X 等）时，Chrome 可能在顶部显示“Openmd 已开始调试此浏览器”：这是 Chrome 对使用调试接口的插件统一显示的提示，插件自身无法隐藏；读取只在 Openmd 的后台窗口里进行。

插件通过 Chrome Native Messaging 调用本机 `openmd`，不开本地端口；只有上面这个插件 ID 能连。所有保存进入后台队列，一次只跑一个（避免本机转录同时占满显卡），后台不弹任何窗口。关机时没跑完的任务，开机后插件一联系本机就自动重新排队（最多重试一次）。重启电脑后无需任何操作。任务记录在 `~/.openmd/browser-jobs/`。

ASR 步骤自动选择已有 CUDA worker；主 CLI 保留自己的环境。本地模型目录或缓存不存在时明确报错，不自动从 HF 下载。工具位置、ASR Python 和模型根目录统一放在本机配置（`OPENMD_CONFIG` 指定 JSON，源码 checkout 默认 `out/local-config.json`，安装版默认 `~/.openmd/local.json`）；只存安装位置，不存凭证。配置示例与安装边界见 [工具接入规范](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)，实际路由由打包 JSON 生成：[当前路由](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md)。架构图和各平台工具一览：[Openmd 全景](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html)（浏览器打开）。

## 输出格式

```md
---
title: ...
author: ...
url: ...
published: "2026-11-07"
platform: bilibili
---

# 标题

**00:00:01**

合并成段的原始逐字稿……

*来源：自动字幕；识别错误保持原样。*
```

### 状态口径

- `verified`：已成功产出可用正文 / 逐字稿，包括原生正文、官方/作者字幕、自动字幕、第三方 transcript、ASR 转写、飞书原生 Markdown 导出。
- `partial`：产出文本但明显不完整，例如只抓到预览、部分章节缺失、视频只转写一段、collection 部分失败。
- `failed`：没有产出可用文字稿 / 正文。

`source_level` 表示来源等级，不等同于 `status`。这些字段保留在运行记录和显式 package 中，不在普通 Markdown 中重复展开。

## 文档

- [子系统地图](https://github.com/baigao417/openmd/blob/main/docs/subsystems/README.md)：每个模块的职责、入口和验证命令
- [接入新平台](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md)：大多数平台只需加一张工具卡
- [工具接入规范](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)、[当前路由](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md)、[全景图](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html)

## 参与开发

Windows / macOS / Linux 都可以开发：`python scripts/dev_setup.py` 一条命令装好环境。流程、issue 与 PR 要求见 [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md)，硬规则见 [code-standards](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md)。

## 许可证

[MIT](https://github.com/baigao417/openmd/blob/main/LICENSE)。第三方组件见 [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md)。
