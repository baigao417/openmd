# Local OCR router

## Document engine adapter and Office images

`openmd.ocr.engine.OcrEngine` implements `DocumentEngine` using the existing
`TranscriptResult`, components, error codes and OCR block/evidence metadata.
Local `ocr.engine` configuration selects `ocr.chain`, `ocr.ovis3`,
`ocr.minicpm` or optional `ocr.rapidocr`. The chain retains its existing
configurable breakers, GPU lease, quarantine and unload lifecycle.

Native DOCX/PPTX/XLSX readers retain embedded images at their source
paragraph, slide shape or worksheet row/column. With local OCR permission,
recognized text is placed there with `来源：OCR`; unavailable OCR leaves an
explicit image text gap and a partial receipt. Duplicate image bytes reuse
recognition within the document while retaining each occurrence. Worksheet
placement is based on package relationships and drawing anchors, not layout
rendering; floating objects and paragraph/table geometry are not certified.

See [the comparison procedure](benchmarks/ocr/README.md) for frozen benchmark
and hardware plan boundaries.

## Existing recognition routes

The screenshot route is `ocr.chain → ocr.rapidocr → ocr.tesseract`. The chain
uses Ovis3 for page recognition and switches to MiniCPM layout crops if a
configured breaker trips. `ocr.ovis3` and `ocr.minicpm` are also explicit backend
IDs for diagnosis and benchmarks. No summary, translation or polishing is added.

## Existing dependencies and configuration

PyMuPDF, Pillow and NumPy are optional: explicitly install `openmd-cli[ocr-router]`
in the Openmd interpreter if needed. Openmd never installs them automatically.
**PyMuPDF is AGPL licensed; evaluate licensing before distributing Openmd with
this dependency.** MarkItDown remains the default Office parser and uses the
existing optional `document` extra. LibreOffice conversion is used only if its
executable already exists; it is never installed by Openmd.

MinerU is an optional, user-managed local OCR executable. Configure
`tools.mineru.executable` in `openmd.local/v1` or set `OPENMD_MINERU`; Openmd
does not install MinerU or download its weights. Select it only for a local
PDF with `--ocr-engine mineru`; `auto` preserves the existing route. The
optional `tools.mineru.tier` value is `basic` (default) or `standard`; set
`tools.mineru.default` to `mineru` to select it by default. MinerU
is AGPL licensed and its model weights have separate terms: install and use it
under the applicable licenses. Openmd invokes the external CLI and does not
bundle its code or weights.

Start Ollama yourself and make the two required models available in advance.
Openmd only reads model metadata/digests, performs explicitly enabled inference
and unloads its own model after each page/tile. It never starts/stops the server,
pulls models or downloads weights. Defaults: `http://127.0.0.1:11434`,
`ovis3-bf16`, `minicpm-ocr-f16`.

Only loopback endpoints are accepted: canonical IPv4 in `127.0.0.0/8`,
`::1`, or hostnames whose **every** resolved address is loopback. Connections
are pinned to a validated numeric address; redirects are refused. Legacy
numeric IPv4 forms, IPv6 mapped addresses and remote endpoints fail with
`ocr_ollama_url_not_loopback` (malformed URLs use `ocr_ollama_url_invalid`).
There is no remote opt-in in this version. Doctor applies the same policy.

Location configuration (`openmd.local/v1`) can include:

```json
{
  "schema": "openmd.local/v1",
  "ocr_python": "E:/path/to/external-ocr-env/Scripts/python.exe",
  "model_roots": ["E:/path/to/existing-models"],
  "ollama_url": "http://127.0.0.1:18499",
  "soffice": "C:/Program Files/LibreOffice/program/soffice.exe",
  "ocr": {
    "ovis_model": "ovis3-bf16",
    "minicpm_model": "minicpm-ocr-f16",
    "repeat_min_chars": 15,
    "repeat_count": 3,
    "length_multiplier": 2.0,
    "num_predict": 8192,
    "page_seconds": 600
  }
}
```

