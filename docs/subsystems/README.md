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

## 9. 浏览器插件
- 入口：`src/openmd/browser_extension/`（MV3 插件）、`browser_setup.py`（注册 Native Messaging、复制插件到 `~/.openmd/browser/extension`）、`browser_host.py`（后台队列）。
- 合同：不开本地端口，只有固定插件 ID 能连；目前仅 Windows。
