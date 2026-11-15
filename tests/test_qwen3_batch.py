"""Native batching contracts; these fakes do not establish ASR quality or speed."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import weakref

import pytest

from openmd.adapters.local_audio.backends.qwen3_asr import Qwen3ASRBackend


class Audio:
    def __getitem__(self, key):
        return key.start // 16


class CudaOOM(RuntimeError):
    pass


def fake_torch():
    return SimpleNamespace(cuda=SimpleNamespace(OutOfMemoryError=CudaOOM, empty_cache=Mock()))


class Model:
    def __init__(self, actions=None):
        self.calls = []
        self.actions = list(actions or [])
        self.max_inference_batch_size = 32

    def transcribe(self, *, audio, language):
        self.calls.append((audio, language))
        if self.actions:
            action = self.actions.pop(0)
            if isinstance(action, BaseException):
                raise action
            if action is not None:
                return action
        inputs = audio if isinstance(audio, list) else [audio]
        return [SimpleNamespace(text=f" text {piece} ", language="Chinese") for piece, _ in inputs]


def spans(count):
    return [[index * 30_000, index * 30_000 + 1_000] for index in range(count)]


def run(size, count, model, torch=None):
    backend = Qwen3ASRBackend(batch_size=size, language="Chinese")
    lines, languages = backend._transcribe_spans(model, Audio(), spans(count), torch or fake_torch())
    return backend, lines, languages


def test_native_batch_keeps_order_language_and_odd_tail():
    model = Model()
    backend, lines, languages = run(2, 5, model)
    assert lines == [f"[00:{index // 2:02d}:{index % 2 * 30:02d}] text {index * 30_000}" for index in range(5)]
    assert languages == ["Chinese"] * 5
    assert [len(audio) for audio, _ in model.calls] == [2, 2, 1]
    assert all(language == "Chinese" for _, language in model.calls)
    assert backend._run_metadata["batch_size_histogram"] == {"2": 2, "1": 1}
    assert backend._run_metadata["batch_size_effective"] == 2


def test_scalar_batch_keeps_original_tuple_path():
    model = Model()
    run(1, 2, model)
    assert [audio for audio, _ in model.calls] == [(0, 16_000), (30_000, 16_000)]


def test_blank_results_keep_original_timestamp_and_language_filtering():
    model = Model([[SimpleNamespace(text=" ", language="English"), SimpleNamespace(text=" kept ")]])
    _, lines, languages = run(2, 2, model)
    assert lines == ["[00:00:30] kept"]
    assert languages == ["unknown"]


def test_oom_halves_without_losing_completed_progress_or_duplicating():
    model = Model([None, CudaOOM(), CudaOOM()])
    torch = fake_torch()
    backend, lines, _ = run(4, 9, model, torch)
    attempted_inputs = [[piece for piece, _ in audio] if isinstance(audio, list) else [audio[0]] for audio, _ in model.calls]
    assert attempted_inputs[:4] == [[0, 30_000, 60_000, 90_000], [120_000, 150_000, 180_000, 210_000], [120_000, 150_000], [120_000]]
    assert len(lines) == len(set(lines)) == 9
    assert lines[-1] == "[00:04:00] text 240000"
    assert backend._run_metadata["batch_size_requested"] == 4
    assert backend._run_metadata["batch_size_effective"] == 1
    assert backend._run_metadata["batch_size_histogram"] == {"4": 1, "1": 5}
    assert backend._run_metadata["batch_oom_retries"] == 2
    assert backend._run_metadata["batch_oom_attempts"] == [4, 2]
    assert torch.cuda.empty_cache.call_count == 2


def test_oom_at_one_fails_without_retry():
    model = Model([CudaOOM()])
    torch = fake_torch()
    backend = Qwen3ASRBackend(batch_size=1)
    with pytest.raises(CudaOOM):
        backend._transcribe_spans(model, Audio(), spans(1), torch)
    assert len(model.calls) == 1
    assert backend._run_metadata["batch_oom_retries"] == 0
    assert backend._run_metadata["batch_oom_attempts"] == [1]


@pytest.mark.parametrize("error", [RuntimeError("unrelated error"), KeyboardInterrupt()])
def test_other_errors_and_cancellation_are_not_retried(error):
    model = Model([error])
    with pytest.raises(type(error)):
        run(4, 4, model)
    assert len(model.calls) == 1


@pytest.mark.parametrize("count", [0, 1, 3])
def test_wrong_result_cardinality_fails(count):
    model = Model([[SimpleNamespace(text="wrong", language="English")] * count])
    with pytest.raises(ValueError, match="cardinality_mismatch"):
        run(2, 2, model)


def test_batch_metadata_and_numerical_source_note_reach_transcript(tmp_path, monkeypatch):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    backend = Qwen3ASRBackend(batch_size=4)
    monkeypatch.setattr(backend, "is_available", lambda: True)

    def fake_run(_path):
        backend._run_metadata.update(batch_size_requested=4, batch_size_effective=2)
        return ["[00:00:00] hi"], ["English"], 1.0

    monkeypatch.setattr(backend, "_run", fake_run)
    result = backend.transcribe(path)
    assert result.metadata["batch_size_requested"] == 4
    assert result.metadata["batch_size_effective"] == 2
    assert any("numerical differences" in note for note in result.source_notes)


@pytest.mark.parametrize("fails", [False, True])
def test_loaded_model_is_released_before_return_without_real_gpu(tmp_path, monkeypatch, fails):
    import openmd.adapters.local_audio.backends.qwen3_asr as module

    refs = []
    cache_model_alive = []

    class LoadedModel:
        def transcribe(self, **kwargs):
            if fails:
                raise RuntimeError("inference failed")
            return [SimpleNamespace(text="hello", language="English")]

    def load_model(*args, **kwargs):
        assert kwargs["max_inference_batch_size"] == 2
        model = LoadedModel()
        refs.append(weakref.ref(model))
        return model

    torch = fake_torch()
    torch.bfloat16 = "bfloat16"
    torch.cuda.is_available = lambda: True
    torch.cuda.empty_cache = lambda: cache_model_alive.append(refs[0]() is not None)
    monkeypatch.setitem(module.sys.modules, "torch", torch)
    monkeypatch.setitem(module.sys.modules, "soundfile", SimpleNamespace(read=lambda *a, **k: ([0] * 16_000, 16_000)))
    monkeypatch.setitem(module.sys.modules, "funasr", SimpleNamespace(AutoModel=lambda **k: SimpleNamespace(generate=lambda **k: [{"value": [[0, 1000]]}])))
    monkeypatch.setitem(module.sys.modules, "qwen_asr", SimpleNamespace(Qwen3ASRModel=SimpleNamespace(from_pretrained=load_model)))
    monkeypatch.setattr(module, "require_model", lambda *a, **k: tmp_path)
    monkeypatch.setattr(module, "offline_model_loading", nullcontext)
    monkeypatch.setattr(module, "owned_temporary_directory", lambda **k: nullcontext(str(tmp_path)))
    monkeypatch.setattr(module, "run_interruptibly", lambda *a, **k: None)
    path = tmp_path / "audio.wav"
    path.write_bytes(b"audio")
    backend = Qwen3ASRBackend(batch_size=2)
    monkeypatch.setattr(backend, "is_available", lambda: True)
    result = backend.transcribe(path)
    assert result.status == ("failed" if fails else "verified")
    assert refs[0]() is None
    assert cache_model_alive[-1] is False
    assert result.metadata["elapsed_seconds"] >= result.metadata["inference_seconds"]
