# Openmd Agent Skill

Use this skill when an agent needs a non-interactive, source-faithful Markdown or transcript from Openmd.

## Standard invocation

```bash
openmd "<url-or-local-path>" -o <output-directory> --json
openmd batch <links.txt> --out <output-directory> --json
```

Use an explicit output directory for automation. Do not parse human-readable prose when `--json` is available. The saved Markdown remains the source text; Openmd does not summarize, translate, or polish it.

## JSON receipt fields

Every `--json` receipt exposes these fields at the top level when the route reaches the CLI result:

- `status`: whether the result is usable (`verified`, `partial`, or `failed`).
- `source_level`: where the body came from, such as `native_text`, `native_caption`, `auto_caption`, `media_asr`, or `browser`.
- `completeness`: coverage details and whether the expected source was fully obtained.
- `sync`: canonical-file update state, such as `created`, `unchanged`, `conflict_kept_user_edit`, or `restored_missing`.
- `login`: login state or a login requirement discovered by the route.
- `next_action`: the narrow next step for an agent or user.
- `error_code`: a stable machine-readable failure category, or `null` on success.

A zero process exit does not by itself prove that the body is valid. Check `status`, `completeness`, and the saved artifact.

## Common errors and next actions

| `error_code` | Next action |
|---|---|
| `login_required` | Ask the user to sign in to the supported browser profile, then retry. Do not ask the agent to handle credentials. |
| `engine_unavailable` | Install the named optional extra or local converter shown by `next_action`; retry in the same output directory. |
| `ai_chat_not_stable` / `ai_chat_empty` | Treat the export as unavailable or partial; retry only after the site has finished loading and keep the receipt. |
| `ai_chat_site_not_implemented` | Do not substitute another site route. Report the site as unsupported. |
| `opencli_write_not_allowed` | Use a read-only OpenCLI command, or obtain an explicit `--allow-write` authorization for the exact write. |
| `opencli_daemon_foreign` | Do not stop or replace the daemon. Check the configured managed OpenCLI location and retry later. |
| `douyin_media_asr_not_enabled` | Install/configure the local `[asr]` path if transcription is wanted; metadata or detail identity alone is not a transcript. |
| `xiaohongshu_request_blocked` | Report the site block. Do not bypass site security controls. |

## Agent 自带浏览器时

For AI conversations, first open the authorized conversation with the agent's
browser controller and wait for its message history to load and generation to
finish. Read that tab's current URL and `document.documentElement.outerHTML`,
and save the HTML to a private UTF-8 local file. Do not read cookies, storage,
credentials, or unrelated tabs. Pass the current URL even after a redirect so
login pages can return `login_required`.

```bash
openmd go <page.html> --as-snapshot --url <current-conversation-url> -o <private-output-directory> --json
```

This existing snapshot entry uses the same conversation parser and receipt
fields as the extension's current-page capture. It does not open the URL again.
ChatGPT, Grok, DeepSeek, Doubao, Gemini, Qwen and Yuanbao have site parsers;
Claude is not implemented. The output includes role-ordered turns, original
code and tables. Coverage is limited to the loaded DOM branch. Compare the
receipt's `completeness`; `unknown` does not prove the whole chat was loaded.
An active stop-generation control yields `partial` / `ai_chat_not_stable`.

For “latest N turns”, the agent controls scrolling/loading and repeats capture
until its authorized range is present, then selects N ordered turns downstream.
Openmd does not scroll, send a prompt, or load more history from this entry.
Repeated capture uses the same source identity as URL detail: reuse the output
directory to obtain `unchanged` or `conflict_kept_user_edit`. Keep private HTML
and Markdown out of prompts, logs and reports. Never use a real conversation
body as an example or fixture. The background URL detail route remains an
experimental fallback.

## Managed OpenCLI

`openmd opencli list` and `openmd opencli <site> <command> [args]` use Openmd's pinned, managed OpenCLI and preserve its stdout, stderr, and exit status for read-only commands. Built-in read-only commands such as `doctor`, `list`, `status`, `daemon status`, and supported `--help` are allowed. Writes, unknown commands, and unknown-site help fail closed unless the caller explicitly supplies `--allow-write`.

## Privacy boundary

- Never print, store, or copy cookies, tokens, passwords, profile contents, or conversation bodies into prompts, logs, reports, fixtures, or committed artifacts.
- Treat login state as a boolean or structured receipt only; do not export private history unless the user has explicitly authorized that exact source and destination.
- Keep network, browser, media download, local ASR, OCR, and cloud capabilities within the route's explicit permissions. Cloud services are never enabled by default.
- If a result is incomplete, preserve the `partial` status and the missing coverage instead of presenting it as verified.
