# 接入新平台 / 新工具

主链路与模块所有权见[架构入口](architecture.md)。接入已有工具通常只需在
`src/openmd/routing/declarations.json` 中增加工具卡和路由，再运行
`python -m openmd.routing.generate`，不必改 Python 提取逻辑。

## 1. 三种接入方式，从轻到重

| 方式 | 适合 | 改动 |
|---|---|---|
| **工具卡**（首选） | 已有命令行工具能把链接输出成 Markdown / JSON / 字幕 | declarations 的 `cards` / `routes`，再生成投影 |
| 内置 provider | 需要解析 HTML、调平台公开 API、处理分页 | `src/openmd/backends/<platform>.py` + declarations 的 builtin 能力 |
| 新平台识别 | Openmd 还不认识这个域名 | `src/openmd/core/detector.py` |

## 2. 工具卡长什么样

参考现成的 [defuddle.json](../src/openmd/routing/cards/defuddle.json)（命令行工具）和 [yutto.json](../src/openmd/routing/cards/yutto.json)（Python 工具，下载媒体后本机转录）。

```jsonc
{
  "schema": "openmd.tool-card/v1",
  "id": "mytool",                       // 全局唯一
  "kind": "command",                    // command | python_entry | python_script
  "version": "1.2.3",                   // 你验证时用的版本
  "license": "MIT",                     // 必填：用户要知道自己装的是什么
  "homepage": "https://github.com/...",
  "installation": {"mode": "isolated_existing", "verification": "..."},
  "evidence": [{"date": "YYYY-MM-DD", "ledger": "...", "scope": "2 real samples; ..."}],
  "launcher": {"which": ["mytool"]},    // 在 PATH 上怎么找到它
  "capabilities": [{
    "id": "mytool.someplatform.content",
    "sources": ["web_article"],         // 平台 id，见 detector.py
    "capability": "content",            // content | transcript | media ...
    "argv": ["{url}", "--markdown", "-o", "{workdir}/out.md"],
    "reader": {"type": "markdown_file", "path": "out.md"},
    "source_level": "third_party_extractor",
    "gates": ["network"],               // 需要哪些权限：network / browser / media_download / asr / cloud / account
    "timeout_s": 120,
    "requires_login": false,
    "cost_class": "none",
    "status_policy": "verified_if_valid_else_failed",
    "validation": {"min_chars": 40}
  }]
}
```

可用的 `reader.type`：`markdown_file`、`json_file`（用 `fields` 指定 text / title / author 字段）、`json_stdout`、`subtitle_dir`、`media_dir`（必须接 `"then": "asr.auto"`，并声明 `media_download` 和 `asr` 两个 gate）。

然后在 declarations 的 `routes.routes` 对应平台 `chain` 中添加 capability id，
生成 [routes.json](../src/openmd/routing/routes.json)。**位置就是优先级**。
提交前运行 `python -m openmd.routing.generate --check` 拒绝声明与投影漂移。

加载时会做硬校验（[loader.py](../src/openmd/routing/loader.py) 的 `validate_catalog`）：缺 version、license、evidence，或用了未知 gate、未知 reader，都会直接报错。

## 3. 必须附带的证据

和其他 PR 一样，见 [CONTRIBUTING](../CONTRIBUTING.md) 与 [code-standards](code-standards.md)：

1. **至少 2 个真实样本**（形态尽量不同），贴出 `openmd "<链接>" -o out --verbose` 的输出和产物片段。
2. **一个状态页拒收样例**（CS-8）：这个平台"已删除 / 不存在 / 需要验证"时，Openmd 不能把那个页面存成正文。
3. **`status` / `source_level` 口径说明**：什么时候算 verified，什么时候算 partial。
4. **没验证什么**：例如需要登录的内容、合集、超长视频。
5. `python -m pytest` 全量通过（catalog 校验在 `tests/test_tool_registry.py`、`tests/test_route_lifecycle.py`）。

## 4. 本地试跑而不改仓库

不想先改仓库里的路由，可以用本机路由覆盖（route overlay）试跑：配置方法见 [tool-integration.md](tool-integration.md)。确认可用后再提 PR。

## 5. Independently installed platform packages (contract v1)

Use a separate Python package when a platform needs its own detector, adapter,
providers and routing resources. It must use its own namespace; no files are
copied into `openmd`. Install core and the plugin in the same environment:

```sh
python -m pip install openmd-cli
python -m pip install ./my-platform
openmd routes
```

Declare one entry point in the plugin's `pyproject.toml`:

```toml
[project.entry-points."openmd.platforms"]
sample = "my_platform:plugin"
```

The entry point value is a `openmd.core.plugins.PlatformPlugin` instance:

```python
from openmd.core.plugins import PlatformPlugin

plugin = PlatformPlugin(
    PLATFORM_IDS=("sample_feed",),
    detect_platform=lambda value: "sample_feed" if value.startswith("sample:") else None,
    adapter_modules={"sample_feed": "my_platform.adapter"},
    backend_modules={"sample_feed": ("my_platform.providers",)},
    collection_sources=frozenset({"sample_feed"}),
    collection_bindings=lambda: {"sample_feed": ("sample.content", ("browser",))},
    resource_package="my_platform.resources",
    routes_resource="routes.json",
    card_resources=("cards/tool.json",),
)
```

- `PLATFORM_IDS` are globally unique, including core IDs. A detector returns
  `None` or one declared ID. Ambiguous matches fail. Collection selectors also
  reach planning; no core scheme table change is needed.
- Each adapter module exposes `create_adapter(platform_id, default_backend)`.
  Backend modules expose `register_backend_providers(registry, options)` and/or
  `register_discovery_providers(registry, options)` using existing runtime APIs.
  These callbacks must respect `BackendRuntimeOptions`, including explicit
  browser/network/media/ASR/cloud gates. Missing declared modules are errors;
  discovery never guesses an external module inside the core namespace.
- Collection bindings map every declared collection source to its discovery
  provider ID and gates. The provider ID must be in the plugin cards. This
  describes availability; it neither constructs a provider nor grants permission.
- Package resources use `openmd.plugin-routes/v1` with `routes` and
  `capabilities` arrays matching the built-in route and source metadata shapes.
  Cards use `openmd.tool-card/v1`. Include JSON files as package data.
- Keep one owner for platform IDs/default provider IDs in your plugin (for
  example derive the descriptor and bindings from its route resource). The
  core does not duplicate platform-specific facts.
- Duplicate platform, entry-point, tool-card, backend-capability, route
  `(source, capability)` or source metadata IDs raise an error. Installed
  plugins extend the catalog; they cannot overwrite a bundled ID.
- Descriptors must be cheap and free of network/session/model side effects.
  Installed plugins are trusted executable Python, not a sandbox. Discovery
  is cached per process; restart the CLI after installing/removing a plugin.
- `openmd routes` shows installed resources plus local priority overrides.
  `python -m openmd.routing.view --bundled --format markdown` generates only
  core documentation, regardless of installed plugins. Local route overrides
  remain separate and do not register platforms.

Test core alone, core with fake entry points, and the installed package's own
adapter/provider tests. Keep invalid declarations and duplicate-ID negative
cases. Offline tests do not establish live platform availability; report real
CLI output and read the final artifact with the same gates and installed code.
