# 工具与运行时接入规范

运行时读取 `src/openmd/routing/routes.json` 和 `cards/*.json`。默认链、能力投影、`openmd routes` 与 `docs/routes.generated.md` 由这些声明生成；历史 benchmark 文档只记录当时的证据，不再充当运行时路由来源。

当前通用执行器支持原生命令、Python entry point 与 Python script。HTTP/MCP/复杂浏览器操作继续复用现有 builtin provider 合同，尚未改造成纯配置驱动。工具卡声明身份、版本、许可证、安装入口 SHA256、输入来源与 URL 约束、argv、产物读取方式、source_level、校验、权限、超时与真实证据；退出码 0 仅表示进程结束，正文必须另行核验。媒体卡的 `then: asr.auto` 表示下载音频后必须得到真实逐字稿才可成功。

## 本机安装位置

`OPENMD_CONFIG` → checkout 的 `out/local-config.json` → `~/.openmd/local.json`，只选第一份存在的配置。显式配置不存在或格式错误会直接报错。示例路径需替换为本机实际目录：

```json
{
  "schema": "openmd.local/v1",
  "asr_python": "E:/tools/asr/venv/Scripts/python.exe",
  "model_roots": ["E:/models"],
  "tools": {
    "defuddle": {"node": "node", "root": "E:/tools/node_modules"},
    "yutto": {"python": "E:/tools/yutto/venv/Scripts/python.exe", "paths": ["E:/tools/yutto/site-packages"]},
    "xyz-dl": {"python": "E:/tools/xyz/venv/Scripts/python.exe", "script_root": "E:/tools/xyz", "paths": ["E:/tools/xyz/site-packages"]}
  }
}
```

不自动安装、不修改现有环境，不自动下载模型。ASR worker 需要 CUDA 版 torch、ctranslate2、faster-whisper、qwen_asr、funasr、soundfile、opencc 和本地 Qwen3/VAD/Whisper 模型；`openmd doctor --profile media` 检查实际 GPU 可见性与模型文件。工具依赖分别隔离，`doctor --profile tools` 核对入口哈希与安装版本，不使用账号状态。Python 工具必须配置隔离解释器及依赖目录；不直接使用 which 找到的任意 Python launcher。默认目录、环境变量与缓存都由本地 resolver 查找。

## 接入与复测

1. 检查仓库、维护状态、许可证和是否涉及账号操作，记录在卡片及证据中。
2. 在隔离目录安装并固定版本/入口 SHA256；杀毒软件拦截时停止，不绕过。
3. 声明来源、能力、产物、门禁和失败条件。argv 使用参数数组；Windows `.cmd/.bat` 不允许承载链接，Node 用原生 node 解释器执行 JS。工具用隔离 HOME/cwd，不沿用下载器保存的账号；Python shim 强制 requests TLS 校验。
4. 显式打开所需门禁，对固定真实样本运行，读取实际产物。有参考文本才评质量，无参考只记可用性。
5. 通过真实验证后更新卡片与路由；生成文档，添加脱敏回归样例。入口文件变动必须重新核验后更新锁定值。

```powershell
python -m openmd.routing.view --bundled --format markdown > docs/routes.generated.md
openmd routes bench --source web_article --capability content --sample web-python-classes --candidates defuddle.web.content --enable-network-backends
openmd routes demote defuddle.web.content --source web_article --ledger <trial-ledger.jsonl> --evidence <id1> --evidence <id2>
openmd routes restore defuddle.web.content --source web_article
```

`routes bench` 每次只跑一个固定样本；可以用 `--sample` 选择。质量/速度权重为 80%/20%，只比较同一来源、能力、样本和参考。当前样本注册表是种子集，多数格没有独立参考，不代表已经建好每格 2–3 个金标准样本。两次同版本、同样本的可复核工具失败才允许本地降级；缺依赖、缺权限与缺模型不算工具失效。覆盖层写 append-only 事件，运行回执保留 `route_trace`；恢复追加事件，不改打包路由。当前需要显式执行降级，不自动调整；建议首选每周、备用每月复测，本轮未创建任何调度任务。

已真实验证四个工具的原生调用及普通 CLI 中英文 ASR；另由日常 `openmd go` 完成一个 291 秒 B 站视频的匿名 yutto 下载→本地 ASR→包/回执保存。xyz 长节目仍只验证下载，未做整集转录。单样本执行成功不证明独立准确率或所有节目覆盖；平台广度、长音频质量和登录内容仍需分别验收。
