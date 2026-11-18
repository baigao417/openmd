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

ChatGPT also recognizes standalone `data-author` and user-message bubbles.
A separate user-container sweep is compared with parsed user turns; a missing
user turn downgrades the result even if the general turn selector misses it.

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

## Thinking sections

By default, recognized assistant thinking sections are omitted and counted in
source notes (`已省略思考过程：N 段`). Use `--include-thinking` with `go`, set
`include_thinking: true` in a native `save_snapshot` request, or enable
**AI 对话 → 保留思考过程** in extension settings to place them in a separate
`思考过程` subsection before that turn's final answer. They do not add turns.
Login retries retain the queued option. Existing extension settings default to
omission; reloading the updated extension requires joint acceptance.

Exact DOM signals are defined in `THINKING_SELECTORS` and
`COMMON_THINKING_SELECTOR`. DeepSeek's `.ds-markdown--think` is based on the
managed helper's explicit separation. ChatGPT, Gemini, Doubao, Qwen and Yuanbao
variants have synthetic regression coverage and await authorized live DOM
acceptance. Gemini custom turn elements also retain thinking siblings outside
`.model-response-text`. Unknown structures and prose are preserved unchanged;
the parser never guesses a boundary from words such as “Thought for”.
The experimental detail path honors separately supplied thinking fields; when
its upstream has flattened them into `Text`, that text is preserved with a note.

## Output quality

Math uses `annotation[encoding="application/x-tex"]` when present, with `$…$`
inline and `$$…$$` in display blocks. Missing annotations keep MathML or visible
text and add a formula-fidelity warning. Oversized data-URI images (over 8192
UTF-8 bytes in the URI) become counted placeholders, retaining alt text and
figure captions. Small images explicitly marked as icons or at most 32×32 are
removed; other small images and external image links retain existing behavior.

Both routes share title cleanup: remove a recognized site suffix; for generic
site titles use the current DOM/session title, then the first user message's
first 30 characters. When the generated filename changes, sync atomically
renames an unedited canonical file and updates its manifest path. The receipt's
`sync.renamed_from` is the previous path relative to the save directory. An
occupied name receives the usual numeric suffix; repeated saves reuse it.
User edits retain the original canonical path and conflict protection. A missing
canonical file is restored at its recorded path. Content operation values keep
their existing meaning; a rename can accompany `unchanged` content.
`TurnBlock` and legacy block projections normalize the four standard roles to
`user`, `assistant`, `system`, `tool`; unknown speaker identities are preserved.
