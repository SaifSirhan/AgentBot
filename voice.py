"""
Speech-to-text for the agent.
Requires: pip install faster-whisper sounddevice numpy
"""

import os
import wave
import tempfile
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

_whisper_model = None
WHISPER_MODEL_SIZE = "small.en"   # change to "small" for better accuracy

# Push-to-talk state
_stream = None
_recording_chunks = []
_is_recording = False
_SAMPLERATE = 16000


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
# Push-to-talk API
# ------------------------------------------------------------------
def start_recording():
    """Begin capturing audio from the mic. Call stop_and_transcribe() to finish."""
    global _stream, _recording_chunks, _is_recording

    _recording_chunks = []
    _is_recording = True

    def callback(indata, frames, time_info, status):
        if _is_recording:
            _recording_chunks.append(indata.copy())

    _stream = sd.InputStream(
        samplerate=_SAMPLERATE,
        channels=1,
        dtype='int16',
        callback=callback,
    )
    _stream.start()
    print("Recording started... (release to stop)")


def stop_and_transcribe():
    """Stop the recording and return the transcribed text."""
    global _stream, _is_recording, _recording_chunks

    _is_recording = False

    if _stream is not None:
        try:
            _stream.stop()
            _stream.close()
        except Exception:
            pass
        _stream = None

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