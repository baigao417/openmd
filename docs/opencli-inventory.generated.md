# OpenCLI command dependencies

Generated statically from call sites, declarations and owned cli() metadata. No commands are executed.
Only required column names are pinned; raw Markdown/file consumers need no table columns. Planned providers are excluded.

| Site | Command | Arguments (including optional branches) | Positionals | Required columns | Owner |
|---|---|---|---|---|---|
| bilibili | subtitle | -f | 1 | content, from, to, language, track_type | upstream |
| bilibili | user-videos | --limit | 1 | title, url, bvid | upstream |
| browser | chrome | — | 3 | — | upstream |
| browser | close | --session --window | 0 | — | upstream |
| browser | eval | --session --window | 1 | — | upstream |
| browser | open | --session --window | 1 | — | upstream |
| chatgpt | detail | --markdown --stable --timeout --wait -f | 1 | Role, Text | upstream |
| chatgpt | status | -f | 0 | — | upstream |
| deepseek | detail | -f | 1 | Role, Text | upstream |
| deepseek | status | -f | 0 | — | upstream |
| doubao | detail | -f | 1 | — | upstream |
| doubao | status | -f | 0 | — | upstream |
| douyin | detail | --keep-tab --site-session --window -f | 1 | aweme_id, desc, author, duration_s, play_url, images | upstream |
| douyin | user-videos | --limit --with_comments -f | 1 | aweme_id, title | upstream |
| gemini | detail | -f | 1 | — | upstream |
| gemini | status | -f | 0 | — | upstream |
| grok | detail | --markdown -f | 1 | Role, Text | upstream |
| grok | status | -f | 0 | — | upstream |
| openmd-douyin | creator-videos | --page_delay_ms --request_timeout_ms --timeout -f | 1 | index, aweme_id, title, author, published_at, duration, digg_count, aweme_type, image_count, media_kind, play_url, transcript_candidates, page_count, empty_page_count, duplicate_page_count, pagination_retry_count, source_exhausted | owned |
| openmd-xiaohongshu | creator-notes | --scroll_delay_ms --selector_stdin --stable_rounds --timeout --transient_note_urls -f | 1 | index, note_id, title, author, published_at, type, likes, url, page_count, scroll_count, stable_round_count, empty_snapshot_count, duplicate_snapshot_count, anomaly_count, source_exhausted, explicit_exhaustion, exhaustion_reason, store_source, runtime_note_url | owned |
| openmd-xiaohongshu | note-assets | --creator --download --output --selector_stdin --timeout -f | 1 | row_type, note_id, title, author, native_text, tags, media_kind, native_transcript, image_count, video_count, asset_index, asset_type, asset_status, asset_error_code, local_path, initial_state_note_present, detail_key_count, structured_note_count, dom_image_count, scoped_image_count, background_image_count, dom_video_count, candidate_image_count, candidate_video_count | owned |
| qwen | detail | --markdown -f | 1 | — | upstream |
| qwen | status | -f | 0 | — | upstream |
| twitter | article | --keep-tab --site-session --window -f | 1 | title, author, content, url | upstream |
| twitter | thread | --keep-tab --limit --site-session --window -f | 1 | id, author, text, url | upstream |
| twitter | tweets | --keep-tab --limit --site-session --window -f | 1 | id, text, url | upstream |
| web | read | --download-images --keep-tab --site-session --stdout --url --window -f | 0 | — | upstream |
| weixin | download | --download-images --keep-tab --output --site-session --url --window -f | 0 | — | upstream |
| xiaoe | catalog | --keep-tab --site-session --window -f | 1 | ch, chapter, no, title, type, resource_id, url, status | owned |
| xiaoe | content | --keep-tab --site-session --window -f | 1 | title, content, content_length, image_count | owned |
| xiaoe | play-url | --keep-tab --site-session --window -f | 1 | title, resource_id, m3u8_url, candidate_media_urls, duration_sec, method | owned |
| xiaohongshu | note | -f | 1 | field, value | upstream |
| xiaohongshu | user | --limit -f | 1 | id, title, url | upstream |
| xiaoyuzhou | episode | -f | 1 | title | upstream |
| xiaoyuzhou | podcast-episodes | --limit -f | 1 | eid, title | upstream |
| xiaoyuzhou | transcript | --json --output --text -f | 1 | json_file, text_file | upstream |
| youtube | channel | --limit -f | 1 | field, value | upstream |
| youtube | playlist | --limit -f | 1 | title, url | upstream |
| youtube | transcript | --mode -f | 1 | — | upstream |
| yuanbao | detail | -f | 1 | — | upstream |
| yuanbao | status | -f | 0 | — | upstream |

