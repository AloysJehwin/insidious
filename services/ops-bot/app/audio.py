"""ffmpeg helpers: WAV -> WhatsApp voice note (OGG/Opus mono), trimming, probing, tone generation."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

from .log import log

MAX_STT_SECONDS = 30.0
TRIM_TO_SECONDS = 29
VOICE_NOTE_MAX_BYTES = 512 * 1024


class AudioError(RuntimeError):
    pass


async def _run(args: list[str], stdin: bytes | None = None, timeout: float = 30.0) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise AudioError(f"{args[0]} timed out")
    if proc.returncode != 0:
        raise AudioError(f"{args[0]} failed rc={proc.returncode}: {err.decode(errors='replace')[-400:]}")
    return out


async def probe_duration(data: bytes, suffix: str = ".ogg") -> float | None:
    """Duration in seconds via ffprobe (temp file: ogg pipes do not always report duration)."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(data)
        path = f.name
    try:
        out = await _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path])
        dur = json.loads(out or b"{}").get("format", {}).get("duration")
        return float(dur) if dur not in (None, "N/A") else None
    except Exception as e:  # noqa: BLE001
        log("audio_probe_failed", error=str(e))
        return None
    finally:
        os.unlink(path)


async def trim_audio(data: bytes, seconds: int = TRIM_TO_SECONDS, suffix: str = ".ogg") -> bytes:
    """Trim to the first `seconds` seconds, re-encoded as OGG/Opus (Sarvam accepts it)."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in" + suffix)
        dst = os.path.join(d, "out.ogg")
        with open(src, "wb") as f:
            f.write(data)
        await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-t", str(seconds),
                    "-ac", "1", "-c:a", "libopus", "-b:a", "32k", dst])
        with open(dst, "rb") as f:
            return f.read()


async def ensure_stt_length(data: bytes, suffix: str = ".ogg") -> tuple[bytes, float | None, bool]:
    """Returns (audio, duration, trimmed). Sarvam REST STT rejects audio over 30 s."""
    dur = await probe_duration(data, suffix)
    if dur is not None and dur > MAX_STT_SECONDS - 0.5:
        log("audio_trimmed", duration_s=dur, to_s=TRIM_TO_SECONDS)
        return await trim_audio(data, TRIM_TO_SECONDS, suffix), dur, True
    return data, dur, False


async def wav_to_ogg_opus(wav: bytes) -> bytes:
    """Bulbul WAV -> mono 48 kHz Opus in OGG at 32 kbps (WhatsApp voice-note format)."""
    ogg = await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "wav", "-i", "pipe:0",
                      "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-application", "voip",
                      "-f", "ogg", "pipe:1"], stdin=wav)
    if len(ogg) > VOICE_NOTE_MAX_BYTES:
        # Re-encode at a lower bitrate so WhatsApp still shows the play icon.
        log("voice_note_too_big", bytes=len(ogg))
        ogg = await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "wav", "-i", "pipe:0",
                          "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "16k", "-application", "voip",
                          "-t", "120", "-f", "ogg", "pipe:1"], stdin=wav)
    return ogg


async def tone_wav(seconds: float = 1.5, freq: int = 440, sample_rate: int = 22050) -> bytes:
    """A short sine tone WAV (used by SARVAM_MOCK=1 as the 'speech')."""
    return await _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                       f"sine=frequency={freq}:duration={seconds}:sample_rate={sample_rate}",
                       "-ac", "1", "-f", "wav", "pipe:1"])
