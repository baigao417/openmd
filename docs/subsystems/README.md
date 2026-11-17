# 子系统地图

每个子系统写清：职责、入口文件、失败时的行为、改它之后跑什么验证。改哪个子系统，先读这一节。

主链路与模块所有权见[架构与接手入口](../architecture.md)。此处只维护各子系统的故障与验证入口。

## 1. 日常入口 `openmd <链接>`
- 职责：一个链接 → 一份 Markdown，落到用户选的目录；失败时给人话原因和可提 issue 的一行。
- 入口：`src/openmd/interactive.py`（`cmd_go`、`_authorize`、`_failure_hint`、`issue_context`），参数在 `src/openmd/cli_parser.py`，单项流水线在 `cli_items.py`。
- 合同：给了目录 / 在终端交互 → 自动开日常权限（CS-3）；云服务永不默认开启。
- 验证：`python -m pytest tests/test_interactive_save.py tests/test_walkthrough_fixes.py tests/test_open_source_ux.py`

## 2. 平台识别
- 入口：`src/openmd/core/detector.py`（`detect_platform`）、`src/openmd/core/input_validation.py`（输入与网络预检）。
- 失败行为：识别不了的链接按通用网页处理。

## 3. 路由与工具卡（扩展面）
- 职责：声明每个平台按什么顺序试哪些读取路线；外部工具通过工具卡接入。
- 声明源：`src/openmd/routing/declarations.json`；`python -m openmd.routing.generate` 生成 routes/cards/docs。加载与执行：`loader.py`（`validate_catalog` 硬校验）、`runner.py`、`readers.py`。
- 指南：[adding-a-platform.md](../adding-a-platform.md)。查看当前路由：`openmd routes --format markdown`。
- 验证：`python -m pytest tests/test_tool_registry.py tests/test_route_lifecycle.py`

## 4. 执行器与 fallback
- 入口：`src/openmd/backends/executor.py`（`execute`）。
- 合同：只有上一条返回结构化失败才换下一条；全部失败时，关于内容本身的结论优先于"工具未安装"，每次尝试记入 `fallback_attempts`（进 runlog 的 `route_trace`）。

## 5. 内容校验
- 职责：在保存前拒收错误页、验证页、导航空壳、只有元数据的结果、平台状态页。
- 入口：`src/openmd/core/content_validation.py`（`validate_extraction_content`、`browser_error_page_reason`、`weixin_error_page_reason`、`navigation_shell_reason`）。
- 合同：CS-2、CS-8。新增平台状态页识别必须配一个"引用了该短语的正常文章不被误杀"的测试。

## 6. 平台 provider
- 入口：`src/openmd/backends/<platform>.py`、`src/openmd/adapters/<platform>/`。
- 合同：说明 `status` 与 `source_level` 口径；合集 discovery 与逐条 extraction 分离（CS-5）。

## 7. 渲染与产物
- 入口：`src/openmd/core/renderer.py`、`output.py`、`package_writer.py`、`runlog.py`。
- 合同：CS-1 原文优先；front matter 字段变更属于核心合同变更，先开 issue。

## 8. 本机运行时（转录 / OCR）
- 入口：`src/openmd/runtime/asr.py`、`asr_worker.py`、`src/openmd/ocr/`；体检 `src/openmd/core/doctor.py`，人话视图 `src/openmd/doctor_view.py`。
- 合同：模型缺失时明确提示，不自动从网络下载模型。

托管 OpenCLI 可通过本机位置配置 `tools.opencli.root` 放到其他目录。
该值指向包含 `node_modules` 的安装根目录；未配置时仍使用
`<data>/tools/opencli`。偏好文件和浏览器会话位置保持各自的数据目录。
移动已有安装后须校验文件并运行 doctor；本配置不会自动迁移、删除或更新工具。
模型副本的位置另由下面的 `asr_models.prepared_root` 配置，不依赖工具根目录。

### 本地 Qwen3 模型准备与内存守门

`openmd asr prepare-models` 只处理已安装模型，默认将大于 512 MiB 的 safetensors
权重流式写成独立副本，目录为 `~/.openmd/models/prepared/<label>-<source-id>-v1/`。
每个文件大小包含 header；名称、shape、dtype 和逐张量 SHA256 校验完成后才原子发布。
重跑已验证副本不重写；失败、中断或失效副本不会被加载，也不会覆盖现有目录。
原模型始终保留。单张量无法在标准 safetensors 索引中拆成多个文件；如果某张量
超过上限，命令会失败并提示所需最小值。部分 Qwen3-ASR-1.7B 权重含约 593.5 MiB
的单张量，此时可用 `openmd asr prepare-models --max-shard-mb 640`。

可用 `--model <已有目录>` 指定来源，`--prepared-root <目录>` 指定本次输出根目录。
持续加载自定义位置须同时配置 `asr_models.prepared_root` 或环境变量
`OPENMD_PREPARED_MODEL_ROOT`；准备命令不会自动改写配置。配置示例：

