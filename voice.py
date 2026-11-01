"""
Speech-to-text for the agent.
Requires: pip install faster-whisper sounddevice numpy
"""

import os
import threading
import time
import wave
import tempfile
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

from audio_bus import audio_bus

_whisper_model = None
WHISPER_MODEL_SIZE = "small.en"   # change to "small" for better accuracy

# A spoken utterance ends after this much silence. Continuous mode passes a
# longer idle timeout so a silent room does not close the mic after 3.5s.
SILENCE_TIMEOUT = 3.5
SPEECH_RMS_THRESHOLD = 300

# Push-to-talk state
_stream = None
_recording_chunks = []
_is_recording = False
_SAMPLERATE = 16000
_input_device = None   # (index, name) selected by resolve_input_device()
# A just-released device can fail the first open while the OS finishes tearing
# the previous stream down. Retry exactly once after a short settling delay.
_MIC_OPEN_ATTEMPTS = 2
_MIC_RETRY_DELAY = 0.2

# Inputs that are loopback/virtual endpoints, not a room microphone.
_VIRTUAL_INPUT_HINTS = (
    "cable input", "cable output", "vb-audio", "virtual",
    "stereo mix", "monitor", "loopback", "sound mapper",
    "primary sound capture", "pc speaker", "wave out",
)

# Auto-stop state (only used when start_recording gets an on_transcript
# callback; F9 push-to-talk still stops on key release).
_on_transcript = None
_silence_timeout = SILENCE_TIMEOUT
_idle_timeout = None
_last_speech_at = 0.0
_speech_detected = False
_recording_started_at = 0.0
_whisper_lock = threading.Lock()
# Incremented on every start/cancel. A monitor thread that was already
# transcribing when the GUI cancelled must not deliver its stale transcript.
_recording_generation = 0


def _input_devices():
    """Return [(index, name)] for every device that can record."""
    devices = []
    for index, device in enumerate(sd.query_devices()):
        channels = int(device.get("max_input_channels", 0) or 0)
        if channels > 0:
            devices.append((index, str(device.get("name", ""))))
    return devices


def _supports_16k_input(index):
    try:
        sd.check_input_settings(
            device=index,
            samplerate=_SAMPLERATE,
            channels=1,
            dtype="int16",
        )
        return True
    except Exception:
        return False


def _is_virtual_input(name):
    lowered = str(name or "").lower()
    return any(hint in lowered for hint in _VIRTUAL_INPUT_HINTS)


def _microphone_score(name):
    lowered = str(name or "").lower()
    if _is_virtual_input(name):
        return -1
    score = 0
    if "microphone" in lowered or "headset" in lowered:
        score += 4
    if "usb" in lowered:
        score += 2
    if "array" in lowered:
        score += 1
    if "mic" in lowered:
        score += 1
    return score


def _configured_mic_device():
    """Read MIC_DEVICE / AGENT_MIC_DEVICE without failing on config errors."""
    try:
        import config
        configured = str(config.load_config().get("MIC_DEVICE", "") or "")
    except Exception:
        configured = ""
    return (os.environ.get("AGENT_MIC_DEVICE", "") or configured).strip()


def _match_configured_device(raw, devices):
    try:
        wanted = int(raw)
    except ValueError:
        matches = [
            pair for pair in devices if raw.lower() in pair[1].lower()
        ]
        if not matches:
            raise RuntimeError(
                f"MIC_DEVICE={raw!r} did not match any input device")
        return matches[0]
    for index, name in devices:
        if index == wanted:
            return index, name
    raise RuntimeError(f"MIC_DEVICE={wanted} is not an input device")


