# 接入新平台 / 新工具

Openmd 的策略是**接入已知工具**，不重写抓取器：每个平台可以有多条读取路线，按顺序尝试，前一条失败才换下一条。接一个新工具，多数情况只需要加一张**工具卡**（一个 JSON 文件）再加一行路由，不用改 Python 代码。

## 1. 三种接入方式，从轻到重

| 方式 | 适合 | 改动 |
|---|---|---|
| **工具卡**（首选） | 已有命令行工具能把链接输出成 Markdown / JSON / 字幕 | `src/openmd/routing/cards/<tool>.json` + `routes.json` 一行 |
| 内置 provider | 需要解析 HTML、调平台公开 API、处理分页 | `src/openmd/backends/<platform>.py` + `builtin.json` 声明 |
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

然后在 [routes.json](../src/openmd/routing/routes.json) 对应平台的 `chain` 里加上你的 capability id。**位置就是优先级**：放在前面表示优先尝试，放在后面表示作为兜底。

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