Merge these fields into existing configuration; retain its ASR/tool fields.
`OPENMD_CONFIG` selects a configuration file. Environment overrides are
`OPENMD_OLLAMA_URL`, `OPENMD_OCR_OVIS_MODEL`, `OPENMD_OCR_MINICPM_MODEL`,
`OPENMD_OCR_PYTHON`, `OPENMD_MODEL_ROOTS`, `OPENMD_OCR_LAYOUT_MODEL` and
`OPENMD_SOFFICE`. The layout folder is `layout` or `PP-DocLayoutV3` under a model
root, or a root containing its `config.json` and safetensors. A fully installed
external torch/transformers/torchvision/Pillow/NumPy environment is required for
PP-DocLayoutV3. Detection runs in a separate CPU process with local files only;
torch is not added to the main Openmd environment. A missing/broken runtime
records `fallback_unavailable` and `partial` if fallback is needed.
CPU-only is a runtime contract: the controller passes `CUDA_VISIBLE_DEVICES=-1`
(an empty value can leave CUDA visible on Windows). The layout worker refuses
visible CUDA instead of using the GPU. Doctor probes the actual torch runtime
and reports `device=cpu`, `cpu_only=true`, `cuda_visible=false`, and
`gpu_lease_inherited=false`.

### Windows 本机部署示例

本机可复用已经准备好的外部 OCR 环境和本地版面模型路径：

```powershell
$env:OPENMD_OLLAMA_URL = 'http://127.0.0.1:18499'
$env:OPENMD_OCR_PYTHON = 'D:\ocr\venv\Scripts\python.exe'
$env:OPENMD_MODEL_ROOTS = 'D:\ocr\models'
```

该 `venv` 是保留的部署外部环境，提供 PP-DocLayoutV3 所需的 torch、
transformers、torchvision、Pillow 和 NumPy；模型根目录下的 `layout/` 存放
PP-DocLayoutV3 本地权重。产品代码不硬编码上述 `out\ocr-benchmark` 路径，
部署到其他机器时应改用等价的 `OPENMD_OCR_PYTHON` 与
`OPENMD_MODEL_ROOTS`（或 `openmd.local/v1` 中的 `ocr_python`、`model_roots`）
配置。Ollama 仍由用户自行启动，Openmd 只读取配置的服务和模型元数据。

## Usage

```powershell
$env:OPENMD_OLLAMA_URL = 'http://127.0.0.1:18499'
openmd doctor --profile documents
openmd ingest 'E:\inputs\long.png' --out 'E:\outputs\image' --raw --enable-network-backends
openmd ingest 'E:\inputs\mixed.pdf' --out 'E:\outputs\pdf' --raw --enable-network-backends
openmd ingest 'E:\inputs\slides.pptx' --out 'E:\outputs\office' --raw --enable-network-backends
openmd routes --source screenshot --format json
# Diagnose one engine explicitly:
openmd ingest 'E:\inputs\page.png' --out 'E:\outputs\mini' --backend ocr.minicpm --no-fallback --raw --enable-network-backends
```

The existing network backend flag is required to contact Ollama, including a
loopback endpoint. Native PDF text extraction needs no network. Doctor reads
only the configured server's version/tags and local dependency metadata, never
loads OCR models; its documents profile reports readiness of both OCR engines
and the fallback runtime, plus optional MarkItDown/LibreOffice availability.

PDF routing is `pdf.markitdown → pdf.textlayer → ocr.chain`. Before accepting
MarkItDown's PDF result, every page must have a reliable text layer. Unreliable
pages proceed to the page router: reliable pages use PyMuPDF text; other pages
are rendered at 144 DPI and recognized in source order. A page number over a
large scan does not count as a reliable text layer. These checks are heuristic;
missing glyphs/odd hidden text layers still require source review. Mixed
documents use `source_level=ocr`, with each page's route retained in metadata.

User PDF text layers use a conservative whole-page policy. Any clip, invisible
rendering, detected low-contrast character, overlapping image or later paint,
annotation/widget overlap, out-of-page character or failed visibility probe
forces OCR with per-page reasons. Even a thin or translucent cover counts.
Adjacent text is checked using nonspace character boxes; padded paint boxes or
spaces cannot alone make neighboring words into occluders. Drawing inspection
is supplemented by content-stream checks, including nested Form XObjects,
because the drawing device omits text clipping modes (`Tr 4–7`). Unknown or
malformed operators and inline-image payloads are conservatively unproven.
This can route an otherwise readable user PDF through OCR.

