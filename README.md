# Openmd

Openmd is a Markdown-first content ingestion CLI. It routes supported URLs and local files through explicit backend capabilities, preserves source text, and produces resumable Markdown artifacts without summarizing the input.

## 解决什么问题

网页、视频字幕、课程目录和本地文件的获取方式不同。Openmd 将输入路由、后端选择、失败状态和 Markdown 输出统一起来，让下游工具读取原始正文或逐字稿，而不是把抓取元数据误当作内容。

本仓库是无历史、无账号数据的公开源码快照，作者为 `baigao417`。默认不联网、不调用付费服务、不自动登录。平台代码存在不等于平台当前可用。

## 核心机制

```text
URL / URL list / collection / local file
  -> detector + platform adapter
  -> capability registry + explicit execution gates
  -> discovery (manifest) / extraction (backend chain)
  -> native body or captions
  -> optional temporary media + Whisper / OCR fallback
  -> raw Markdown or .openmd package + provenance sidecars
  -> sanitized runlog + coverage + validated resume/skip
```

- `ingest` 处理单项，`batch` 处理 URL 列表，`collect` 先发现再提取；`--manifest-only` 不提取正文。
- 后端能力注册表区分网络、浏览器、媒体下载、ASR 和云服务许可，默认使用安全占位路径。
- 包模式保留来源、组件状态、提取合同与校验信息；复跑只跳过符合合同的已有产物。
- `verified`、`partial`、`failed` 和来源等级分别记录，不以发现条数或元数据充当逐字稿覆盖率。
- 保留抖音、小红书的项目自有 OpenCLI 插件，以及已在项目内的小鹅通适配器。未打包上游 OpenCLI 源码。

## 快速开始

