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

# Auto-stop state (only used when start_recording gets an on_transcript
# callback; F9 push-to-talk still stops on key release).
_on_transcript = None
_silence_timeout = SILENCE_TIMEOUT
_idle_timeout = None
_last_speech_at = 0.0
_speech_detected = False
_recording_started_at = 0.0


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        print(f"Loading Whisper model ('{WHISPER_MODEL_SIZE}')... (first call only)")
        _whisper_model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _whisper_model
    
def unload_whisper_model():
    """Free the Whisper model from RAM. It will reload on next use."""
    global _whisper_model
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
    unload_whisper_model()   # ← ADD THIS LINE
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


def _monitor_recording():
    """End an auto-stop recording and hand the transcript to the callback."""
    while _is_recording:
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

    if not _is_recording:
        return

    callback = _on_transcript
    try:
        text = stop_and_transcribe()
    except Exception as e:
        print(f"[voice] auto-stop transcription failed: {e}")
        _close_stream()
        if callback is not None:
            try:
                callback("", "error")
            except Exception:
                pass
        return
    if callback is None:
        return
    try:
        callback(text, reason)
    except Exception as e:
        print(f"[voice] transcript callback failed: {e}")


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
    global _recording_started_at

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

    _stream = sd.InputStream(
        samplerate=_SAMPLERATE,
        channels=1,
        dtype='int16',
        callback=callback,
    )
    _stream.start()
    if on_transcript is not None:
        threading.Thread(
            target=_monitor_recording,
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