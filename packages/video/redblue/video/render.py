"""FFmpeg renderer: one MP4 per format from a script, per-beat footage and narration.

Text overlays and captions are burned in from a generated ASS subtitle file (libass), so no
drawtext/fontfile setup is needed. Works with the static binary from ``imageio-ffmpeg``."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from redblue.video.captions import WordTiming, chunk, even_timings
from redblue.video.schemas import Script
from redblue.video.templates import FORMATS, TEMPLATES, Format, Template, TextStyle, ass_colour


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


def poster_frame(video: Path, out: Path, at: float = 1.0) -> Path:
    """A JPEG still from the video (Reddit needs a poster image for video posts)."""
    try:
        _run(
            ["-ss", str(at), "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)], out.parent
        )
    except RenderError:  # very short videos: take the first frame
        _run(["-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)], out.parent)
    return out


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
    words: list[WordTiming] | None = None  # word timings relative to the beat (Whisper)


def _ts(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_text(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def _style(name: str, st: TextStyle, fmt: Format, primary: str, secondary: str) -> str:
    sc = fmt.scale
    if st.box:  # BorderStyle 3: the "outline" colour paints an opaque box
        border, outline_c = 3, ass_colour(st.box_colour, st.box_alpha)
        back = ass_colour("#000000", 0xFF)
    else:
        border, outline_c = 1, ass_colour("#000000", 0)
        back = ass_colour("#000000", 0x80)
    return (
        f"Style: {name},DejaVu Sans,{round(st.size * sc)},{primary},{secondary},"
        f"{outline_c},{back},{-1 if st.bold else 0},0,0,0,100,100,0,0,{border},"
        f"{round(st.outline * sc)},{round(st.shadow * sc)},{st.align},"
        f"{round(0.07 * fmt.width)},{round(0.07 * fmt.width)},"
        f"{round(st.margin * fmt.height)},1"
    )


def build_ass(
    script: Script,
    durations: list[float],
    fmt: Format | None = None,
    template: Template | None = None,
    words: list[list[WordTiming] | None] | None = None,
) -> str:
    """Title per beat plus word-grouped captions in the template's style, sized for the
    format. With word timings (Whisper), captions follow the narration and each word fills
    with the highlight colour as it is spoken; without, timings are spread by word length."""
    fmt = fmt or FORMATS["9:16"]
    tpl = template or TEMPLATES["bold"]
    cap = tpl.caption
    styles = [_style("Caption", cap, fmt, ass_colour(tpl.highlight), ass_colour(cap.colour))]
    if tpl.title:
        styles.append(
            _style(
                "Title", tpl.title, fmt, ass_colour(tpl.title.colour), ass_colour(tpl.title.colour)
            )
        )
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {fmt.width}\nPlayResY: {fmt.height}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles)
        + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    t = 0.0
    for i, (beat, d) in enumerate(zip(script.beats, durations, strict=True)):
        if tpl.title and beat.on_screen_text:
            lines.append(
                f"Dialogue: 0,{_ts(t)},{_ts(t + d)},Title,,0,0,0,,{_ass_text(beat.on_screen_text)}"
            )
        timed = (words[i] if words and i < len(words) else None) or even_timings(beat.narration, d)
        groups = chunk([w for w in timed if w.start < d], max_words=fmt.words_per_line)
        for j, group in enumerate(groups):
            start = group[0].start
            nxt = groups[j + 1][0].start if j + 1 < len(groups) else d
            end = min(max(group[-1].end, start + 0.3) + 0.6, nxt, d)
            parts = []
            for k, w in enumerate(group):
                until = group[k + 1].start if k + 1 < len(group) else w.end
                cs = max(1, round((until - w.start) * 100))
                parts.append(f"{{\\kf{cs}}}{_ass_text(w.word)}")
            lines.append(
                f"Dialogue: 1,{_ts(t + start)},{_ts(t + end)},Caption,,0,0,0,," + " ".join(parts)
            )
        t += d
    return head + "\n".join(lines) + "\n"


def render(
    script: Script,
    media: list[BeatMedia],
    out: Path,
    *,
    fmt: Format | None = None,
    template: Template | None = None,
    fps: int = 30,
) -> Path:
    fmt = fmt or FORMATS["9:16"]
    tpl = template or TEMPLATES["bold"]
    width, height = fmt.width, fmt.height
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
                f"crop={width}:{height},setsar=1,fps={fps}"
            )
            if m.clip:
                vin = ["-stream_loop", "-1", "-i", str(m.clip)]
                if tpl.footage_filter:
                    vf += "," + tpl.footage_filter
            else:
                color = tpl.palette[i % len(tpl.palette)]
                vin = ["-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r={fps}"]
            vf += ",format=yuv420p"
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
            build_ass(script, durations, fmt, tpl, [m.words for m in media]), encoding="utf-8"
        )
        total = round(sum(durations), 2)
        inputs = ["-i", "video.mp4", "-i", "audio.wav"]
        if tpl.progress:
            bar_h = max(4, round(10 * fmt.scale))
            y = 0 if tpl.progress_edge == "top" else height - bar_h
            colour = "0x" + tpl.progress.lstrip("#")
            inputs += ["-f", "lavfi", "-i", f"color=c={colour}:s={width}x{bar_h}:r={fps}"]
            # The bar slides in from the left, reaching full width at the end of the video.
            graph = (
                f"[0:v]ass=subs.ass[v];[v][2:v]overlay=x='-W+W*t/{total}':y={y}:"
                "eval=frame:shortest=1[out]"
            )
            vmap = ["-filter_complex", graph, "-map", "[out]", "-map", "1:a"]
        else:
            vmap = ["-vf", "ass=subs.ass"]
        _run(
            [
                *inputs,
                *vmap,
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
                "-t",
                f"{total}",
                "-movflags",
                "+faststart",
                str(out),
            ],
            work,
        )
    return out
