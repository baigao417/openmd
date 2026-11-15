# AGENTS.md

给 AI 编码 Agent（Claude Code、Codex、Cursor …）的路由表。这里只告诉你去哪里找真相，规则本身在被引用的文件里。

## 先读
1. [CONTRIBUTING.md](CONTRIBUTING.md)：贡献流程与迭代闭环。
2. [docs/code-standards.md](docs/code-standards.md)：硬规则，PR 逐条对照。
3. [LESSONS.md](LESSONS.md)：最近的返工与误判，避免重犯。

## 改哪里读哪里
| 你要改 | 读 |
|---|---|
| 日常命令 `openmd <链接>`、提示文案、保存行为 | docs/subsystems/README.md §1 |
| 接入新平台 / 新工具 | docs/adding-a-platform.md，§3 |
| fallback、错误归因 | §4 |
| 错误页 / 空壳拒收 | §5 |
| 输出格式、front matter | §7（核心合同，先开 issue） |
| 转录 / OCR | §8 |
| 浏览器插件 | §9 |

## 工作方式
- 修 bug：先写会失败的测试，再修复（CS-7）。
- 宣称可用：真实链接跑真实 CLI，读实际产物；写清没验证什么（CS-6）。
- 交付前：`python -m pytest` 全量通过。
- 遇到新的返工 / 误判：追加到 LESSONS.md；同类 3 个不同日期 → 提议晋升到 code-standards.md。

## 不要做
- 不要把密钥、cookie、token、他人版权正文、本机绝对路径写进仓库（CS-4）。
- 不要让默认输出总结、改写、润色原文（CS-1）。
- 不要默认开启云服务，不要新增向第三方发送用户内容的行为（CS-3）。
