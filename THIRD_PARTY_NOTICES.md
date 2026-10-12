# Third-Party Notices

No complete third-party repository, vendored library, installer, model weight, captured
article, course, transcript, image or media file is included in this snapshot.
The OpenCLI plugins in `integrations/` are project-owned adapter code and import
the external OpenCLI host; they do not include its implementation.

Optional runtime dependencies are declared in `pyproject.toml` or plugin
`package.json` files. Executable integrations (OpenCLI, yt-dlp, FFmpeg and
lark-cli) must be installed separately by the user. Their own licenses apply;
this repository's MIT license does not relicense dependencies, model weights,
external services or content retrieved later.

Dependency names used by the source include faster-whisper, MarkItDown,
RapidOCR, Pillow, trafilatura, youtube-transcript-api, Docling, FunASR,
PaddleOCR and Tesseract. Some lanes are only placeholders, and dependency
installation alone does not make them operational. esbuild and pytest are
development/test tools. No installed dependency files are redistributed here.

## Bilibili-Harvest WBI reference

`src/openmd/backends/bilibili_wbi.py` adapts the request-signing core represented
by Bilibili-Harvest's `bili_wbi.py`, including the protocol permutation table.
The reference project's local LICENSE identifies MIT and
`Copyright (c) 2023 Lanbin07`. The original license text is preserved in
`LICENSES/Bilibili-Harvest-MIT.txt`. This attribution covers the adapted signing
core; the rest of the upstream repository is not included. Openmd adds its own
provider interface, injected fetcher, cache encapsulation and explicit
user-provided configuration boundary.

A backend name containing `bilibili-harvest` is a compatibility identifier,
not a claim that the complete upstream pipeline is integrated.

The read-only test environment used an already installed OpenCLI 1.8.7 host.
Its installed package metadata declares Apache-2.0. No OpenCLI files are copied
into this repository, and its license is independent from Openmd's MIT license.

All committed package samples and mock content are synthetic. No ownership or
redistribution right is claimed over platform data. If redistributing external
dependencies or media in a future release, first include the exact upstream
license texts and attribution for the versions and files being distributed.
