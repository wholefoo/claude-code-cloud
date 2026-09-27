"""FFmpeg renderer: one vertical MP4 from a script, per-beat footage and narration.

Text overlays and captions are burned in from a generated ASS subtitle file (libass), so no
drawtext/fontfile setup is needed. Works with the static binary from ``imageio-ffmpeg``."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from redblue.video.schemas import Script

PALETTE = ["#1f4fd1", "#0f1115", "#c2185b", "#1b7a3d", "#8a5a00"]


class RenderError(RuntimeError):
    pass


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        import shutil

        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RenderError("FFmpeg not found: pip install imageio-ffmpeg") from exc


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    proc = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        raise RenderError(proc.stderr[-2000:] or "ffmpeg failed")
    return proc


def media_duration(path: Path) -> float | None:
    proc = subprocess.run(  # noqa: S603 - fixed binary, argv list, no shell
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not m:
        return None
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


@dataclass
class BeatMedia:
    clip: Path | None = None  # licensed stock footage
    narration: Path | None = None  # TTS audio


def _ts(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def build_ass(script: Script, durations: list[float], width: int, height: int) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,DejaVu Sans,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,8,80,80,260,1
Style: Caption,DejaVu Sans,64,&H00FFFFFF,&H0000FFFF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,3,4,0,2,90,90,420,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    t = 0.0
    for beat, d in zip(script.beats, durations, strict=True):
        if beat.on_screen_text:
            lines.append(
                f"Dialogue: 0,{_ts(t)},{_ts(t + d)},Title,,0,0,0,,{_ass_text(beat.on_screen_text)}"
            )
        words = beat.narration.split()
        chunks = [" ".join(words[i : i + 4]) for i in range(0, len(words), 4)] or [""]
        step = d / len(chunks)
        for j, chunk in enumerate(chunks):
            lines.append(
                f"Dialogue: 1,{_ts(t + j * step)},{_ts(t + (j + 1) * step)},Caption,,"
                f"0,0,0,,{_ass_text(chunk)}"
            )
        t += d
    return head + "\n".join(lines) + "\n"


def render(
    script: Script,
    media: list[BeatMedia],
    out: Path,
    *,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
) -> Path:
    if len(media) != len(script.beats):
        raise RenderError("Need one BeatMedia per beat.")
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rb-render-") as tmp:
        work = Path(tmp)
        durations: list[float] = []
        for i, (beat, m) in enumerate(zip(script.beats, media, strict=True)):
            audio_len = media_duration(m.narration) if m.narration else None
            d = round(max(beat.seconds, (audio_len or 0) + 0.25), 2)
            durations.append(d)
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
            )
            if m.clip:
                vin = ["-stream_loop", "-1", "-i", str(m.clip)]
            else:
                color = PALETTE[i % len(PALETTE)]
                vin = ["-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}"]
            _run(
                [
                    *vin,
                    "-t",
                    f"{d}",
                    "-vf",
                    vf,
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    f"v{i:02d}.mp4",
                ],
                work,
            )
            if m.narration:
                ain = ["-i", str(m.narration), "-af", "apad"]
            else:
                ain = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            _run([*ain, "-t", f"{d}", "-ar", "44100", "-ac", "2", f"a{i:02d}.wav"], work)
        (work / "v.txt").write_text(
            "".join(f"file 'v{i:02d}.mp4'\n" for i in range(len(durations)))
        )
        (work / "a.txt").write_text(
            "".join(f"file 'a{i:02d}.wav'\n" for i in range(len(durations)))
        )
        _run(["-f", "concat", "-safe", "0", "-i", "v.txt", "-c", "copy", "video.mp4"], work)
        _run(["-f", "concat", "-safe", "0", "-i", "a.txt", "-c", "copy", "audio.wav"], work)
        (work / "subs.ass").write_text(
            build_ass(script, durations, width, height), encoding="utf-8"
        )
        _run(
            [
                "-i",
                "video.mp4",
                "-i",
                "audio.wav",
                "-vf",
                "ass=subs.ass",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "21",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out),
            ],
            work,
        )
    return out
