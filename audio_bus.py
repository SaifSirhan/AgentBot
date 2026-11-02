"""Thread-safe bridge between background audio threads and the Qt HUD."""
from __future__ import annotations

import threading
from collections import deque

import numpy as np


class AudioBus:
    """Bounded, lock-protected snapshot of the most recent audio samples.

    Mic callbacks and the Kokoro playback thread publish from their own
    threads; the GUI only ever reads finished snapshots under the lock.
    """

    def __init__(self, maxlen: int = 1024):
        self._maxlen = max(1, int(maxlen))
        self._lock = threading.Lock()
        self._samples = deque(maxlen=self._maxlen)
        self._rms = 0.0
        self._source = "idle"

    @staticmethod
    def _normalize(samples) -> np.ndarray:
        arr = np.asarray(samples)
        if arr.ndim > 1:
            arr = arr.reshape(-1)
        if arr.size == 0:
            return np.empty(0, dtype=np.float32)
        if arr.dtype.kind in "iu":
            info = np.iinfo(arr.dtype)
            scale = float(max(abs(info.min), abs(info.max), 1))
            arr = arr.astype(np.float32) / scale
        else:
            arr = arr.astype(np.float32, copy=True)
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=-1.0)
        np.clip(arr, -1.0, 1.0, out=arr)
        return arr

    def publish(self, samples, source: str = "mic") -> None:
        arr = self._normalize(samples)
        if arr.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(arr[-self._maxlen:]))))
        with self._lock:
            self._samples.extend(arr[-self._maxlen:])
            self._rms = rms
            self._source = str(source or "unknown")

    def get_latest(self):
        with self._lock:
            if not self._samples:
                return np.empty(0, dtype=np.float32), 0.0
            samples = np.fromiter(
                self._samples, dtype=np.float32, count=len(self._samples))
            return samples, self._rms

    @property
    def source(self) -> str:
        with self._lock:
            return self._source


audio_bus = AudioBus()
