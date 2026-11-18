# Loaded AI conversation pages

`go page.html --as-snapshot --url <original-url> -o <directory> --json`
and the extension's native `save_snapshot` queue use `backends/loaded_chat.py`
through `SnapshotProvider`. The parser reads local HTML without navigating or
starting a browser. It covers ChatGPT, Grok, DeepSeek, Doubao, Gemini, Qwen and
Yuanbao. Claude has no conversation parser.

## Selector provenance

Role and content selectors are ported from the Apache-2.0 licensed, managed
OpenCLI **2.0.3** package. `clis/<site>/detail.js` delegates to these helpers:

| Site | Helper file and function | Turn roots |
| --- | --- | --- |
| ChatGPT | `utils.js`, `getVisibleMessages` | author-role / conversation-turn / search-unit attributes |
| Grok | `utils.js`, `getMessageBubbles` | user-message / assistant-message test IDs |
| DeepSeek | `utils.js`, `getVisibleMessages` | `.ds-message`; upstream class-count role rule |
| Doubao | `utils.js`, `getConversationDetailScript` | message-list union_message with send/receive role |
| Gemini | `utils.js`, `getGeminiVisibleTurns` | query-text / model-response-text; legacy role sweep |
| Qwen | `utils.js`, `getMessageBubbles` | data-chat-question-wrap / data-chat-answers-wrap |
| Yuanbao | `shared.js`, `getYuanbaoMessageBubbles` | human / ai list items, hyc-content body |

The common Python parser preserves DOM order and repeated identical turns;
overlapping containers are removed by node identity. Code whitespace and
language are preserved with fenced literals; tables keep their original cell
text through the existing HTML converter. No model rewrites the content.
Site controls and hidden content are excluded. Static HTML can only identify
inline/attribute hiding; callers should capture a loaded, rendered page.

## Coverage and identity

Coverage is always `visible_branch`: DOM history can be virtualized or unloaded.
The count of recognized visible turn containers is collected before empty or
unrecognized turns are discarded, then checked by core completeness. Gemini's
legacy sweep has no independent count and reports `unknown`. A matching visible
count certifies only the captured branch, never the full remote conversation.
An enabled stop-generation control keeps the result `partial` with
`ai_chat_not_stable`. Login gates return `login_required`; empty pages return
`ai_chat_empty` with no artifact.

Snapshots resolve the original URL using the same adapter and source identity
as the experimental URL detail route. Grok's `rid` remains an anchor and does
not change the canonical conversation identity. Sync uses the existing core
manifest and preserves user edits. The background URL route remains an
experimental fallback; this parser does not change its loaders or navigation.

Synthetic fixtures cover all seven structures. Actual extension reload/click
requires joint acceptance after integration; fixture checks do not establish
live availability of every site.
