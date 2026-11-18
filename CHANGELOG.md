# Changelog

## [Unreleased]
### 新增 / Added
- 新增日文、韩文 README，并同步四语安装、能力和限制说明。 / Add Japanese and Korean READMEs with the same installation, capability, and limitation guidance as the existing guides.
- 随包提供 `openmd/SKILL.md`，说明非交互调用、JSON 回执字段、常见错误的下一步与隐私边界。 / Ship `openmd/SKILL.md` with non-interactive invocation, JSON receipt fields, common error next actions, and privacy boundaries.
- 本地文档支持 24 种格式；`[document]` 使用自有胶水层连接成熟解析库，不再依赖 MarkItDown，安装体积约减少 65%。检测到 LibreOffice 或 Calibre 时才启用第二层转换。 / Support 24 local document formats; `[document]` now uses Openmd glue with mature parsers instead of MarkItDown, reducing install size by about 65%. LibreOffice or Calibre is used as a second layer only when detected.
- 保存当前页、登录提醒以及对话、字幕和表格结构块可用；未登录时给出登录链接和下一步。 / Save the current page, show login reminders, and preserve conversation, transcript, and table blocks; unauthenticated routes provide a login link and next action.
- AI 对话导出改为读取已加载的页面：在 ChatGPT、Grok、DeepSeek、豆包、Gemini、千问、元宝的对话页点扩展即保存当前标签，按角色和轮次输出，保留列表、标题与表格；Agent 可用 `openmd go <page.html> --as-snapshot --url <链接>` 交给 Openmd 转换。范围标注为当前可见分支，生成中标为 partial。 / AI chat export now reads the already-loaded page: clicking the extension on ChatGPT, Grok, DeepSeek, Doubao, Gemini, Qwen or Yuanbao saves the current tab as role-ordered turns with lists, headings and tables; agents can hand a captured page to `openmd go <page.html> --as-snapshot --url <link>`. Coverage is the visible branch; in-progress replies stay partial.
- 离线导入 ChatGPT 与 Claude 官方数据导出（`--chatgpt-export` / `--claude-export` 或自动识别），每个会话一份 Markdown，可按标题或时间筛选，与网页导出共用同步身份。 / Import official ChatGPT and Claude data exports offline (`--chatgpt-export` / `--claude-export` or auto-detect), one Markdown file per conversation with title/time filters and the same sync identity as web exports.
- Word、PowerPoint、Excel 中的嵌入图片在原位置做本地 OCR，并标注“来源：OCR”；不可用时保留缺口并标为 partial。 / Embedded images in Word, PowerPoint and Excel are OCR'd locally in place and labelled as OCR; when unavailable the gap is kept and the result is partial.
- `openmd doctor` 读取本机 GPU 与内存给出 OCR/ASR 档位建议（未实测的配置标“未验证”）；`openmd models install <档位>` 只输出模型、大小与目标路径的计划，不自动下载。 / `openmd doctor` recommends OCR/ASR tiers from local GPU and memory (unmeasured setups are marked unverified); `openmd models install <tier>` prints a plan of models, sizes and target paths without downloading.
- 本地聊天导出插件可按游标增量续导，新消息追加到同一文档，无新消息时报告 unchanged；读取聊天记录须显式 `--enable-local-export`。 / Local chat-export plugins resume incrementally from a cursor, appending new messages to the same document and reporting unchanged when nothing is new; reading chat history requires an explicit `--enable-local-export`.
- 可选的 MCP 服务 `openmd mcp`（`pip install "openmd-cli[mcp]"`）：提供保存链接或本地文件、保存 Agent 已加载的页面、查询任务回执、诊断四个工具，复用 CLI 的权限默认值与任务队列，可在限额内直接返回正文。 / Optional MCP server `openmd mcp` (`pip install "openmd-cli[mcp]"`) with four tools — save a link or local file, save a page the agent already loaded, read a job receipt, and run diagnostics — reusing CLI permission defaults and the job queue, and optionally returning bounded body text.
- AI 对话导出默认省略模型的思考过程并在来源说明中计数，`--include-thinking` 或扩展设置可保留为单独小节；公式保留 TeX，过大的内嵌图片以占位符代替并计数；标题去掉站点后缀，通用站名时改用会话标题或首条提问。 / AI chat exports omit model thinking by default and count it in the source note (`--include-thinking` or the extension setting keeps it as a separate section); formulas keep TeX, oversized embedded images become counted placeholders, and titles drop site suffixes, falling back to the conversation title or first prompt.
- 浏览器扩展在 Openmd 升级后自动同步并在空闲时自行重新加载，无需再手动执行 `browser-setup`；宿主不可用时给出明确修复提示。 / The browser extension syncs and reloads itself when idle after an Openmd upgrade, without a manual `browser-setup`; when the host is unusable it shows a clear fix.
- 本地 ASR 模型准备命令 `openmd asr prepare-models`：流式生成并逐张量校验独立小分片副本，保留原模型；Qwen3 优先加载有效副本，并记录加载来源。 / Stream and verify separate small-shard local models, preserve originals, and prefer verified prepared copies with origin receipts.
- Qwen3 与自动语言探测在可用物理内存低于预计峰值时明确拒绝加载；可配置余量仅产生内存偏紧警告并记入回执，自动路由不在拒绝后加载另一套本地模型。 / Qwen3 and automatic language detection refuse loading only below the estimated peak; configurable headroom produces advisory receipt warnings, with no local fallback after refusal.
- 小鹅通长课程可通过本机配置明确授权听悟云转录，默认阈值 10 分钟；公开默认仍为本地 ASR，回执记录实测时长与选择理由。 / Explicit local opt-in for Tingwu cloud ASR on long Xiaoe lessons, with a configurable ten-minute threshold and selection receipts; public defaults remain local.
### 优化 / Improved
- `openmd doctor` 显示托管 OpenCLI 补丁是否全部命中当前版本、各路线最后一次真实验证日期（超过 30 天提示），并检查浏览器扩展的本地宿主与扩展副本是否可用。 / `openmd doctor` reports whether all managed OpenCLI patches match the pinned version, each route's last real verification date (flagging >30 days), and whether the browser host and extension copy are usable.
- Agent 或脚本发起的非交互调用（`--json`、batch）不再改写用户的默认保存目录。 / Non-interactive agent or script calls (`--json`, batch) no longer overwrite the user's default save directory.
- 独立的完整度核对和同步清单保护原地更新、未变化、用户编辑冲突和缺失恢复：`created` / `unchanged` / `conflict_kept_user_edit` / `restored_missing`。 / Independent completeness checks and the sync manifest protect created, unchanged, user-edit conflicts, and missing-file recovery states.
- 浏览器读取默认在现有窗口的不激活后台标签中进行，任务标签默认静音并在完成后关闭；只使用托管 OpenCLI，`openmd opencli` 默认只读透传。 / Browser reads default to inactive background tabs in an existing window, owned tabs start muted and close after completion; managed OpenCLI is used, and `openmd opencli` is read-only by default.
- 回执统一暴露 `status`、`source_level`、`completeness`、`sync`、`login`、`next_action`、`error_code`，便于 Agent 判断是否可用。 / Receipts expose `status`, `source_level`, `completeness`, `sync`, `login`, `next_action`, and `error_code` so agents can judge usability.
- 托管 OpenCLI 安装根目录可通过本机 `tools.opencli.root` 配置，便于将工具放到独立运行时磁盘，默认路径保持兼容；测试默认隔离本机配置、用户目录和运行时，避免改动开发者工具与模型。 / Configure the managed OpenCLI root independently of preferences and browser sessions; tests isolate machine configuration, home and runtime paths to protect developer tools and models.
- 有界预取有限 HLS 的 MPEG-TS 分片，保留加密与顺序语义；签名地址和内容密钥不落盘，复杂播放列表继续使用原 FFmpeg 传输。 / Bounded prefetch of finite MPEG-TS HLS preserves encryption and ordering, keeps signed locators and content keys off disk, and retains FFmpeg transport for complex playlists.
### 修复 / Fixed
- ChatGPT 已加载页面的用户提问不再漏抓；页面存在提问而导出缺失时降级为 partial。各导出路线的角色名统一为小写标准值。 / User prompts on loaded ChatGPT pages are no longer dropped, and a missing prompt downgrades the export to partial; conversation roles are normalized across all export routes.
- 未登录页面和登录墙会被拒收，并返回 `login_required`、登录链接和下一步，不再把登录页保存成正文。 / Reject unauthenticated pages and login walls with `login_required`, a login link, and a next action instead of saving the login page as content.
- 修复托管 OpenCLI 路径隔离、浏览器标签生命周期及部分平台错误映射，使失败原因更明确。 / Fix managed OpenCLI path isolation, browser-tab lifecycle, and selected platform error mappings so failures are clearer.

## [0.1.1]
### 新增 / Added
- 逐字稿按时间戳分段，保留原字词；补充中英文使用指南。 / Timestamped transcript paragraphs preserve source words; Chinese and English usage guides.
### 修复 / Fixed
- 修复 Linux 进程退出时的存活误判，让取消与临时文件回收更可靠。 / Fix Linux process-exit detection for more reliable cancellation and temporary-file cleanup.

## [0.1.0]
### 新增 / Added
- 链接直存 Markdown：网页与 YouTube 字幕开箱可用，Windows 浏览器扩展支持已登录页面。 / Save links as Markdown: web articles and YouTube captions work out of the box; the Windows extension reads signed-in pages.
### 修复 / Fixed
- 拒收错误页、验证页；提取失败时优先说明内容原因。 / Reject error and verification pages; report content-specific failures first.
