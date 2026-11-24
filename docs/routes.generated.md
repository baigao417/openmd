# Openmd routes

Generated from bundled cards and routes, version `2026-11-10.1`.

| Source | Capability | Ordered backends | Last verified |
|---|---|---|---|
| bilibili | transcript | opencli.bilibili.subtitle → bilibili-harvest.core → yt-dlp.bilibili → asr.fallback | 2026-11-18 |
| youtube | transcript | opencli.youtube.transcript → youtube-transcript-api → yt-dlp.youtube → asr.fallback | 2026-11-18 |
| weixin | content | weixin.article.local → defuddle.web.content → opencli.weixin.download → opencli.web.read | 2026-11-18 |
| qq_group | content | qq_group.export | — |
| xiaoyuzhou | transcript | opencli.xiaoyuzhou.transcript | 2026-11-18 |
| douyin | content | douyin.transcript | 2026-11-18 |
| tiktok | content | opencli.tiktok.user | — |
| xiaohongshu | content | xiaohongshu.composite | — |
| xiaoe | transcript | opencli.xiaoe.transcript → opencli.xiaoe.content | 2026-11-18 |
| x | content | openmd.x.public → openmd.x.logged_in_browser → opencli.twitter.thread → browser.kaze.snapshot | 2026-11-18 |
| web_article | content | web_article.discourse → defuddle.web.content → web_article.trafilatura → opencli.web.read → browser.kaze.snapshot | 2026-11-18 |
| feishu | content | lark-cli.docs.fetch → lark-cli.drive.export-markdown → lark-cli.markdown.fetch → manual.markdown-export | — |
| generic_media | transcript | asr.fallback | — |
| pdf | content | pdf.markitdown → pdf.textlayer → ocr.chain → document.engines | — |
| screenshot | content | ocr.chain → ocr.rapidocr → ocr.tesseract | — |
| page_snapshot | content | page_snapshot.defuddle | — |
| ai_chat | content | opencli.chatgpt.detail → opencli.deepseek.detail → opencli.claude.detail → opencli.doubao.detail → opencli.gemini.detail → opencli.qwen.detail → opencli.yuanbao.detail → opencli.grok.detail | — |
| zhihu | content | opencli.zhihu.read → browser.kaze.snapshot | — |

Tools run only with the gates declared in their cards. Media steps require local ASR before producing text.

Local overlay applied: false.
