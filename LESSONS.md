# LESSONS

返工、误判、验证不足的记录。每条写清**现象 → 原因 → 规则 → 证据**。

晋升规则：同一类教训在 **3 个不同日期**出现，就晋升为 [docs/code-standards.md](docs/code-standards.md) 里的硬规则，并在这里标注 `→ 已晋升 CS-N`。只出现一两次的留在这里观察。

格式：

```
## YYYY-MM-DD 类别：一句话
- 现象：
- 原因：
- 规则：
- 证据：issue / PR / 测试名
- 同类出现：YYYY-MM-DD, ...
```

---

## 2026-11-17 媒体传输：协议白名单须覆盖现有代理传输
- 现象：最终真实短样本发现媒体成功，FFmpeg 下载因 httpproxy 不在白名单而失败。
- 原因：HTTP/HTTPS 资源可能经调用者已有的 HTTP 代理连接，底层额外需要 httpproxy 协议。
- 规则：允许已有网络传输所需的明确协议，同时保持远端播放列表不能启用 file/concat/data；不输出可能含签名地址的 FFmpeg 原始错误流。
- 证据：tests/test_runtime_media.py::test_media_download_supports_proxy_transport_without_local_protocols；真实样本诊断只保存错误类别与被拒协议。
- 同类出现：2026-11-17

## 2026-11-17 声明生成：生成器与运行视图必须共用渲染边界
- 现象：路由声明生成检查通过，但全量测试发现生成文档与运行时路由视图不同。
- 原因：声明源虽已统一，生成器和运行视图仍分别维护 Markdown 渲染逻辑。
- 规则：声明与运行时共用投影函数；保留生成漂移检查和运行视图相等检查。
- 证据：tests/test_tool_registry.py::test_declarative_default_projection_and_generated_document；tests/test_prevention_contracts.py::test_generated_declarations_match_runtime_projections。
- 同类出现：2026-11-17

## 2026-11-15 验证环境：开发机的全局工具掩盖了"装完即用"的真实状态
- 现象：干净 venv 里网页、B 站、YouTube 都"成功"，但把 PATH 收窄到只剩系统目录后全部失败：成功全靠开发机全局安装的 opencli。
- 原因：venv 只隔离 Python 包，不隔离 PATH 上的 Node 工具、ffmpeg、Ollama 和代理。
- 规则：宣称"装完即用"前，必须在**收窄 PATH + 空用户目录 + 无本机配置**的环境里跑真实链接；`--verbose` 的"工具："一行要记进证据。
- 证据：tests/test_open_source_ux.py；docs/agent-runs/2026-11-15-open-source-ease-of-use/state.md
- 同类出现：2026-11-15

## 2026-11-15 错误归因：只报最后一条路线的错误，掩盖了真正原因
- 现象：无效公众号链接失败时提示"opencli 找不到"（最后一条路线），真正的原因"公众号返回错误页"被吞掉。
- 原因：fallback 执行器只保留 last_result。
- 规则：全部路线失败时，关于**内容本身的结论**（错误页、验证页、无字幕）优先于"工具未安装"；每次尝试都要记进 `fallback_attempts`。
- 证据：test_source_verdict_outranks_later_missing_tool
- 同类出现：2026-11-15

## 2026-11-15 错误页当正文：站点状态页被保存成"已取得正文"
- 现象：公众号 "Parameter error / 参数错误" 页面被报告成功并落盘。
- 原因：内容校验只认通用浏览器错误页和 Cloudflare 验证页，不认平台自己的状态页。
- 规则：每个平台接入都要提供该平台的"已删除 / 不存在 / 需验证"状态页样例，并有拒收测试。
- 证据：test_weixin_error_pages_are_not_saved_as_content
- 同类出现：2026-11-11（共享挑战页识别，walkthrough-fixes），2026-11-15

## 2026-11-15 测试环境耦合：测试只在维护者机器上全绿
- 现象：主环境 1241 全过；干净 venv（只装 `-e .`）先是 4 个模块导入失败，补依赖后仍有 37 个失败（缺 numpy / PyMuPDF / markdownify / ffmpeg、断言"所有可选工具就绪"）。
- 原因：测试隐式依赖开发机上已装的可选包、PATH 工具和本机配置。
- 规则：CI 必须走贡献者同一条安装路径（`scripts/dev_setup.py`）；测试依赖写进 `[dev]` extra；断言可选工具就绪的测试只能断言"退出码与 ok 一致"。
- 证据：.github/workflows/ci.yml；tests/test_package_contract.py；tests/test_v1_runtime_benchmark.py
- 同类出现：2026-11-15（与"验证环境"同类）

## 2026-11-15 跨平台：Windows 上成立的假设在 Linux / 其他 Python 版本上失效
- 现象：Linux 上 5 个失败：git hook 带 CRLF；单个目录名 90 个汉字超过 255 字节；测试读取 docs/agent-runs 里的证据脚本；`html.parser` 对未闭合标签的处理随 Python 版本变化（3.12.2 报出、3.10/3.14 吞掉）；Windows venv 的 python.exe 是转发器，kill 它不会杀掉真解释器。
- 规则：见 CONTRIBUTING「跨平台约定」；三端 × 两个 Python 版本的 CI 矩阵是最低保障。
- 证据：.gitattributes；tests/test_receipt.py；tests/fixtures/runtime/review_repro_a.py；src/openmd/ocr/tiling.py；tests/test_hardkill_recovery.py
- 同类出现：2026-11-15
