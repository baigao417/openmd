# Changelog

## [Unreleased]
### 新增 / Added
- 本地 ASR 模型准备命令 `openmd asr prepare-models`：流式生成并逐张量校验独立小分片副本，保留原模型；Qwen3 优先加载有效副本，并记录加载来源。 / Stream and verify separate small-shard local models, preserve originals, and prefer verified prepared copies with origin receipts.
- Qwen3 与自动语言探测在可用物理内存低于预计峰值时明确拒绝加载；可配置余量仅产生内存偏紧警告并记入回执，自动路由不在拒绝后加载另一套本地模型。 / Qwen3 and automatic language detection refuse loading only below the estimated peak; configurable headroom produces advisory receipt warnings, with no local fallback after refusal.
- 小鹅通长课程可通过本机配置明确授权听悟云转录，默认阈值 10 分钟；公开默认仍为本地 ASR，回执记录实测时长与选择理由。 / Explicit local opt-in for Tingwu cloud ASR on long Xiaoe lessons, with a configurable ten-minute threshold and selection receipts; public defaults remain local.
### 优化 / Improved
- 托管 OpenCLI 安装根目录可通过本机 `tools.opencli.root` 配置，便于将工具放到独立运行时磁盘，默认路径保持兼容。 / Configure the managed OpenCLI root independently of preferences and browser sessions.
- 有界预取有限 HLS 的 MPEG-TS 分片，保留加密与顺序语义；签名地址和内容密钥不落盘，复杂播放列表继续使用原 FFmpeg 传输。 / Bounded prefetch of finite MPEG-TS HLS preserves encryption and ordering, keeps signed locators and content keys off disk, and retains FFmpeg transport for complex playlists.

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