Normal fill+stroke (`Tr2`) is one drawing even though MuPDF emits separate fill
and stroke traces. Their mutual overlap is exempt only with the same retained
interpreter text object, transform and adjacent drawing sequence, with parsed
source text-show count equal to paired device paint count. Forms/Type3 expansion
does not grant this exemption. Independent
draws stay subject to occlusion checks even with identical text/coordinates.
`Tr6` still forces OCR because it also clips text.
One interpreter text buffer can contain several source text-show operations;
overlapping nonadjacent nonspace glyphs inside it still reject the user PDF page.
Ordinary adjacent kerning is preserved. Shared
buffer identity only pairs fill/stroke and never authorizes internal overdraw.

Image coverage at or above `textlayer_image_coverage` (default `0.5`) also forces
OCR. At 72 DPI the final raster must contain contrasting ink inside each glyph
box; surviving pixels do not waive a clip or occlusion. The old
`textlayer_hidden_ratio` setting remains accepted for configuration compatibility
and no longer permits any detected low-contrast characters through.

Glyph/vector alternatives are accepted only for PDFs Openmd itself generated
from DOCX/PPTX in this invocation. The converter passes a private in-process
capability bound to the temporary absolute path and SHA256. A PDF's producer
metadata or supplied input metadata cannot grant this exemption. This retains
LibreOffice outlined formula operators and same-origin ligature continuation
characters without model calls. XLSX and user-supplied PDFs have no exemption.
Per-page receipts retain `textlayer_origin`, clip/overlap/rejection metrics,
`vector_alternative_text_chars` and `office_glyph_alternative_text_chars`.
These checks do not certify font mappings or transcription accuracy.

Office DOCX/PPTX/XLSX keeps MarkItDown by default. Empty text, or fewer than 200
visible alphanumeric characters with images/charts/SmartArt in the OOXML
container, triggers recovery: existing LibreOffice converts to PDF with an
isolated temporary profile, then the same page router extracts it. When recovery
is missing/fails, retain usable native text, record the reason and mark partial.
Long documents with enough text can still lose individual visual objects; asset
export and reliable reconstruction of chart semantics are outside this scope.
Office recovery reports actual page routes and engines in
`office_recovery.page_extractions`; native pages list `PyMuPDF`, not an OCR
engine that was never used.

For DOCX recovery whose pages all have reliable native text, supported source
OMML formulas can restore fractions, subscripts, superscripts and large-operator
limits lost in PDF text extraction. A formula is replaced only if its original
characters match a unique native text interval, ignoring whitespace alone,
without crossing blank lines or page boundaries. Any `mc:AlternateContent`,
revision, field code, hidden or dynamic content in any Word XML/VML part disables
OMML restoration for the whole document and records `disabled_reasons`.
Unknown structures, changed symbols or ambiguous matches stay unchanged. This
does not call a model or correct the source. Counts and the matching rule are
recorded in `office_recovery.native_formula_restoration`; source notes identify
the source-backed restoration. Mixed OCR/native pages do not use this step.

Recovery first requires a positively licensed OOXML source path: main
`document/body`, optionally nested `tbl/tr/tc`, then `p/oMath` or
`p/oMathPara/oMath`. Run-level inline math is a sibling of `w:r` in `w:p`;
`w:r/oMath` is not licensed. Header/footer roots may contain the same paths but
do not supply body replacement candidates. This bounded grammar does not yet
license textbox/shape wrappers. Formulas in properties, unknown/extension
ancestors, ignored namespaces, unapproved parts/roots or duplicate source parts
disable recovery for the whole file, with reasons. Native text remains intact.
XML source parts are inspected even when they have a non-XML filename suffix.

Before LibreOffice starts, Openmd parses every OOXML `*.rels` part and its
referring source. An external relationship is exempt only when it is referenced
exclusively as a text-only `w:hyperlink r:id`, with no use by another element.
Relationship Type is never permission: a picture/object/style borrowing a
hyperlink relationship still blocks conversion. Unknown, unreferenced or
duplicate relationship IDs also block. Word `INCLUDEPICTURE` / `INCLUDETEXT` /
`LINK` resource fields are conservatively blocked for nested, dynamic, network
or unknown targets, including split instructions and single-backslash paths.
Only explicit local-drive INCLUDE targets are exempt.
The default result is `partial` / `office_external_resource_blocked`, with
relationship/field types and destination hostnames in receipts; usable native text is preserved. Malformed
relevant XML fails closed as `office_external_resource_check_failed`.
Exempt pure hyperlinks remain text and do not trigger resource fetching.
**Passing `--enable-network-backends` explicitly also permits LibreOffice to
fetch Office external resources**, in addition to enabling network backends and
local Ollama OCR. Office resource destinations may be remote; the Ollama endpoint
itself still must be loopback, with no remote override.

