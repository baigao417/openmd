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

小鹅通长课听悟的本机 `xiaoe_long_asr` 上传授权、撤销方式、时长判断及回执字段见
[子系统地图](subsystems/README.md#小鹅通长课程的显式听悟授权)。公开默认不启用云服务。

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


## Managed OpenCLI configuration and lifecycle

Openmd always runs its pinned OpenCLI. Other PATH installations are ignored.
The optional `tools.opencli` location has a required `root` and optional
`config_dir` (nonempty strings). Without `config_dir`, configuration lives in
`<Openmd data>/opencli`. `openmd doctor --json` reports the executable, installed
version, selected config directory, plugin names and an ignored PATH install.

`openmd opencli migrate` lists only relative filenames and sizes from the legacy
`.opencli` directory. `--apply --to <directory>` copies files with timestamps,
retains source files, preserves existing destination files as reported conflicts,
and writes `openmd-migration-receipt.json`. No credential contents are printed.
Xiaoyuzhou checks the selected directory first, then the read-only legacy file.

OpenCLI 2.0.3 uses `OPENCLI_CONFIG_DIR` for profiles but uses the home directory
for plugin discovery and `plugins.lock.json`. Openmd gives only its child
process a private home whose `.opencli` link resolves to `config_dir`.
The controller's home and global PATH are unchanged. Packaged read adapters are
installed under `openmd-owned/xiaoe` and `plugins/openmd-*`; Node's built-in TypeScript
stripper produces the JavaScript needed by 2.0.3 plugin discovery. Existing
modified adapter files are rejected rather than overwritten.

`openmd opencli list` and `openmd opencli <site> <command> [args]` preserve stdout,
stderr and exit status. Registry metadata must say `access: read`; writes and
unknown commands require an explicit `--allow-write` (before or after the site).
A foreign daemon on port 19825 fails with `opencli_daemon_foreign`; Openmd never
stops it. Unknown daemon identity also fails closed.

Browser options in local configuration:

```json
{"browser": {"idle_timeout_seconds": 120, "window_strategy": "offscreen-reuse"}}
```

`window_strategy` accepts `offscreen-reuse` (offscreen creation plus container
reuse), `reuse` (ordinary background creation plus reuse), and `existing-window`
(inactive owned tabs in a collapsed group within an existing ordinary window).
Override with `OPENMD_BROWSER_WINDOW_STRATEGY` and `OPENMD_BROWSER_IDLE_SECONDS`.
Owned pages start blank, are muted, and then navigate. `OPENMD_BROWSER_SOUND=1`
enables sound for newly created owned tabs; `OPENMD_SHOW_BROWSER=1` enables the
visible debug window. Borrowed user tabs retain their mute and ownership state.
`OPENCLI_CLOSE_OWNED_TAB` defaults to `1` and remains explicitly overridable.
Browser adapters identified by the pinned command manifest default to ephemeral
sessions; API commands retain their arguments and do not prepare a browser daemon.
Unknown third-party command metadata does not receive guessed browser options.
Multi-step jobs close in `finally`.
Only owned, inactive leases in the `openmd` profile are eligible for orphan
cleanup. Cleanup receipts go to `<Openmd data>/browser-cleanup.jsonl`, separately
from item runlogs; `joint_reload_required` means the currently loaded bridge does
not implement the new protocol, not successful cleanup.

Bridge changes require extension reload. Generate with
`python scripts/vendor_opencli_bridge.py` and verify with `--check`; never edit
the vendor JavaScript. Windows diagnostics are available via
`scripts/diag_window_events.py --output evidence.json -- <command>`.
No window/sound availability claim is made until real extension acceptance.

## Agent receipt contract

For non-interactive use, prefer:

```bash
openmd "<url-or-path>" -o <directory> --json
```

Read the saved artifact together with the top-level `status`, `source_level`, `completeness`, `sync`, `login`, `next_action`, and `error_code` fields. A successful process exit only says that the command ended; it does not certify the body. `status=partial` must remain partial when a source or member is missing.

`openmd opencli list` and `openmd opencli <site> <command> [args]` are read-only pass-throughs to the pinned managed OpenCLI. The controller permits built-in read-only commands and rejects writes, unknown commands, and unknown-site help unless the caller explicitly supplies `--allow-write`. The exact write gate and privacy boundary are documented in [SKILL.md](../src/openmd/SKILL.md).
