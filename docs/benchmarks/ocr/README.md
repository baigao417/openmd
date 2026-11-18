# Local OCR comparison

The maintainer benchmark reuses the frozen 2026-11-08 twelve-page packet:
six Chinese and six English independently annotated OmniDocBench pages from
dataset revision `aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec`.
The packet and run receipts are maintained outside the public package.
No private documents or model weights belong in public fixtures.

## Frozen procedure

- Reuse the original manifest, metrics protocol, `run_transformers.py`,
  `score_benchmark.py`, `table_diagnostics.py` and `verify_final.py`;
  preserve hashes and raw predictions.
- Read archived baseline receipts. Run new checkpoints and missing pages of
  previously incomplete contenders. Do not run old baselines again.
- Follow each model's official prompt and page or layout/crop input method.
  Record adaptations separately: quantization, pixel limits, backend, cold
  load, warm latency, memory and actual GPU offload.
- Use the shared GPU lease and a separate Ollama service on loopback port
  18499. Models are local; no cloud OCR. Unload only models requested by the
  benchmark and stop the owned service after the run.

Quality weights are text 60, table 20, formula 10 and reading order 10, with
absent categories renormalized by the original scorer. Overall is quality 80%
plus speed 20%. Chinese and English have separate rankings. Every expected
page remains in the denominator; failed or missing pages receive zero.
Acquisition/runtime blockers are reported separately from measured OCR
failures. Neither supports a complete winner claim.

These are local proxies, not official OmniDocBench scores, global CER, TEDS or
rendered equation equivalence. English spacing, numeric cell coordinates,
omissions, repetitions and unannotated image text require source comparison.
Preserve annotation errors and record them in manual review; never replace
frozen references with model predictions.

Speed normalization depends on the contender set. Recompute scores from
unchanged archived measurements when adding candidates; keep CPU offload
runs outside the full GPU speed comparison. Historical published scores
remain identifiable. Recommend a new default only when both quality and the
80/20 score improve clearly, with complete pages and manual review.

## Hardware plans

`openmd doctor` reports locally observed hardware and separate OCR/ASR evidence.
Evidence applies to the named tested machine and component; other machines
in the same memory range remain unverified.

`openmd models install ocr:balanced --root <model-directory>` prints a JSON
plan with model identities, sizes and destinations. It does not create
directories, download files or select the model automatically. Review exact
model and runtime licenses before installation.
