# Openmd routes

Generated from bundled cards and routes, version `2026-11-10.1`.

| Source | Capability | Ordered backends |
|---|---|---|
| bilibili | transcript | opencli.bilibili.subtitle → bilibili-harvest.core → yt-dlp.bilibili → asr.fallback |
| bilibili | media | yutto.bilibili.media-asr |
| youtube | transcript | opencli.youtube.transcript → youtube-transcript-api → yt-dlp.youtube → asr.fallback |
| weixin | content | opencli.weixin.download → defuddle.web.content → weixin.article.local → opencli.web.read |
| qq_group | content | qq_group.export |
| xiaoyuzhou | transcript | opencli.xiaoyuzhou.transcript |
| xiaoyuzhou | media | xyz-dl.xiaoyuzhou.media-asr |
| douyin | content | douyin.transcript |
| tiktok | content | opencli.tiktok.user |
| xiaohongshu | content | xiaohongshu.composite |
| xiaoe | transcript | opencli.xiaoe.transcript → opencli.xiaoe.content |
| x | content | x-tweet-fetcher.x.content → opencli.twitter.thread |
| web_article | content | web_article.discourse → defuddle.web.content → web_article.trafilatura → opencli.web.read |
| feishu | content | lark-cli.docs.fetch → lark-cli.drive.export-markdown → lark-cli.markdown.fetch → manual.markdown-export |
| generic_media | transcript | asr.fallback |
| pdf | content | pdf.markitdown → pdf.textlayer → ocr.chain → document.engines |
| screenshot | content | ocr.chain → ocr.rapidocr → ocr.tesseract |
| page_snapshot | content | page_snapshot.defuddle |
| ai_chat | content | opencli.chatgpt.detail → opencli.deepseek.detail → opencli.claude.detail → opencli.doubao.detail → opencli.gemini.detail → opencli.qwen.detail → opencli.yuanbao.detail → opencli.grok.detail |

Tools run only with the gates declared in their cards. Media steps require local ASR before producing text.

Local overlay applied: false.
