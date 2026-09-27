"""Word-level caption timing.

Whisper (via faster-whisper, running locally) transcribes each beat's narration with word
timestamps. The script's own words are then aligned to that transcript, so captions show the
script's spelling ("2x", "Wi-Fi") with the audio's timing. Words Whisper missed or heard
differently get timings interpolated from their neighbours. Without narration or without
Whisper, timings fall back to spacing weighted by word length.
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger("redblue.video.captions")


@dataclass(frozen=True)
class WordTiming:
    word: str
    start: float
    end: float


class Transcriber(Protocol):
    def words(self, audio: Path) -> list[WordTiming]: ...


def _norm(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def even_timings(text: str, duration: float, start: float = 0.0) -> list[WordTiming]:
    """Spread words across ``duration``, longer words taking proportionally longer."""
    words = text.split()
    if not words or duration <= 0:
        return []
    weights = [max(1, len(_norm(w))) + 1 for w in words]  # +1 ≈ inter-word gap
    total, t, out = sum(weights), start, []
    for w, weight in zip(words, weights, strict=True):
        d = duration * weight / total
        out.append(WordTiming(w, round(t, 3), round(t + d, 3)))
        t += d
    return out


def align(script_text: str, heard: list[WordTiming], duration: float) -> list[WordTiming]:
    """Give every script word a start/end from the Whisper words it matches.

    Matching uses difflib on normalised tokens; unmatched script words are placed between
    their matched neighbours (or at the edges), so the result is monotonic and covers all
    script words even when Whisper hears "two times" for "2x".
    """
    words = script_text.split()
    if not words:
        return []
    if not heard:
        return even_timings(script_text, duration)
    a, b = [_norm(w) for w in words], [_norm(h.word) for h in heard]
    times: list[tuple[float, float] | None] = [None] * len(words)
    for block in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            h = heard[block.b + k]
            times[block.a + k] = (h.start, h.end)
    # Fill gaps: spread unmatched runs evenly between the surrounding matched words.
    i, n = 0, len(words)
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else (heard[0].start if heard else 0.0)
        right = times[j][0] if j < n else max(left, heard[-1].end if heard else duration)
        if right <= left:
            right = left + 0.25 * (j - i)
        for k, wt in enumerate(even_timings(" ".join(words[i:j]), right - left, left)):
            times[i + k] = (wt.start, wt.end)
        i = j
    out, prev_end = [], 0.0
    for w, (s, e) in zip(words, times, strict=True):  # type: ignore[misc]
        s = max(s, prev_end)
        e = max(e, s + 0.05)
        out.append(WordTiming(w, round(s, 3), round(e, 3)))
        prev_end = e
    return out


def chunk(
    timings: list[WordTiming], max_words: int = 4, max_seconds: float = 1.6
) -> list[list[WordTiming]]:
    """Group words into caption lines: break on punctuation, word count, duration or pauses."""
    chunks: list[list[WordTiming]] = []
    cur: list[WordTiming] = []
    for w in timings:
        if cur and (
            len(cur) >= max_words
            or w.end - cur[0].start > max_seconds
            or w.start - cur[-1].end > 0.35
        ):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if re.search(r"[.!?,;:]$", w.word):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


class FasterWhisper:
    """Local Whisper via faster-whisper (``pip install redblue-video[whisper]``).

    The model is downloaded from Hugging Face on first use and cached; ``base.en`` is a
    good default on CPU (~150 MB). Runs entirely on your machine: no key, no per-minute cost.
    """

    def __init__(self, model: str = "base.en", device: str = "auto", compute_type: str = "int8"):
        from faster_whisper import WhisperModel  # optional dependency

        self.model = WhisperModel(model, device=device, compute_type=compute_type)

    def words(self, audio: Path) -> list[WordTiming]:
        segments, _info = self.model.transcribe(
            str(audio), word_timestamps=True, vad_filter=False, beam_size=1
        )
        out = []
        for seg in segments:
            for w in seg.words or []:
                word = w.word.strip()
                if word:
                    out.append(WordTiming(word, float(w.start), float(w.end)))
        return out


def get_transcriber(mode: str, model: str = "base.en") -> Transcriber | None:
    """``whisper`` → FasterWhisper, or None when it isn't installed or the model can't be
    loaded (e.g. offline on first use); ``even`` → None. Callers fall back to even timing."""
    if mode != "whisper":
        return None
    try:
        return FasterWhisper(model)
    except ImportError:
        log.info("faster-whisper not installed; pip install 'redblue-video[whisper]'")
    except Exception as exc:  # noqa: BLE001 - model download/load failure
        log.warning("Whisper model %r unavailable (%s); using even caption timing", model, exc)
    return None
