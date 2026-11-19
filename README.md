<div align="center">

# Openmd

**Everything to Markdown — just beginning.**

**Turn what you can see but can't take out into Markdown your agents can use.**

[![PyPI](https://img.shields.io/pypi/v/openmd-cli?color=3b5bdb)](https://pypi.org/project/openmd-cli/)
[![Python](https://img.shields.io/pypi/pyversions/openmd-cli)](https://pypi.org/project/openmd-cli/)
[![CI](https://github.com/baigao417/openmd/actions/workflows/ci.yml/badge.svg)](https://github.com/baigao417/openmd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/baigao417/openmd/blob/main/LICENSE)

English · [简体中文](https://github.com/baigao417/openmd/blob/main/README.zh-CN.md) · [日本語](https://github.com/baigao417/openmd/blob/main/README.ja.md) · [한국어](https://github.com/baigao417/openmd/blob/main/README.ko.md)

</div>

AI models keep getting smarter, but what they can read still depends on what you can feed them.

Much of the information that matters is stuck inside platforms: articles behind a login, the exact words spoken in a video, a long podcast conversation, an afternoon-long chat with an AI, a discussion in a group chat. You can see it. You just can't take it out.

Openmd aims to be a cornerstone: it turns that content into open, portable Markdown that **people and agents can both read and use directly**. Web articles, video platforms, office files, chat conversations — all of it can become raw material for your own knowledge base.

We build for AI-native work first. Openmd keeps the original text unchanged, attaches a receipt stating the source and how complete the result is, and rejects error pages and login walls outright. For an agent, "can I trust this material?" matters as much as "do I have it?"

Everything to Markdown. This is just the beginning.

---

## Quick start

Openmd is a command-line tool and a Python module, `openmd` (package name `openmd-cli`). It requires Python 3.10 or newer and runs on Windows, macOS and Linux.

```bash
# Install the base package (or: pipx install openmd-cli / uv tool install openmd-cli)
pip install openmd-cli

# Check your local environment and optional components
openmd doctor

# Paste a link, press Enter, choose where to save
openmd

# Save one web page straight into a folder
openmd "https://example.com/article" -o ./notes

# For agents and scripts: no prompts, plus a machine-readable JSON receipt
openmd "https://example.com/article" -o ./notes --json

# Process a list of links (one per line); the batch command takes --out
openmd batch links.txt --out ./notes
```

Each link becomes `Title.md` in the folder you choose. Run logs go to `~/.openmd/runs/`, never into your notes.

For local office documents or local audio/video transcription, install the optional extras:

```bash
# Document and image parsing
pip install "openmd-cli[document]"

# OCR for images and scanned pages
pip install "openmd-cli[ocr]"

# Optional Pandoc conversion fallback
pip install "openmd-cli[pandoc]"

# Local speech recognition (ASR) for audio and video
pip install "openmd-cli[asr]"

# Save video/audio files (Openmd-managed yt-dlp)
pip install "openmd-cli[media]"
```

> Note: besides the Python packages, document parsing, OCR and local ASR may also need FFmpeg or local models. `openmd doctor` shows what is ready.

To connect Openmd to your own agent or automation pipeline, see the [tool integration guide](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md).

## MCP for agents

Use CLI + SKILL.md with shell access; use MCP for discoverable typed tools and per-tool client approval. Install the optional extra, then start the stdio server. Claude Code:

```bash
pip install "openmd-cli[mcp]"
claude mcp add --transport stdio openmd -- openmd mcp
```

One-line `uvx` setup (no prior installation):

```bash
uvx --from "openmd-cli[mcp]" openmd mcp
```

Cursor: add the same stdio server in **Cursor Settings → MCP → Add new MCP Server**:

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

Windows: use an absolute executable path and UTF-8 process output when needed:

```json
{
  "mcpServers": {
    "openmd": {"command": "C:\\path\\to\\openmd.exe", "args": ["mcp"], "env": {"PYTHONIOENCODING": "utf-8"}}
  }
}
```

Claude Desktop: add this to its MCP configuration; use an absolute executable path if needed.

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

The four tools are `openmd_save`, `openmd_save_page`, `openmd_job` and `openmd_doctor`. `openmd_save` accepts one source or up to 20 `sources`; `openmd_job(action="cancel")` cancels a queued or running job. Clients that send a `progressToken` receive queue progress, while polling remains available. Completed Markdown artifacts are exposed through `openmd://jobs` and `openmd://job/{job_id}` resources, with bounded reads. Output directories must be explicit absolute paths. HTML snapshots accept a string or an absolute file path, up to 2 MiB UTF-8. All five capability flags default off; explicitly grant `network`, `browser`, `media`, `asr` or `cloud`. The CLI capability flags are reused; MCP does not infer the daily CLI grants from an output directory and does not remember it. This stdio setup requires a local process launcher; remote connectors need their own transport bridge.

---

## Why it fits in front of AI and knowledge bases

Openmd is not a knowledge base or a RAG retrieval system. It focuses on the foundational step: getting information out and getting it straight.

1. **Keep the original; leave interpretation to later steps**
   By default Openmd keeps the full original text, captions or transcript, and only tidies paragraphs and whitespace. It does not summarize, translate or rewrite. People can check the exact wording, and no key details are lost before the text reaches an AI context.

2. **Source, metadata and timestamps included**
   The Markdown can carry the title, author, original link and other metadata (as far as the source provides them). Audio and video transcripts keep timestamps and a note on the source and completeness. An optional structured package output is available for downstream tools.

3. **Invalid pages are rejected, not saved**
   When Openmd recognizes an error page or a login wall, it rejects the result and reports it in the receipt instead of quietly saving a broken page as a normal-looking note. (Web pages vary too much to promise that every bad page is caught.)

---

## What it can read today

| Category | Source | Status | Notes |
| :--- | :--- | :--- | :--- |
| **Web and social** | Web articles | Ready | Works right after install |
| | WeChat Official Account posts, X posts | Ready | Needs the Openmd browser extension and your existing access |
| | Save current page, login reminders | Ready | Requires the Openmd browser extension |
| **Audio, video and transcripts** | YouTube (with captions), Xiaoyuzhou | Ready | Xiaoyuzhou sets up a ~20 MB reader component on first use |
| | Bilibili, Xiaoe-tech | Ready | Needs the browser extension; Bilibili AI subtitles also need you to be logged in |
| | Audio/video without subtitles, local media | Needs extras | Needs the `[asr]` extra, FFmpeg and a local speech model |
| **Local documents and images** | PDF, DOCX, PPTX, XLSX, images | Needs extras | Needs the `[document]` extra; complex layouts and scans may go through an extra OCR route |
| **Workplace docs** | Feishu / Lark documents | Partial | Needs `lark-cli` installed and logged in on your machine, plus `--enable-network-backends` to allow network reads |
| **AI chat** | Doubao, Gemini, Qwen, Yuanbao, Grok, DeepSeek (an open conversation page) | Partial | Needs the browser extension: click on the page to save what is already loaded, or let an agent hand the page over with `--as-snapshot`; covers the visible branch; model thinking is omitted by default |
| | ChatGPT conversation pages; ChatGPT / Claude official export ZIP or JSON | Experimental | A ChatGPT page that does not render the user's prompts is saved as partial; official exports import locally, without a browser |
| **Chat history** | QQ group export files (JSON / HTML / TXT) | Paused | Export parsing exists but is not yet offered as a stable feature; Openmd never reads or decrypts chat databases |
| **Experimental** | Douyin, Xiaohongshu | Experimental | Needs the browser extension and login; these sites often block automated reads, so stability and full coverage are not promised |

### Local document formats

The `[document]` extra is the baseline for local document parsing. This matrix was measured on a clean install; it describes native text and reports partial results honestly. Install `[ocr]` for images or scans and `[pandoc]` for an optional conversion fallback. See the [format chains and limits](https://github.com/baigao417/openmd/blob/main/docs/subsystems/document-formats.generated.md).

| Formats | Status with `[document]` | Optional enhancement / limitation |
|---|---|---|
| PDF with native text | Available | Scans need local OCR; table geometry is not certified |
| DOCX | Partial | Text is available; table shape is not certified for every sample |
| PPTX, XLSX, XLS | Available | Embedded image, notes, formula-cache and binary-image limits are reported |
| HTML, HTM, EPUB, RTF, ODT, ODS, ODP, FB2 | Available or partial | Local files; scripts do not run; complex layout may be partial |
| CSV, TSV, JSON, XML, IPYNB, MSG | Available | Large tables may be truncated; notebooks are never executed; MSG attachments are reported as gaps |
| DOC, PPT | Partial text fallback | Install LibreOffice for the preferred conversion; layout and active binary slide order are not certified |
| ZIP | Partial, with per-entry status | Mixed-media entries require ASR/OCR separately; archive resource limits apply |
| MOBI, AZW3 | Requires Calibre | Missing tool returns `engine_unavailable`; encrypted books are rejected without decryption |

The Explorer / Finder / file-manager action is installed per user with `openmd shell-menu install`. Right-click a supported document to create a neighboring `.md`; right-click a folder to create a `<folder>-md` mirror that preserves subfolders. Repeated runs skip unchanged inputs and preserve edited Markdown. Same-name files in different formats keep their extensions in the Markdown filename. On Windows, install Openmd persistently with `uv tool install openmd-cli` or `pipx install openmd-cli` before adding the menu; temporary `uvx` cache installs are rejected. Scanned PDFs still need local OCR. Windows 11 may show the classic HKCU menu under **Show more options**; a first-level packaged Explorer command is outside this lightweight installer. Use `openmd shell-menu uninstall` to remove it.

Run `openmd doctor` to see what is ready on your machine. If a link fails, Openmd prints the reason and one line to paste into a [bug report](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml).

---

## What the output looks like

```markdown
---
title: "How we built the reading lamp"
author: "Example Studio"
url: "https://www.bilibili.com/video/BV..."
published: "2026-05-24"
platform: "bilibili"
---

# How we built the reading lamp

**00:00:00**

Today I want to show you the lamp we spent three months on and why the first two versions failed

**00:00:31**

The first prototype was too bright at night so we added a second light source under the shade

*来源：自动字幕；识别错误保持原样。*
```

- YAML front matter that note apps (Obsidian, Logseq) and scripts can read.
- Transcripts are split into paragraphs of roughly 30 seconds, about 450 characters, or a natural pause, each starting with its timestamp, so people can skim and agents can cite a moment.
- The last line states where the text came from (original text, platform captions, auto captions, speech recognition …) and whether it is complete. The label is currently in Chinese.

---

## Browser extension and local operation

- **Reuses the browser access you already have.** Some routes — WeChat Official Accounts, X, Bilibili and others — work through the Openmd browser extension, and some content requires you to be logged in. The extension currently supports **Chrome and Edge on Windows**. Openmd only uses access you legitimately have; it does not promise to get around logins, paywalls, DRM or other access controls.
- **Local first, with clear network boundaries.** Openmd runs locally, but reading online content necessarily contacts the original site, and some components may download on first use. Anything involving a cloud service must be turned on by you explicitly.

Install the extension:

1. Run `openmd browser-setup`. It registers the local connection and opens the extension folder.
2. Open `chrome://extensions` (or `edge://extensions`), turn on **Developer mode**, click **Load unpacked**, and choose that folder.
3. Click the Openmd icon on any page to save it. Select several tabs to save them all, or right-click a selection to save every link in it.

On any public `http(s)` page, the right-click menu also offers **Save with video/audio transcript** and **Save with video/audio file**. Native transcript routes are not transcribed twice; `file` keeps public decoded media beside the Markdown when the route supports it. DRM, paywalled and encrypted media are not retained.

You can also download the extension ZIP from [Releases](https://github.com/baigao417/openmd/releases), unzip it and load the unpacked folder after running `openmd browser-setup`.

- **Default browser behavior.** Reads happen without a popup in inactive background tabs in an existing window; owned tabs start muted and close automatically when the job finishes.

Chrome may show a browser-provided bar saying that Openmd started debugging the browser; extensions cannot hide this notice.

---

## Managed OpenCLI

OpenCLI is Openmd's managed browser and platform component. `openmd opencli list` and
`openmd opencli <site> <command> [args]` pass through read-only commands and preserve their
output and exit status. Writes and unknown commands fail closed unless you explicitly add
`--allow-write`; Openmd does not print credential contents.

---

## Configuration

| Environment variable | Effect |
|---|---|
| `OPENMD_AUTO_INSTALL_TOOLS=0` | Never download the browser reader component automatically |
| `OPENMD_NPM_REGISTRY` | npm mirror used for that one-time download |
| `OPENMD_SHOW_BROWSER=1` | Keep the background browser window visible, for troubleshooting |
| `OPENMD_DATA_DIR` | Where Openmd keeps its settings, logs and tools (default `~/.openmd`) |

More: [tool integration](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md), [current routes](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md), [architecture map](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html).

---

## Where it's heading

**Everything to Markdown** is a long-term vision. These directions are being explored and are not delivered yet:

- **Web AI chat export** reads the conversation page you already have open (extension click, or an agent's own browser via `--as-snapshot`); it never re-opens the link in the background. It is partial support: only the loaded, visible branch is saved. ChatGPT / Claude official exports can be imported offline with `openmd go export.zip -o archive --chatgpt-export` / `--claude-export` (JSON is also accepted; formats are auto-detected). Use `--claude-title`, `--claude-since`, `--claude-until` to filter Claude exports; non-text blocks and attachments become counted placeholders. Repeated imports preserve user edits.
- **E-books and more text formats**: EPUB, RTF, MOBI, AZW3 and others.
- **Broader document parsing**: better extraction from documents with complex layouts.

None of these counts as a delivered feature until it has been verified on real samples.

---

## FAQ

**Does Openmd summarize or clean up the text?**
No. It only changes whitespace and paragraph breaks. Summaries, translation and polishing are left to whatever tool you use next.

**Where does my data go?**
Nowhere. Openmd runs locally, reads pages with your own network and browser, and writes files to the folder you choose. It never stores cookies or passwords.

**Why is the package called `openmd-cli`?**
The name `openmd` on PyPI belongs to an unrelated project. Once installed, the command is still `openmd`.

**A link didn't work.**
Run it again with `--verbose` to see which reader was tried, then open a [link failure report](https://github.com/baigao417/openmd/issues/new?template=link-failure.yml) with the line Openmd printed. Failing links are the most useful contribution there is. If a page you already had open (for example an AI chat) was saved wrongly, run `openmd report feedback <job-id> -o <dir>` to create a sanitized problem package (page structure only, text replaced with synthetic text), check it, and attach it as well.

---

## Contributing

Bug reports with a real link, new platform integrations and fixes are all welcome. Most new platforms need only a small JSON "tool card" — see [adding a platform](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md). Development works the same on Windows, macOS and Linux:

```bash
git clone https://github.com/baigao417/openmd && cd openmd
python scripts/dev_setup.py     # creates .venv, installs dev dependencies, checks ffmpeg / Node
python -m pytest
```

Every fix comes with a test that failed before it, and every "it works" claim comes with a real link that was actually run. Read [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md) and the [code standards](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md) before opening a pull request.

---

## Acknowledgements and license

Openmd stands on the work of [OpenCLI](https://github.com/jackwener/opencli) (browser reading, built in), [trafilatura](https://github.com/adbar/trafilatura), [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api), Bilibili-Harvest (Lanbin07), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [MarkItDown](https://github.com/microsoft/markitdown). Details and licenses: [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md).

Openmd is released under the [MIT](https://github.com/baigao417/openmd/blob/main/LICENSE) license. Respect the copyright of what you save; Openmd is meant for personal archiving and research.

**Everything to Markdown — just beginning.**