需要 Python 3.10 或更高版本。基础 CLI 只依赖标准库。

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# POSIX: source .venv/bin/activate
python -m pip install -e '.[dev]'
openmd --help
openmd capabilities
openmd doctor
openmd ingest https://example.com/synthetic-article --backend mock --out out/demo
python -m pytest -q -p no:cacheprovider
```

Windows 无需安装即可运行仓库入口：

```powershell
.\scripts\openmd.ps1 --help
.\scripts\openmd.ps1 ingest https://example.com/synthetic-article --backend mock --package --out out/demo
```

`mock` 只生成合成文字，不验证平台。`docs/fixtures/` 和 `tests/fixtures/` 均为合成契约数据，不含真实抓取产物。实际内容、日志和包应保存到被忽略的 `out/` 或仓库之外。

### 可选后端

```bash
python -m pip install -e '.[asr]'       # local faster-whisper
python -m pip install -e '.[ocr]'       # local RapidOCR + Pillow
python -m pip install -e '.[document]'  # local PDF / Office
python -m pip install -e '.[web]'       # article extraction
python -m pip install -e '.[youtube]'   # native transcript API
```

媒体转换还需要用户自行安装的 FFmpeg；部分媒体下载路径需要 yt-dlp。Whisper 模型可能首次下载且耗时较长，离线使用时请预先准备模型。CLI 不托管模型或这些外部工具。

### 浏览器适配器

用户自行安装 OpenCLI，并在其受控浏览器中提供已获授权的会话。Openmd 不附带账号、cookie、浏览器 profile 或登录脚本；抖音、小红书、小鹅通等适配器复用外部工具的会话，不导出会话数据。

两个插件需要兼容的 OpenCLI 主机和 Node.js。只在需要使用插件时，在对应目录运行 `npm install`、`npm run build`，然后按外部 OpenCLI 的插件安装流程注册本地目录。`scripts/setup-openmd-*-plugin.ps1` 会注册或更新插件，应由用户明确执行；普通 Python smoke 不执行这些脚本。插件离线测试使用 `npm test`；TypeScript 测试需要支持直接运行 TypeScript 的 Node.js 版本。

授权平台输入的示例：

```powershell
Get-Clipboard | openmd ingest - --enable-browser-backends --package --out out/authorized
openmd collect '<authorized-creator-url>' --limit 20 --enable-browser-backends --manifest-only --out out/discovery
```

签名链接优先经 stdin 提供，不将会话参数写进命令历史。媒体下载及 ASR 必须分别显式启用 `--enable-media-download`、`--enable-asr`。`--all` 仅对实现了全量合同的 discovery lane 开放，其他路径提前拒绝；不保证所有平台作者页都支持全量。

## 配置说明

`.env.example` 只有空白和非敏感默认项，CLI **不自动加载 `.env`**。请在运行环境设置变量，不提交本地配置。

| 配置 | 用途 |
| --- | --- |
| `OPENMD_PYTHON` | Windows wrapper 的 Python 可执行文件；默认优先 `.venv` |
| `OPENMD_BILIBILI_COOKIE_FILE` | 用户在仓库外自行提供的 UTF-8 Cookie header 文本文件，仅请求时读取；不在错误中回显内容 |
| `OPENMD_BILIBILI_COOKIE_HEADER` | 兼容已有环境变量输入，不建议将值写入命令历史 |
| `OPENMD_YOUTUBE_API_KEY` | 可选 YouTube Data API；仅显式启用网络 discovery 时需要 |
| `OPENMD_WECHAT_ARTICLE_EXPORTER_BASE_URL` | 用户自行运行的本地 sidecar 地址 |
| `OPENMD_WECHAT_ARTICLE_EXPORTER_AUTH_KEY` | 外部 sidecar 的认证输入；不附带配置或服务 |
| `OPENMD_FASTER_WHISPER_MODEL` | 默认 `large-v3`，可设本地模型路径或其他模型名 |
| `OPENMD_FASTER_WHISPER_DEVICE` / `OPENMD_FASTER_WHISPER_COMPUTE_TYPE` | 默认 `cpu` / `int8` |
| `OPENMD_FASTER_WHISPER_PROFILE` | 默认 `general_v1`；`zh_podcast_v2` 是可选配置，不代表质量保证 |

Cookie 文件应只包含单行 header 值，不是浏览器数据库、JSON 或 Netscape cookie 导出文件。没有提供 Cookie 时保留无 Cookie 路线；对不属于 Bilibili 的目标不会读取或发送该 Cookie。优先使用用户自己的浏览器会话，避免另行导出凭证。

## 已知限制

- 本公开快照只做离线测试与 mock 冒烟；未重新登录平台、执行真实抓取或调用云 API。
- 平台页面、接口、字幕可用性和反爬规则可能变化；授权会话也不保证成功。验证码、导航拒绝与权限限制应停止并报告，不绕过。
- 历史项目验收曾记录一次抖音作者发现 678 条、677 条成功；这里只保留该文字概述，不附原文、作者身份或验收包，也不作为当前快照实测结果。
- ASR/OCR 结果需人工核对；多栏阅读顺序可能标为 `partial`，原生文本提取不证明公式、表格或版面保真。
- TikTok 和 QQ 群等能力位置可能为暂停或占位，不应从注册项推断完整实现。
- 云服务代码保留显式许可边界；本轮未调用，用户使用前应自行确认费用、隐私和供应商条款。
- Windows 上的锁与强杀恢复测试不能证明断电、网络文件系统或其他操作系统上的恢复可靠性。
- 输出仍可能包含用户授权源中的私人内容；脱敏的源码仓库不意味着运行结果适合公开。

## 免责声明

仅用于个人学习和已获授权内容。请遵守各平台服务条款、版权、访问权限及适用法律，不用于绕过登录、付费墙、验证码或访问控制，不批量传播第三方正文、逐字稿、课程或个人数据。提供浏览器会话或 Cookie 不代表拥有内容再发布权。

软件按现状提供，无可用性、准确性或平台兼容性保证。第三方运行依赖由用户自行安装，许可与分发注意事项见 `THIRD_PARTY_NOTICES.md`。本项目源码使用 MIT 许可证。
