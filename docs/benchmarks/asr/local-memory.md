# Local ASR physical-memory estimates

Measured on Windows 11, 16 GiB RAM, RTX 4060 Laptop 8 GiB, torch 2.5.1+cu124,
transformers 4.57.6. RSS is the process working set; private commit and GPU memory
are different metrics. These estimates guide admission and do not guarantee a
fixed maximum on every device, driver, dependency version or input.

## Admission and warnings

- Available physical memory **below the estimated peak** refuses model loading
  with `asr_insufficient_memory`. Original Qwen weights suggest
  `openmd asr prepare-models`; refusal does not start another local model or upload
  audio to a cloud service.
- Memory between the peak and peak + margin allows loading and records the
  advisory `asr_memory_tight` code and a result warning. The default 1024 MiB
  margin only sets this warning threshold.
- `memory_required_bytes` is the hard peak threshold;
  `memory_warning_threshold_bytes` is peak + margin. Language-probe metrics are
  retained separately under `language_probe_memory` in ASR execution receipts.

## Estimate sources (2026-11-17, round 8)

All receipt names below identify private maintainer evidence; raw source audio,
transcripts and machine paths are not included in the public package.

| Stage | Default estimate MiB | Observed RSS MiB | Source receipt / scope |
| --- | ---: | ---: | --- |
| Original Qwen3-ASR-1.7B | 5632 | 5389.684 | Round7 `short-receipt.json`, historical short real CLI. New original attempts were refused, so this is a historical baseline. |
| Prepared Qwen3-ASR-1.7B | 2304 | 2223.660 | Round8 `long-prepared-receipt.json`, 35-minute real CLI; `clip-prepared-fixed-receipt.json` observed 1889.301 MiB. |
| Automatic language probe | 2496 | 2248.270 full probe; 2264.098 earlier model load | Round8 `samples/rework-auto-clip/phases.jsonl` (`language_probe_full`), and earlier `samples/auto-prepared/phases.jsonl` (`load_whisper`). Estimate is the higher observation plus about 10%, rounded up to 64 MiB. |

The probe loads **faster-whisper / CTranslate2 `large-v3-turbo`**, uses CUDA
`int8_float16` on this machine, calls `transcribe(vad_filter=True, beam_size=1)`
for language metadata, and releases that model before Qwen3 transcription. It
does not load `large-v3` for this default probe. The full probe measurement
includes torch/CTranslate2 imports, construction, language detection and model
release. The Qwen3 part then uses the existing FunASR VAD and prepared weights.

The earlier 2560 MiB probe floor was conservative relative to the 2264 MiB load
measurement; the full phase measurement supports a 2496 MiB floor. Earlier
peak+1024 hard refusal was an availability regression and is superseded by the
peak-only rule above. A normal-memory default auto run succeeded starting with
3003.875 MiB available, without closing programs or overriding estimates.

For larger model files, defaults scale with the largest weight file: at least
file MiB + 1536 for Qwen3 and file MiB + 768 for the probe. These additions are
conservative sizing heuristics, not measured guarantees for arbitrary models.
Explicit positive MiB peak and margin settings retain their existing meanings
as configurable estimates and advisory headroom. See the subsystem guide for
configuration names.