def resolve_input_device(force=False):
    """Pick a real microphone, skipping VB-Audio/CABLE-style loopbacks.

    Returns ``(index, name)`` and caches the choice for later recordings.
    """
    global _input_device
    if _input_device is not None and not force:
        return _input_device

    devices = _input_devices()
    if not devices:
        raise RuntimeError(
            "No microphone input devices found. Connect a microphone and "
            "restart the GUI.")

    configured = _configured_mic_device()
    if configured:
        selected = _match_configured_device(configured, devices)
        if not _supports_16k_input(selected[0]):
            raise RuntimeError(
                f"Microphone '{selected[1]}' (device {selected[0]}) does not "
                "support 16 kHz mono int16 input")
        _input_device = selected
        print(f"[voice] input device override: {selected[1]} "
              f"(index {selected[0]})")
        return _input_device

    try:
        default_index = int(sd.default.device[0])
    except Exception:
        default_index = None
    default_pair = next(
        (pair for pair in devices if pair[0] == default_index), None)
    if (default_pair is not None
            and not _is_virtual_input(default_pair[1])
            and _supports_16k_input(default_pair[0])):
        _input_device = default_pair
        print(f"[voice] input device: {default_pair[1]} "
              f"(index {default_pair[0]})")
        return _input_device

    candidates = [
        pair for pair in devices if _microphone_score(pair[1]) >= 0
    ]
    if not candidates:
        default_label = (
            f" Default input is '{default_pair[1]}'." if default_pair else "")
        raise RuntimeError(
            "No physical microphone found; default input is a virtual or "
            f"loopback device.{default_label} Set MIC_DEVICE to override.")
    candidates = [
        pair for pair in candidates if _supports_16k_input(pair[0])
    ]
    if not candidates:
        raise RuntimeError(
            "No physical microphone supports 16 kHz mono int16 input.")
    candidates.sort(key=lambda pair: (-_microphone_score(pair[1]), pair[0]))
    _input_device = candidates[0]
    if default_pair is not None and _is_virtual_input(default_pair[1]):
        print(f"[voice] default input '{default_pair[1]}' is virtual; "
              f"using {_input_device[1]} (index {_input_device[0]})")
    else:
        print(f"[voice] input device: {_input_device[1]} "
              f"(index {_input_device[0]})")
    return _input_device


