<div align="center">

# Openmd

**Save any link as clean Markdown — the original text or full transcript, not a summary.**

[![PyPI](https://img.shields.io/pypi/v/openmd-cli?color=3b5bdb)](https://pypi.org/project/openmd-cli/)
[![Python](https://img.shields.io/pypi/pyversions/openmd-cli)](https://pypi.org/project/openmd-cli/)
[![CI](https://github.com/baigao417/openmd/actions/workflows/ci.yml/badge.svg)](https://github.com/baigao417/openmd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/baigao417/openmd/blob/main/LICENSE)

English · [简体中文](https://github.com/baigao417/openmd/blob/main/README.zh-CN.md)

</div>

Openmd turns web articles, WeChat posts, Bilibili and YouTube videos, podcasts and more into one consistent Markdown format that both people and AI agents can read. It keeps the source's own words: no summarizing, no rewriting, no added punctuation. When something can't be fetched completely, it says what is missing instead of saving a broken page.

```bash
pip install openmd-cli
openmd "https://www.youtube.com/watch?v=jNQXAC9IVRw"
```

## Highlights

- **One command, many sources.** Articles, WeChat Official Account posts, X posts, Bilibili and YouTube subtitles, Xiaoyuzhou podcast transcripts, PDFs and Office files.
- **Original text first.** Captions and transcripts are grouped into readable, timestamped paragraphs; the words themselves are never changed.
- **Honest results.** Error pages, login walls and "verify you are human" screens are rejected, not saved. Partial results are labeled as partial.
- **Works after `pip install`.** No Node.js or extra tools to install by hand; the browser reader prepares itself the first time it is needed.
- **Uses your browser when it has to.** One browser extension lets Openmd read pages through the Chrome or Edge profile you are already logged in to, in a minimized background window.
- **Local by default.** Everything runs on your machine. Cloud services are never turned on unless you ask for them.
- **Built for agents too.** `openmd "<url>" -o <dir> --json` runs without prompts and returns a machine-readable receipt.

## Install

Requires Python 3.10 or newer. Windows, macOS and Linux.

```bash
pip install openmd-cli          # or: pipx install openmd-cli / uv tool install openmd-cli
openmd doctor                   # shows what works now and how to add the rest
```

The package is called `openmd-cli`; the command and the Python module are both `openmd`.

## Usage

```bash
openmd                                   # paste a link, press Enter, choose a folder
openmd "https://sspai.com/post/73145"    # process this link, then ask where to save
openmd "https://..." -o ~/notes          # save straight into a folder, no questions
openmd "https://..." -o ~/notes --json   # same, with a JSON receipt for scripts and agents
openmd batch links.txt --out ~/notes     # one link per line
```

Each link becomes `Title.md` in the folder you choose. Run logs go to `~/.openmd/runs/`, never into your notes.

## What it can read

| Content | Ready after install | Also needs |
|---|:---:|---|
| Web articles | ✓ | — |
| YouTube videos with captions | ✓ | — |
| Xiaoyuzhou podcast transcripts | ✓ | — (a ~20 MB reader component is set up automatically on first use) |
| WeChat articles, X posts, Bilibili subtitles | ✓ | The Openmd browser extension; Bilibili AI subtitles also need you to be logged in |
| Videos and podcasts without subtitles | | `pip install "openmd-cli[asr]"` and a local speech model ([guide](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)) |
| PDF, Word, PowerPoint, Excel, images | | `pip install "openmd-cli[document]"` |
| Douyin, Xiaohongshu | experimental | Browser extension and login; these sites often block automated reads |

Run `openmd doctor` to see what is ready on your machine. If a link fails, Openmd prints the reason and one line to paste into a [bug report](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml).

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

## Browser extension

Some sites only show their content to a logged-in browser. The Openmd extension (Chrome and Edge; Windows for now) lets Openmd read those pages through your own browser profile.

1. Run `openmd browser-setup`. It registers the local connection and opens the extension folder.
2. Open `chrome://extensions` (or `edge://extensions`), turn on **Developer mode**, click **Load unpacked**, and choose that folder.
3. Click the Openmd icon on any page to save it. Select several tabs to save them all, or right-click a selection to save every link in it.

While a page is being read, Chrome may show a bar saying that Openmd started debugging the browser. Chrome shows this for every extension that uses its debugging interface, and extensions cannot hide it. The reading itself happens in a separate, minimized window.

## Configuration

| Environment variable | Effect |
|---|---|
| `OPENMD_AUTO_INSTALL_TOOLS=0` | Never download the browser reader component automatically |
| `OPENMD_NPM_REGISTRY` | npm mirror used for that one-time download |
| `OPENMD_SHOW_BROWSER=1` | Keep the background browser window visible, for troubleshooting |
| `OPENMD_DATA_DIR` | Where Openmd keeps its settings, logs and tools (default `~/.openmd`) |

More: [tool integration](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md), [current routes](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md), [architecture map](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html).

## FAQ

**Does Openmd summarize or clean up the text?**
No. It only changes whitespace and paragraph breaks. Summaries, translation and polishing are left to whatever tool you use next.

**Where does my data go?**
Nowhere. Openmd runs locally, reads pages with your own network and browser, and writes files to the folder you choose. It never stores cookies or passwords.

**Why is the package called `openmd-cli`?**
The name `openmd` on PyPI belongs to an unrelated project. Once installed, the command is still `openmd`.

**A link didn't work.**
Run it again with `--verbose` to see which reader was tried, then open a [link failure report](https://github.com/baigao417/openmd/issues/new?template=link-failure.yml) with the line Openmd printed. Failing links are the most useful contribution there is.

## Contributing

Bug reports with a real link, new platform integrations and fixes are all welcome. Most new platforms need only a small JSON "tool card" — see [adding a platform](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md). Development works the same on Windows, macOS and Linux:

```bash
git clone https://github.com/baigao417/openmd && cd openmd
python scripts/dev_setup.py     # creates .venv, installs dev dependencies, checks ffmpeg / Node
python -m pytest
```

Every fix comes with a test that failed before it, and every "it works" claim comes with a real link that was actually run. Read [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md) and the [code standards](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md) before opening a pull request.

## Acknowledgements

Openmd stands on the work of [opencli](https://github.com/jackwener/opencli) (browser reading, built in), [trafilatura](https://github.com/adbar/trafilatura), [youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api), Bilibili-Harvest (Lanbin07), [faster-whisper](https://github.com/SYSTRAN/faster-whisper) and [MarkItDown](https://github.com/microsoft/markitdown). Details and licenses: [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md).

## License

[MIT](https://github.com/baigao417/openmd/blob/main/LICENSE). Respect the copyright of what you save; Openmd is meant for personal archiving and research.