Browser metadata conditionally injects runtime arguments: `--keep-tab` `--site-session`.
Source: `src/openmd/runtime/managed_tools.py:managed_command`; API-only commands do not receive these flags.

## Source references

- `bilibili/subtitle`: `src/openmd/backends/bilibili.py:407`
- `bilibili/user-videos`: `src/openmd/backends/bilibili.py:863`
- `browser/chrome`: `src/openmd/backends/x_browser.py:66`
- `browser/close`: `src/openmd/backends/discourse.py:157`, `src/openmd/backends/douyin.py:525`
- `browser/eval`: `src/openmd/backends/discourse.py:144`, `src/openmd/backends/douyin.py:514`
- `browser/open`: `src/openmd/backends/discourse.py:116`, `src/openmd/backends/douyin.py:511`
- `chatgpt/detail`: `src/openmd/backends/ai_chat.py:103`
- `chatgpt/status`: `src/openmd/backends/ai_chat.py:255`
- `deepseek/detail`: `src/openmd/backends/ai_chat.py:103`
- `deepseek/status`: `src/openmd/backends/ai_chat.py:255`
- `doubao/detail`: `src/openmd/backends/ai_chat.py:103`
- `doubao/status`: `src/openmd/backends/ai_chat.py:255`
- `douyin/detail`: `src/openmd/backends/douyin_detail.py:86`
- `douyin/user-videos`: `src/openmd/backends/douyin.py:273`
- `gemini/detail`: `src/openmd/backends/ai_chat.py:103`
- `gemini/status`: `src/openmd/backends/ai_chat.py:255`
- `grok/detail`: `src/openmd/backends/ai_chat.py:103`
- `grok/status`: `src/openmd/backends/ai_chat.py:255`
- `openmd-douyin/creator-videos`: `src/openmd/backends/douyin.py:289`
- `openmd-xiaohongshu/creator-notes`: `src/openmd/backends/xiaohongshu.py:124`
- `openmd-xiaohongshu/note-assets`: `src/openmd/backends/xiaohongshu_providers.py:98`
- `qwen/detail`: `src/openmd/backends/ai_chat.py:103`
- `qwen/status`: `src/openmd/backends/ai_chat.py:255`
- `twitter/article`: `src/openmd/backends/x.py:130`
- `twitter/thread`: `src/openmd/backends/x.py:102`
- `twitter/tweets`: `src/openmd/backends/x.py:116`
- `web/read`: `src/openmd/backends/web_article.py:660`, `src/openmd/backends/xiaoyuzhou.py:163`
- `weixin/download`: `src/openmd/backends/weixin.py:269`
- `xiaoe/catalog`: `src/openmd/backends/xiaoe.py:134`
- `xiaoe/content`: `src/openmd/backends/xiaoe.py:152`
- `xiaoe/play-url`: `src/openmd/backends/xiaoe.py:129`
- `xiaohongshu/note`: `src/openmd/backends/xiaohongshu.py:95`
- `xiaohongshu/user`: `src/openmd/backends/xiaohongshu.py:100`
- `xiaoyuzhou/episode`: `src/openmd/backends/xiaoyuzhou.py:324`
- `xiaoyuzhou/podcast-episodes`: `src/openmd/backends/xiaoyuzhou.py:149`
- `xiaoyuzhou/transcript`: `src/openmd/backends/xiaoyuzhou.py:128`
- `youtube/channel`: `src/openmd/backends/youtube_discovery.py:178`
- `youtube/playlist`: `src/openmd/backends/youtube_discovery.py:286`
- `youtube/transcript`: `src/openmd/backends/youtube.py:45`
- `yuanbao/detail`: `src/openmd/backends/ai_chat.py:103`
- `yuanbao/status`: `src/openmd/backends/ai_chat.py:255`
