"""Owned streaming dictation on CPU: voice activity detection plus growing-window decoding.

Parakeet TDT 0.6B v3 (NVIDIA, CC-BY-4.0, https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
is an offline model. It is driven here the usual way for such models: Silero VAD (MIT)
splits the stream into utterances; while an utterance grows, the whole utterance is decoded
again at most every PARTIAL_INTERVAL seconds and shown as a partial; at the end of the
utterance one last decode gives the final text. A decode never queues behind another one of
the same stream, so a slow CPU lowers the partial rate instead of adding delay.

An utterance closes after END_SILENCE seconds of silence: 1.2 s, not 0.6 s. Each utterance is
decoded alone, so a thinking pause of 0.8 s split sentences and lost words: on the dictation
pilot set (2026-11-17) the word error rate with such pauses fell from 35.6 % to 10.9 % in
French and from 80.3 % to 27.4 % in English. The live text is only shown; the text written
for the user comes from transcribe_windows(), one pass over the whole recording at stop in
windows of up to WINDOW_MAX seconds cut at the longest pauses.

Weights are the INT8 ONNX export by istupakov (https://huggingface.co/istupakov/
parakeet-tdt-0.6b-v3-onnx), loaded with the onnx-asr library (MIT). Both are optional
dependencies, installed separately: without them the engine reports itself unavailable.
Audio stays in this process memory and is never stored or logged.
"""

import asyncio
import importlib
import importlib.util
import os
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
VAD_HOP = 512
VAD_CONTEXT = 64
PARTIAL_INTERVAL = float(os.environ.get("ATLAS_DICTATION_LOCAL_PARTIAL_S", "0.6"))
END_SILENCE = float(os.environ.get("ATLAS_DICTATION_LOCAL_END_SILENCE_S", "1.2"))
# Parakeet accepts up to about 20 s per call in onnx-asr; long speech is cut at a pause
# after SOFT_LIMIT seconds, and at HARD_LIMIT in any case.
SOFT_LIMIT = 12.0
HARD_LIMIT = 20.0
SPEECH_ON, SPEECH_OFF = 0.5, 0.35
PAD = int(0.2 * SAMPLE_RATE)
# Final pass windows: up to WINDOW_MAX seconds, cut at the longest pause found after
# WINDOW_MIN seconds (more context per decode keeps the language and sentence starts).
# Swept on the pilot set (2026-11-17): 20/8 s beat 15/6, 12/5, 10/4 and 8/3 in French
# and English. A window the model returns empty although it holds speech (an 18 s window of
# English with thinking pauses did) is split at its longest pause and decoded again.
WINDOW_MAX = HARD_LIMIT
WINDOW_MIN = 8.0
RETRY_MIN = 2.0


@dataclass(frozen=True)
class LocalEvent:
    kind: str
    text: str = ""
    time_s: float | None = None
    seconds: float | None = None