def warmup():
    """Load Whisper and validate the mic path. Call off the GUI thread."""
    index, name = resolve_input_device(force=True)
    model = get_whisper_model()
    print(f"[voice] Whisper ready ({WHISPER_MODEL_SIZE}); "
          f"mic: {name} (index {index})")
    return model


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                print(
                    f"Loading Whisper model ('{WHISPER_MODEL_SIZE}')... "
                    "(first call only)")
                _whisper_model = WhisperModel(
                    WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model
    
def unload_whisper_model():
    """Free the Whisper model from RAM. It will reload on next use."""
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            del _whisper_model
            _whisper_model = None
            import gc
            gc.collect()
            print("[voice] Whisper model unloaded, RAM freed")

def transcribe(audio_path):
    model = get_whisper_model()
    segments, info = model.transcribe(
        audio_path,
        beam_size=5,
        language="en",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    text = " ".join(segment.text.strip() for segment in segments)
    return text.strip()


def _save_wav(audio_int16, samplerate):
    tmp_path = os.path.join(tempfile.gettempdir(), "agent_mic_input.wav")
    with wave.open(tmp_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(audio_int16.tobytes())
    return tmp_path


# ------------------------------------------------------------------
# Push-to-talk and VAD API
# ------------------------------------------------------------------
def auto_stop_reason(
    now,
    speech_detected,
    last_speech_at,
    recording_started_at,
    silence_timeout=SILENCE_TIMEOUT,
    idle_timeout=None,
):
    """Return why an auto-stop recording should end now, or None.

    Speech ends ``silence_timeout`` after the last voiced chunk. Continuous
    mode passes ``idle_timeout`` so a silent room gives up after 60s instead
    of ending the session after 3.5s.
    """
    if speech_detected:
        if now >= last_speech_at + silence_timeout:
            return "silence"
        return None
    if idle_timeout is not None:
        if now >= recording_started_at + idle_timeout:
            return "idle"
        return None
    if now >= recording_started_at + silence_timeout:
        return "no_speech"
    return None


def _close_stream():
    global _stream
    stream = _stream
    _stream = None
    if stream is None:
        return
    try:
        stream.stop()
    except Exception:
        pass
    try:
        stream.close()
    except Exception:
        pass


def _monitor_recording(generation):
    """End an auto-stop recording and hand the transcript to the callback.

    ``generation`` is the recording this monitor belongs to. If the recording
    was cancelled (``/continuous off``) while Whisper was running, the
    generation no longer matches and the transcript is dropped.
    """
    while _is_recording and generation == _recording_generation:
        reason = auto_stop_reason(
            time.monotonic(),
            _speech_detected,
            _last_speech_at,
            _recording_started_at,
            _silence_timeout,
            _idle_timeout,
        )
        if reason:
            break
        time.sleep(0.05)

    if not _is_recording or generation != _recording_generation:
        return

    callback = _on_transcript
    try:
        text = stop_and_transcribe()
    except Exception as e:
        print(f"[voice] auto-stop transcription failed: {e}")
        _close_stream()
        if callback is not None and generation == _recording_generation:
            try:
                callback("", "error")
            except Exception:
                pass
        return
    if callback is None or generation != _recording_generation:
        return
    try:
        callback(text, reason)
    except Exception as e:
        print(f"[voice] transcript callback failed: {e}")


def _open_input_stream(device_index, callback):
    """Open and start the mic, retrying once for a transient device error."""
    last_error = None
    for attempt in range(_MIC_OPEN_ATTEMPTS):
        if attempt:
            time.sleep(_MIC_RETRY_DELAY)
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=_SAMPLERATE,
                channels=1,
                dtype='int16',
                device=device_index,
                callback=callback,
            )
            stream.start()
            return stream
        except Exception as exc:
            last_error = exc
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            print(
                f"[voice] mic open failed "
                f"({attempt + 1}/{_MIC_OPEN_ATTEMPTS}): {exc}")
    raise last_error


def start_recording(
    on_transcript=None,
    silence_timeout=SILENCE_TIMEOUT,
    idle_timeout=None,
):
    """Begin capturing audio from the mic. Call stop_and_transcribe() to finish.

    With ``on_transcript``, a monitor thread ends the recording after
    ``silence_timeout`` of silence (or ``idle_timeout`` without any speech)
    and calls ``on_transcript(text, reason)`` from that thread.
    """
    global _stream, _recording_chunks, _is_recording, _on_transcript
    global _silence_timeout, _idle_timeout, _last_speech_at, _speech_detected
    global _recording_started_at, _recording_generation

    _recording_generation += 1
    generation = _recording_generation
    _recording_chunks = []
    _is_recording = True
    _recording_started_at = time.monotonic()
    _last_speech_at = _recording_started_at
    _speech_detected = False
    _silence_timeout = max(0.1, float(silence_timeout))
    _idle_timeout = (
        None if idle_timeout is None else max(0.1, float(idle_timeout)))
    _on_transcript = on_transcript

    def callback(indata, frames, time_info, status):
        global _last_speech_at, _speech_detected
        if not _is_recording:
            return
        _recording_chunks.append(indata.copy())
        audio_bus.publish(indata, "mic")
        try:
            rms = float(np.sqrt(np.mean(np.square(
                indata.astype(np.float32)))))
        except Exception:
            rms = 0.0
        if rms >= SPEECH_RMS_THRESHOLD:
            _last_speech_at = time.monotonic()
            _speech_detected = True

    try:
        device_index, device_name = resolve_input_device()
    except Exception:
        _is_recording = False
        _on_transcript = None
        _idle_timeout = None
        raise

    try:
        stream = _open_input_stream(device_index, callback)
    except Exception as exc:
        _is_recording = False
        _on_transcript = None
        _idle_timeout = None
        _close_stream()
        raise RuntimeError(
            f"Could not open microphone '{device_name}' "
            f"(device {device_index}): {exc}") from exc
    _stream = stream

    if not _is_recording or generation != _recording_generation:
        # Cancelled while the device was opening: never leave the mic held.
        if _stream is stream:
            _close_stream()
        else:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        return

    if on_transcript is not None:
        threading.Thread(
            target=_monitor_recording,
            args=(generation,),
            daemon=True,
            name="voice-vad",
        ).start()
    print("Recording started... (release to stop)")


def stop_and_transcribe():
    """Stop the recording and return the transcribed text."""
    global _stream, _is_recording, _recording_chunks
    global _on_transcript, _idle_timeout

    _is_recording = False
    _on_transcript = None
    _idle_timeout = None
    _close_stream()

    if not _recording_chunks:
        return ""

    audio = np.concatenate(_recording_chunks, axis=0)
    _recording_chunks = []

    if len(audio) < _SAMPLERATE * 0.3:   # less than 0.3s of audio
        return ""

    path = _save_wav(audio, _SAMPLERATE)
    text = transcribe(path)
    print(f"Transcribed: {text}")
    return text


def cancel_recording():
    """Drop the current recording without transcribing it."""
    global _is_recording, _recording_chunks, _on_transcript, _idle_timeout
    global _recording_generation
    _recording_generation += 1
    _is_recording = False
    _on_transcript = None
    _idle_timeout = None
    _close_stream()
    _recording_chunks = []


# ------------------------------------------------------------------
# Fixed-duration fallback (unchanged, kept for backwards compat)
# ------------------------------------------------------------------
def record_audio(duration=5, samplerate=_SAMPLERATE):
    print(f"Recording for {duration}s...")
    audio = sd.rec(int(duration * samplerate), samplerate=samplerate, channels=1, dtype='int16')
    sd.wait()
    return _save_wav(audio, samplerate)


def listen_and_transcribe(duration=5):
    audio_path = record_audio(duration)
    text = transcribe(audio_path)
    print(f"Transcribed: {text}")
    return text


if __name__ == "__main__":
    # Manual test - hold Enter to record, release to stop
    input("Press ENTER to start recording...")
    start_recording()
    input("Press ENTER again to stop recording...")
    text = stop_and_transcribe()
    print(f"\nYou said: {text}")