```json
{
  "schema": "openmd.local/v1",
  "asr_models": {"prepared_root": "~/.openmd/models/prepared"},
  "asr_memory": {"original_peak_mb": 5632, "prepared_peak_mb": 2304, "margin_mb": 1024}
}
```

prepared 副本须通过 receipt、来源文件指纹、tensor 元数据和字节哈希检查才可用于
实际转录。doctor 的只读提示只检查结构和文件指纹；失效或缺失时沿用原模型，
doctor / runlog 提示准备命令，不自动写入多 GB 副本。
runlog 的 `asr_execution.model_origin` 为 `prepared` / `original`，并提供
`prepare_recommended` 与内存门槛数值；不输出模型绝对路径。

Qwen3 在导入推理依赖前比较可用物理内存与预计峰值，只有低于峰值才拒绝。
默认 prepared 峰值至少 2304 MiB、原模型至少 5632 MiB（均随最大分片增加）。
余量默认 1024 MiB，仅在可用内存低于峰值加余量时给出 `asr_memory_tight` 警告，
不会阻断；runlog 保留硬门槛 `memory_required_bytes`、警告门槛
`memory_warning_threshold_bytes` 与警告代码。估计来源与实测回执见
[本地 ASR 内存测量](../benchmarks/asr/local-memory.md)；不能保证所有设备/驱动/音频均不超限。
`asr_insufficient_memory` 表示应关闭其他程序或明确选择云端；不会自动上传音频，
`asr.auto` 遇到该错误不会再回退加载 Whisper。可用上面的本机配置，或
`OPENMD_ASR_ORIGINAL_PEAK_MB` / `OPENMD_ASR_PREPARED_PEAK_MB` /
`OPENMD_ASR_MARGIN_MB` 覆盖估计与余量（正整数 MiB）。Linux / Windows 使用本机
系统接口测可用内存；其他系统需要 ASR extra 中的 psutil，探测失败也拒绝加载。

`asr.auto` 用 Whisper `large-v3-turbo` 做语言识别，预加载也先守门：默认预计峰值
至少 2496 MiB，同一余量仅警告，
并随该语言模型二进制大小增加；可由 `asr_memory.language_probe_peak_mb` 或
`OPENMD_ASR_LANGUAGE_PROBE_PEAK_MB` 调整。语言识别阶段的内存拒绝同样是终止错误；
它的数值及警告独立保存在 runlog `asr_execution.language_probe_memory`，不会被后续 Qwen3 数值覆盖。

入口：`runtime/prepared_models.py`、`runtime/asr_memory.py`；回归：
`python -m pytest tests/test_prepared_models.py tests/test_runtime_models.py tests/test_asr_execution_metrics.py`。

### 小鹅通长课程的显式听悟授权

公开默认仍使用本地 ASR。用户可以在当前有效的本机位置配置中加入一个键
（通常为 `~/.openmd/local.json`；开发 checkout 的 `out/local-config.json` 优先）：

```json
"xiaoe_long_asr": {"enabled": true, "threshold_seconds": 600}
```

这是一项持续的 **cloud 上传授权**，只适用于小鹅通已由 core 下载的音频。
设置 `enabled: false` 或移除该键即可撤销；单次 `--enable-cloud-backends` 也是明确授权。
音频经 ffprobe 实测时长达到阈值才选择 `asr.tingwu`；短音频、未知时长或未授权时使用本地。
目录时长不参与选择，因为已删除的长课可能实际只返回 5 秒提示音。
runlog 的 `asr_execution.asr_selection` 给出后端、理由、实测时长与阈值；
compact package 的配置指纹包含该授权设置和单次 cloud gate。

入口：`runtime/tingwu.py`、打包的 `runtime/tingwu_bridge.mjs`；经 pinned OpenCLI
Browser Bridge 会话上传、查询唯一上传名的记录、导出原文 API 段落，并核对至少 98% 音频覆盖。
不读取或保存凭证，不启用旧转录插件；失败返回明确错误，后端内部不回退到其他云或本地 ASR。
需要浏览器已登录听悟；登录缺失或 Bridge 不可达会失败，不代表其他平台不可达。

Xiaoe 媒体下载的简单有限 MPEG-TS HLS 使用最多 4 个网络请求并发预取，然后串行抽音频；
ASR 在下载完成后开始。AES-128 内容密钥仅在内存，core 生成的离线播放列表经 stdin
传给禁用网络协议的 FFmpeg。复杂 HLS（变体、fMP4、byte range、discontinuity 等）保留原 FFmpeg 路线。
该优化不修改系统代理；原始远端播放列表始终不能调用本机文件协议。

验证：`python -m pytest tests/test_tingwu_asr.py tests/test_runtime_media.py tests/test_xiaoe_media.py tests/test_local_config.py`。

## 9. 浏览器插件
- 入口：`src/openmd/browser_extension/`（MV3 插件）、`browser_setup.py`（注册 Native Messaging、复制插件到 `~/.openmd/browser/extension`）、`browser_host.py`（后台队列）。
- 合同：不开本地端口，只有固定插件 ID 能连；目前仅 Windows。