class _Models:
    """Process-wide model instances, loaded once on first use."""

    lock = threading.Lock()
    asr: Any = None
    vad_path: Path | None = None
    decodes: threading.BoundedSemaphore | None = None

    @classmethod
    def load(cls) -> None:
        with cls.lock:
            if cls.asr is not None:
                return
            onnx_asr = importlib.import_module("onnx_asr")
            runtime = importlib.import_module("onnxruntime")
            options = runtime.SessionOptions()
            options.intra_op_num_threads = int(
                os.environ.get("ATLAS_DICTATION_LOCAL_THREADS", "2")
            )
            options.inter_op_num_threads = 1
            cls.asr = onnx_asr.load_model(
                "nemo-parakeet-tdt-0.6b-v3",
                os.environ.get("ATLAS_DICTATION_LOCAL_MODEL_DIR") or None,
                quantization="int8",
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            vad = os.environ.get("ATLAS_DICTATION_LOCAL_VAD")
            if vad:
                cls.vad_path = Path(vad)
            else:
                hub = importlib.import_module("huggingface_hub")
                cls.vad_path = Path(
                    hub.hf_hub_download("istupakov/silero-vad-onnx", "silero_vad.onnx")
                )
            cls.decodes = threading.BoundedSemaphore(
                int(os.environ.get("ATLAS_DICTATION_LOCAL_STREAMS", "2"))
            )


def available() -> bool:
    """True when the optional dependencies are importable and the configured files exist.

    A deployment names its model directory and VAD file (ATLAS_DICTATION_LOCAL_MODEL_DIR,
    ATLAS_DICTATION_LOCAL_VAD) so that no request downloads weights; without them the
    weights come from the Hugging Face cache (development).
    """
    model = os.environ.get("ATLAS_DICTATION_LOCAL_MODEL_DIR")
    vad = os.environ.get("ATLAS_DICTATION_LOCAL_VAD")
    return (
        all(
            importlib.util.find_spec(name) is not None
            for name in ("onnx_asr", "onnxruntime", "huggingface_hub")
        )
        and (not model or Path(model).is_dir())
        and (not vad or Path(vad).is_file())
    )


def warm_up() -> None:
    """Load the model ahead of the first stream; a failure is left to that stream."""
    try:
        _Models.load()
    except Exception:  # noqa: BLE001 - LocalStream.connect reports the failure
        return


class _Vad:
    """Streaming Silero VAD: one probability per 32 ms frame, state kept between calls."""

    def __init__(self) -> None:
        runtime = importlib.import_module("onnxruntime")
        options = runtime.SessionOptions()
        options.intra_op_num_threads = 1
        self.session = runtime.InferenceSession(
            str(_Models.vad_path), options, providers=["CPUExecutionProvider"]
        )
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros(VAD_CONTEXT, dtype=np.float32)

    def __call__(self, frame: np.ndarray[Any, np.dtype[np.float32]]) -> float:
        window = np.concatenate([self.context, frame])[None, :]
        output, self.state = self.session.run(
            ["output", "stateN"],
            {
                "input": window,
                "state": self.state,
                "sr": np.array([SAMPLE_RATE], dtype=np.int64),
            },
        )
        self.context = frame[-VAD_CONTEXT:]
        return float(output[0, 0])


def _decode(samples: np.ndarray[Any, np.dtype[np.float32]]) -> str:
    assert _Models.decodes is not None
    with _Models.decodes:
        return str(_Models.asr.recognize(samples, sample_rate=SAMPLE_RATE)).strip()


class LocalStream:
    """One dictation stream on the owned CPU engine; same surface as the cloud stream."""

    def __init__(self, decode: Callable[[Any], str] = _decode) -> None:
        self.decode = decode
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self.sent_bytes = 0
        self.vad: Callable[[np.ndarray[Any, np.dtype[np.float32]]], float] | None = None
        # One speech probability per VAD_HOP samples, kept for the final pass windows.
        self.speech: list[float] = []

    async def connect(self, *, language: str | None, **_: object) -> None:
        # Parakeet v3 detects the language itself; the requested one is not forced.
        await asyncio.to_thread(_Models.load)
        self.vad = await asyncio.to_thread(_Vad)

    async def send(self, pcm: bytes) -> None:
        self.sent_bytes += len(pcm)
        await self.queue.put(pcm)

    async def finish(self) -> None:
        await self.queue.put(None)

    async def events(self) -> AsyncIterator[LocalEvent]:
        vad = self.vad
        if vad is None:
            raise RuntimeError("engine_not_connected")
        pending = np.zeros(0, dtype=np.float32)
        utterance: list[np.ndarray[Any, np.dtype[np.float32]]] = []
        history = np.zeros(0, dtype=np.float32)  # pre-roll before speech starts
        position = 0  # samples seen
        speaking = False
        silence = 0.0
        quiet_at = 0  # samples into the utterance at the latest quiet frame
        decoding: asyncio.Task[str] | None = None
        last_partial = 0.0
        loop = asyncio.get_running_loop()

        def joined() -> np.ndarray[Any, np.dtype[np.float32]]:
            return np.concatenate(utterance) if utterance else pending[:0]

        async def final(samples: np.ndarray[Any, np.dtype[np.float32]]) -> LocalEvent:
            text = await asyncio.to_thread(self.decode, samples)
            return LocalEvent("final", text, time_s=position / SAMPLE_RATE)

        ended = False
        while not ended:
            chunk = await self.queue.get()
            if chunk is None:
                ended = True
            else:
                pending = np.concatenate(
                    [
                        pending,
                        np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768,
                    ]
                )
            while len(pending) >= VAD_HOP:
                frame, pending = pending[:VAD_HOP], pending[VAD_HOP:]
                position += VAD_HOP
                probability = vad(frame)
                self.speech.append(probability)
                if not speaking:
                    history = np.concatenate([history, frame])[-PAD:]
                    if probability >= SPEECH_ON:
                        speaking, silence = True, 0.0
                        utterance = [history.copy()]
                        quiet_at = 0
                        yield LocalEvent("turn_start", time_s=position / SAMPLE_RATE)
                    continue
                utterance.append(frame)
                length = sum(len(part) for part in utterance) / SAMPLE_RATE
                if probability < SPEECH_OFF:
                    silence += VAD_HOP / SAMPLE_RATE
                    quiet_at = sum(len(part) for part in utterance)
                else:
                    silence = 0.0
                cut = None
                if silence >= END_SILENCE or length >= HARD_LIMIT:
                    cut = sum(len(part) for part in utterance)
                elif length >= SOFT_LIMIT and silence >= 0.2:
                    cut = quiet_at
                if cut is not None:
                    if decoding is not None:
                        decoding.cancel()
                        decoding = None
                    samples = joined()
                    yield await final(samples[:cut])
                    rest = samples[cut:]
                    if silence >= END_SILENCE:
                        speaking = False
                        history = rest[-PAD:]
                        utterance = []
                        yield LocalEvent("turn_end", time_s=position / SAMPLE_RATE)
                    else:
                        utterance = [rest]
                    continue
            if decoding is not None and decoding.done():
                if not decoding.cancelled():
                    text = decoding.result()
                    if text:
                        yield LocalEvent("partial", text)
                decoding = None
            if (
                speaking
                and decoding is None
                and not ended
                and loop.time() - last_partial >= PARTIAL_INTERVAL
            ):
                last_partial = loop.time()
                decoding = asyncio.ensure_future(
                    asyncio.to_thread(self.decode, joined())
                )
        if decoding is not None:
            decoding.cancel()
        if speaking and utterance:
            yield await final(joined())
            yield LocalEvent("turn_end", time_s=position / SAMPLE_RATE)
        yield LocalEvent("done", seconds=position / SAMPLE_RATE)

    async def aclose(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()


def plan_windows(speech: list[float], total: int) -> list[tuple[int, int]]:
    """Sample ranges of the final pass: windows of WINDOW_MIN to WINDOW_MAX seconds, each
    cut in the middle of the longest pause of that span, silent windows left out.

    ``speech`` holds one probability per VAD_HOP samples; ``total`` is the sample count.
    """
    frames = -(-total // VAD_HOP)
    speech = (speech + [0.0] * frames)[:frames]
    quiet = [value < SPEECH_OFF for value in speech]
    longest = int(WINDOW_MAX * SAMPLE_RATE) // VAD_HOP
    shortest = int(WINDOW_MIN * SAMPLE_RATE) // VAD_HOP
    cuts, start = [], 0
    while frames - start > longest:
        best, best_length, run = start + longest, 0, 0
        for frame in range(start + 1, start + longest + 1):
            run = run + 1 if quiet[frame - 1] else 0
            middle = frame - run // 2
            if run and middle >= start + shortest and run > best_length:
                best, best_length = middle, run
        cuts.append(best)
        start = best
    windows = []
    for first, last in zip([0, *cuts], [*cuts, frames], strict=True):
        if max(speech[first:last], default=0.0) < SPEECH_ON:
            continue
        spoken = [k for k in range(first, last) if speech[k] >= SPEECH_OFF]
        begin = max(first * VAD_HOP, spoken[0] * VAD_HOP - PAD)
        end = min(last * VAD_HOP, (spoken[-1] + 1) * VAD_HOP + PAD, total)
        windows.append((begin, end))
    return windows


def longest_pause(speech: list[float], begin: int, end: int) -> int | None:
    """Sample index in the middle of the longest pause of a range, leaving at least
    RETRY_MIN seconds on each side; None when there is no such pause."""
    margin = int(RETRY_MIN * SAMPLE_RATE) // VAD_HOP
    first, last = begin // VAD_HOP + margin, min(end // VAD_HOP, len(speech)) - margin
    best, best_length, run = None, 0, 0
    for frame in range(first, last):
        run = run + 1 if speech[frame] < SPEECH_OFF else 0
        if run > best_length:
            best, best_length = frame - run // 2, run
    return None if best is None else best * VAD_HOP


def speech_probabilities(samples: np.ndarray[Any, np.dtype[np.float32]]) -> list[float]:
    """Speech probability of each VAD_HOP frame of a recording (the live stream's VAD)."""
    _Models.load()
    vad = _Vad()
    usable = len(samples) - len(samples) % VAD_HOP
    return [vad(samples[k : k + VAD_HOP]) for k in range(0, usable, VAD_HOP)]


async def transcribe_windows(
    pcm: bytes,
    speech: list[float] | None = None,
    decode: Callable[[Any], str] = _decode,
) -> str:
    """The whole recording decoded in long windows: the text written at stop.

    ``speech`` reuses the live stream's VAD probabilities; without them the VAD runs again.
    Windows are decoded concurrently, within the process-wide decode limit.
    """
    samples = (np.frombuffer(pcm, dtype="<i2") / 32768).astype(np.float32)
    if speech is None:
        probabilities = await asyncio.to_thread(speech_probabilities, samples)
    else:
        probabilities = speech
        if decode is _decode:
            await asyncio.to_thread(_Models.load)

    async def window(begin: int, end: int) -> str:
        text = (await asyncio.to_thread(decode, samples[begin:end])).strip()
        cut = None if text else longest_pause(probabilities, begin, end)
        if cut is None:
            return text
        halves = await asyncio.gather(window(begin, cut), window(cut, end))
        return " ".join(half for half in halves if half)

    texts = await asyncio.gather(
        *(
            window(begin, end)
            for begin, end in plan_windows(probabilities, len(samples))
        )
    )
    return " ".join(text for text in texts if text)
