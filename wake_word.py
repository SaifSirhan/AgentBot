"""Wake-word listener built on openWakeWord and PyAudio.

The pre-trained openWakeWord models are alexa, hey_mycroft, hey_jarvis,
hey_rhasspy, weather and timers. There is no "agent" model, so the default
``agent`` name resolves to a custom ``agent.onnx``/``agent.tflite`` in
%LOCALAPPDATA%\\AgentMemory\\wakeword or the repo's models/ directory. If that
file is missing the service refuses to start instead of listening for a
different phrase. Set ``WAKE_WORD_MODEL`` to a pretrained name (for example
``hey_jarvis``) to exercise the pipeline without a custom model.

The pip package does not ship the pretrained models; download them first with
``python -c "import openwakeword.utils as u; u.download_models()"``. ONNX is
preferred whenever onnxruntime is installed because tflite-runtime has no
Windows wheels.

Audio: 16 kHz, mono, 16-bit, 1280-sample (80 ms) chunks. The stream is closed
while recording, while paused, and on shutdown so the Windows microphone
indicator turns off.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

SAMPLE_RATE = 16_000
CHANNELS = 1
CHUNK_SAMPLES = 1_280
DEFAULT_THRESHOLD = 0.5
DEFAULT_COOLDOWN = 1.5
MAX_READ_FAILURES = 5

CUSTOM_MODEL_NAME = "agent"
MODEL_DIRS = (
    Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AgentMemory" / "wakeword",
    Path(__file__).resolve().parent / "models",
)


class WakeWordError(Exception):
    """The listener cannot start with the requested model or dependencies."""


def resolve_model(spec: str) -> str:
    """Return a model path or a pre-trained model name.

    Raises WakeWordError when a path/default "agent" model does not exist.
    """
    spec = (spec or "").strip() or CUSTOM_MODEL_NAME

    path = Path(spec).expanduser()
    if path.is_file():
        return str(path)

    looks_like_path = (
        "/" in spec or "\\" in spec
        or spec.lower().endswith((".onnx", ".tflite"))
    )
    if looks_like_path:
        raise WakeWordError(f"wake-word model not found: {spec}")

    if spec.lower() == CUSTOM_MODEL_NAME:
        for directory in MODEL_DIRS:
            for suffix in (".onnx", ".tflite"):
                candidate = directory / f"{CUSTOM_MODEL_NAME}{suffix}"
                if candidate.is_file():
                    return str(candidate)
        locations = " or ".join(str(d) for d in MODEL_DIRS)
        raise WakeWordError(
            "No 'agent' wake-word model was found. openWakeWord has no "
            "pre-trained 'agent' model; train one and place agent.onnx or "
            f"agent.tflite in {locations}, or set WAKE_WORD_MODEL to a "
            "pre-trained name such as hey_jarvis."
        )

    return spec


def _module_available(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _pretrained_paths(spec: str) -> tuple[Path, Path]:
    """Return the (tflite, onnx) paths for a pre-trained model name."""
    import openwakeword
    key = spec.replace(" ", "_")
    matches = [name for name in openwakeword.MODELS if key in name]
    if not matches:
        raise WakeWordError(f"unknown pre-trained wake-word model: {spec}")
    tflite = Path(openwakeword.MODELS[matches[0]]["model_path"])
    return tflite, tflite.with_suffix(".onnx")


def resolve_runtime(spec: str) -> tuple[str, str]:
    """Resolve a model spec to (model_path, inference_framework)."""
    path = Path(spec).expanduser()

    if path.is_file():
        suffix = path.suffix.lower()
        if suffix == ".onnx":
            return str(path), "onnx"
        if suffix != ".tflite":
            raise WakeWordError(f"unsupported wake-word model: {spec}")
        if _module_available("tflite_runtime"):
            return str(path), "tflite"
        sibling = path.with_suffix(".onnx")
        if sibling.is_file() and _module_available("onnxruntime"):
            return str(sibling), "onnx"
        raise WakeWordError(
            "tflite-runtime is not installed (it has no Windows wheels). "
            f"Place {sibling.name} beside {path.name} and install onnxruntime, "
            "or provide an ONNX model.")

    tflite, onnx = _pretrained_paths(spec)
    if not tflite.is_file() and not onnx.is_file():
        raise WakeWordError(
            "pre-trained wake-word models are not downloaded. Run: "
            'python -c "import openwakeword.utils as u; u.download_models()"')
    if onnx.is_file() and _module_available("onnxruntime"):
        return str(onnx), "onnx"
    if tflite.is_file() and _module_available("tflite_runtime"):
        return str(tflite), "tflite"
    raise WakeWordError(
        f"{spec} has no usable runtime. Install onnxruntime for ONNX models "
        "(pip install onnxruntime); tflite-runtime is not available on Windows.")


def _load_dependencies():
    import numpy as np
    import pyaudio
    from openwakeword.model import Model
    return pyaudio, np, Model


def _build_model(model_cls, model_path: str, framework: str):
    return model_cls(
        wakeword_models=[model_path], inference_framework=framework)


class WakeWordService:
    """Owns the microphone loop and calls on_detect(score) from its thread."""

    def __init__(
        self,
        on_detect: Callable[[float], None],
        model: str = CUSTOM_MODEL_NAME,
        threshold: float = DEFAULT_THRESHOLD,
        device_index: Optional[int] = None,
        cooldown: float = DEFAULT_COOLDOWN,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._on_detect = on_detect
        self._on_error = on_error
        self._spec = resolve_model(model)  # may raise before any thread starts
        self._model_path, self._framework = resolve_runtime(self._spec)
        self._threshold = max(0.01, min(1.0, float(threshold)))
        self._device_index = device_index
        self._cooldown = max(0.0, float(cooldown))

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._paused = False
        self._resume_after = 0.0
        self._stream = None
        self._audio = None
        self._model = None
        self._last_error = ""

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="wake-word", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the loop and close the microphone stream."""
        self._stop.set()
        self._close_stream()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def pause(self) -> None:
        """Close the mic while recording; no detections until resume()."""
        with self._lock:
            self._paused = True
        self._close_stream()

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            self._resume_after = time.monotonic() + self._cooldown
        self._reset_model()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def model_spec(self) -> str:
        return self._spec

    @property
    def inference_framework(self) -> str:
        return self._framework

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _report_error(self, message: str) -> None:
        self._last_error = message
        callback = self._on_error
        if callback is None:
            return
        try:
            callback(message)
        except Exception:
            pass

    def _reset_model(self) -> None:
        with self._lock:
            model = self._model
        if model is not None and hasattr(model, "reset"):
            try:
                model.reset()
            except Exception:
                pass

    def _ensure_stream(self, pyaudio_module):
        with self._lock:
            if self._stream is not None:
                return self._stream
            if self._audio is None:
                return None
            kwargs = {
                "format": pyaudio_module.paInt16,
                "channels": CHANNELS,
                "rate": SAMPLE_RATE,
                "input": True,
                "frames_per_buffer": CHUNK_SAMPLES,
            }
            if self._device_index is not None:
                kwargs["input_device_index"] = self._device_index
            self._stream = self._audio.open(**kwargs)
            return self._stream

    def _close_stream(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            return
        # Never touch the mixer here: keep shutdown portable.
        try:
            stream.stop_stream()
        except Exception:
            pass
        try:
            stream.close()
        except Exception:
            pass

    def _detect(self, score: float) -> None:
        now = time.monotonic()
        with self._lock:
            if now < self._resume_after:
                return
            self._paused = True
            self._resume_after = now + self._cooldown

        self._close_stream()
        self._reset_model()

        try:
            self._on_detect(float(score))
        except Exception as exc:
            self._report_error(f"wake callback failed: {exc}")
            self.resume()

    def _run(self) -> None:
        pyaudio_module = np = None
        try:
            pyaudio_module, np, model_cls = _load_dependencies()
            self._model = _build_model(
                model_cls, self._model_path, self._framework)
            self._audio = pyaudio_module.PyAudio()
            read_failures = 0

            while not self._stop.is_set():
                if self._paused:
                    self._close_stream()
                    self._stop.wait(0.05)
                    continue
                if time.monotonic() < self._resume_after:
                    self._stop.wait(0.05)
                    continue

                stream = self._ensure_stream(pyaudio_module)
                if stream is None:
                    break
                if self.is_paused():
                    # pause() may have landed while the stream was opening.
                    self._close_stream()
                    continue
                try:
                    data = stream.read(
                        CHUNK_SAMPLES, exception_on_overflow=False)
                except Exception as exc:
                    if self._stop.is_set() or self.is_paused():
                        continue
                    # A USB mic can reset or hiccup; rebuild the stream a few
                    # times instead of ending the listener for the session.
                    read_failures += 1
                    print(
                        f"[wake] mic read failed "
                        f"({read_failures}/{MAX_READ_FAILURES}): {exc}")
                    self._close_stream()
                    if read_failures >= MAX_READ_FAILURES:
                        raise
                    self._stop.wait(0.2 * read_failures)
                    continue
                read_failures = 0

                frame = np.frombuffer(data, dtype=np.int16)
                if frame.size == 0:
                    continue
                scores = self._model.predict(frame)
                if not scores:
                    continue
                _, score = max(
                    scores.items(), key=lambda item: float(item[1]))
                if float(score) >= self._threshold:
                    self._detect(float(score))

        except Exception as exc:
            if not self._stop.is_set():
                message = str(exc)
                if "model" in message.lower() or "onnx" in message.lower():
                    message += (
                        " (custom models go in "
                        f"{MODEL_DIRS[0]}; download pretrained models with "
                        "python -m openwakeword.utils.download_models)")
                self._report_error(message)
        finally:
            self._close_stream()
            with self._lock:
                audio = self._audio
                self._audio = None
                self._model = None
            if audio is not None:
                try:
                    audio.terminate()
                except Exception:
                    pass