When external-resource conversion is started with this flag, receipts record
`office_external_resources_fetched=true`, relationship/field types, and
`target_hosts` (hostnames only, without credentials, ports, paths or query strings).
`source_notes` states that the content depends on external resources. The flag
records that resource fetching was permitted during conversion; LibreOffice does
not provide a per-resource fetch-success receipt, so it does not certify that
every linked resource was available. Conversion failures retain this provenance.
Without the flag, external conversion never starts and the fetched value is
false. Local embedded content still uses the ordinary converter path.

## Breakers, tiling and GPU ownership

Defaults are packaged in `src/openmd/ocr/settings.json` and can be overridden in
the `ocr` config object. Three equal normalized sentences of at least 15 chars
trip the repetition breaker, excluding formula-only fragments. Output exceeding
twice the visible-text capacity estimated from pixels trips the length breaker;
no reference answer is read. Generation is bounded at 8192 predicted tokens and
600 seconds for the page including its slices and fallback (GPU wait is separate).

Tall screenshots above 1440 px and aspect ratio 2 use blank-row cuts, 18% overlap
and an overlap-aware raw text merge. Ordinary page/slide images remain whole;
rendered PDF pages always remain whole. Inconsistent seams and failed slices
are partial. Rejected output is retained under ignored `out/ocr-router/breaker-evidence`
in a checkout, or the user's Openmd runtime directory after installation; receipts
contain its path, never the rejected body.
Overlap candidate search is limited by the actual shared image band, using a
monotonic text-line projection estimate because OCR does not return word
coordinates. Candidate normalization grants no deletion rights. Each deleted
non-whitespace B character must map to an equal original A character. The only
permitted equivalences are whitespace and these explicit fullwidth punctuation
characters mapped to their ASCII counterparts:

```text
！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～
!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~
```

Letters, numbers, literal operators, HTML attributes, formulas, ligatures and
combining Unicode sequences have no other deletion equivalence. Each seam
records the raw character mapping and `deletion_mapping_coverage`.

An uncertain seam triggers at most one additional diagnostic OCR pass over the exact
shared image strip, with the same engine inside the same GPU lease/page budget.
The original A tile, B tile, accumulated A alignment input, arbiter and alignment
are retained under ignored `out/ocr-router/seam-evidence/`; receipts contain
paths, hashes, coordinates and metrics. Matching the arbiter to one reading
cannot authorize deleting a different raw character from the other reading.
Such conflicts stay partial (`arbitration_not_deletion_authority`). The current
arbiter supplies evidence and agreement metrics; it does not resolve differing
characters or guarantee fewer partial results.

Fully equivalent overlaps are merged once. Conflicts expand to complete
Markdown blocks before both readings are retained: whole lines/paragraphs,
tables, list items and code blocks. Existing seam wrappers remain indivisible.
Equal characters inside a conflicting block are deliberately retained twice;
that block has zero deletion mappings. Labels are separated with blank lines:

```markdown
<!-- openmd:seam-diff start -->

[openmd variant A]

first reading of the differing part

[openmd variant B]

second reading of the differing part

<!-- openmd:seam-diff end -->
```

These labels describe alternative OCR readings, not extra source content. If
the seam cannot be aligned uniquely, its estimated physical overlap band is
expanded to block boundaries and wrapped as an `unresolved_overlap_band`
difference block with both readings and zero deletions. No alignment is invented.
A merge call without geometry retains both complete inputs in the same safe
format. Already unclosed fences are displayed as explicitly labeled literal
Markdown inside a longer outer fence, preserving the original characters.
Both cases stay partial.
Balanced HTML containers/comments are also atomic across blank lines, including
Openmd's OTSL HTML tables and `pre`/`details` blocks. Unknown, mismatched or
truncated HTML is explicitly displayed as literal Markdown inside a longer
fence; labels remain outside the original containers. Source code fences are
masked during HTML boundary parsing. These rules also apply to later tiles.
Numeric or symbol conflicts cannot silently become verified.

