"""Owned streaming dictation on CPU: voice activity detection plus growing-window decoding.

Parakeet TDT 0.6B v3 (NVIDIA, CC-BY-4.0, https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
is an offline model. It is driven here the usual way for such models: Silero VAD (MIT)
splits the stream into utterances; while an utterance grows, the whole utterance is decoded
again at most every PARTIAL_INTERVAL seconds and shown as a partial; at the end of the
utterance one last decode gives the final text. A decode never queues behind another one of
the same stream, so a slow CPU lowers the partial rate instead of adding delay.

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
END_SILENCE = float(os.environ.get("ATLAS_DICTATION_LOCAL_END_SILENCE_S", "0.6"))
# Parakeet accepts up to about 20 s per call in onnx-asr; long speech is cut at a pause
# after SOFT_LIMIT seconds, and at HARD_LIMIT in any case.
SOFT_LIMIT = 12.0
HARD_LIMIT = 20.0
SPEECH_ON, SPEECH_OFF = 0.5, 0.35
PAD = int(0.2 * SAMPLE_RATE)


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
            hub = importlib.import_module("huggingface_hub")
            cls.vad_path = Path(
                hub.hf_hub_download("istupakov/silero-vad-onnx", "silero_vad.onnx")
            )
            cls.decodes = threading.BoundedSemaphore(
                int(os.environ.get("ATLAS_DICTATION_LOCAL_STREAMS", "2"))
            )


def available() -> bool:
    """True when the optional dependencies are importable (the model may still download)."""
    return all(
        importlib.util.find_spec(name) is not None
        for name in ("onnx_asr", "onnxruntime", "huggingface_hub")
    )


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