OCR and local ASR share the OS file lock `~/.openmd/runtime/gpu.lock` across
processes (`OPENMD_RUNTIME_DIR` relocates it). Waits are bounded by 600 seconds
or `OPENMD_GPU_LOCK_TIMEOUT` (OCR also has `gpu_lock_seconds`) and support Ctrl+C.
The OS releases crashed owners; the harmless persistent file is reused, never
deleted. Ovis is unloaded with `keep_alive: 0` before MiniCPM starts; each page/
tile unloads its model before releasing the lock. A failed unload prevents model
switching and terminates all remaining tiles/pages of the current file with
`ollama_unload_unconfirmed`. Before inference a durable `gpu-quarantine.json`
marker is written beside the GPU lock (pid, model, endpoint and time). Only a
confirmed unload clears it. Cancellation also retains the marker. The next
OCR or ASR client takes the OS lease first and only reads `/api/ps`. Empty means
the marker can clear. Nonempty means wait within the lock timeout for the user
client or Ollama keep_alive expiry; timeout returns `gpu_external_busy` without
loading or unloading any model. Even a matching model name cannot prove request
ownership. Unreachable/invalid service state keeps the marker and returns
`gpu_quarantined`. Openmd unloads only after its own model request; layout failure
or an all-image layout does not authorize a model unload.

For an explicit marker-only reset (including a corrupt marker), run:

```powershell
openmd doctor --clear-gpu-quarantine
```

This command takes the same OS lock and only deletes the local marker. It never
contacts Ollama or unloads models. Check the shared GPU/service state yourself
before starting inference after a manual reset; it is an explicit override of
the uncertainty marker, not evidence that the GPU is free.
This lease coordinates Openmd processes, not other GPU applications.
The ASR inference worker itself holds the lease, so a surviving worker remains
protected even if its controller crashes. In-process ASR holds it locally.

## Receipts, status and limitations

V0 runlogs retain `document_metadata.pages`; packages also retain extraction
metadata. Each OCR page records engine/model/digest, fallback/reason, both stage
durations, optional observed peak device memory, Han ratio, slicing/merge data
and breaker records. Native pages record their native route. Missing memory
measurements are `null`, never fabricated.

Normal OCR and a completed fallback are `verified` (execution completed, not a
certification of transcription accuracy). Double breakers, unavailable fallback,
failed slices, uncertain merges or unrecovered Office loss are `partial`.
An intentional partial result remains terminal in the route chain; a later
traditional engine cannot erase its coverage gaps. Complete dependency/gate
failures before recognition can continue to the ordinary route fallbacks.

Source notes always include model identities, fallback and these warnings:
**识别模型可能自动纠正原文错字**; **`fallback_used=false` 不代表无幻觉**.
Single inventions, wrong numbers, wrong order and local omissions are not
detected by the breakers. Chart/image bbox tags do not point to exported assets.
Read and compare actual output for important material.

`routes bench` accepts existing local screenshot/PDF samples and standalone OCR
engine candidates with explicit gates. Benchmark trial metadata retains digests
and page receipts. `demote` operates on entries in the declared route (for OCR,
`ocr.chain`); its existing two-trial, verified-tool-failure evidence gate remains.
Partial OCR or uncertain quality is not silently reclassified as such a failure.

Provide a sample registry with `--samples-file samples.json` and an output
directory with `--artifacts out/ocr-bench`. Each sample uses the existing
`openmd.route-samples/v1` schema (`id`, `source`, `capability`, `url`); for local
PDF/images, `url` may be an existing absolute path or `file://` URI. Example:

```powershell
openmd routes bench --source screenshot --capability content --sample page --samples-file out/ocr-samples.json --candidates ocr.ovis3,ocr.minicpm,ocr.chain --ledger out/ocr-trials.jsonl --artifacts out/ocr-bench --enable-network-backends
```

Implementation/real sample evidence: `docs/agent-runs/2026-11-08-ocr-router/`.
The 2026-11-09 repair verification is in `fix2-report.md` and
`evidence/real-verify-fix2.md`; its nine-case CLI matrix records empty Ollama
state after each sample. Uncertain long-image seams remain partial.

Daily-use hardening batch B evidence and current limits are recorded in
`docs/agent-runs/2026-11-09-daily-use-hardening/B-implement-report.md`.
