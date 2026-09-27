import re
import subprocess
from pathlib import Path

import pytest

from redblue.video.render import BeatMedia, build_ass, ffmpeg_exe, render
from redblue.video.schemas import Beat, Script
from redblue.video.templates import FORMATS, TEMPLATES, ass_colour, get_format, get_template

SCRIPT = Script(
    title="t",
    hook="h",
    cta="c",
    beats=[
        Beat(
            narration="Heat pumps work at minus twenty degrees.",
            on_screen_text="Cold climates",
            visual_query="q",
            seconds=2,
        ),
        Beat(narration="Follow for more.", on_screen_text="Follow", visual_query="q", seconds=2),
    ],
)


def test_formats_and_templates_lookup():
    assert get_format("16x9") is FORMATS["16:9"] and get_format("9:16").slug == "9x16"
    assert FORMATS["9:16"].orientation == "portrait" and FORMATS["16:9"].scale == 1.0
    assert FORMATS["9:16"].words_per_line < FORMATS["16:9"].words_per_line
    assert get_template("CLEAN").key == "clean"
    for bad in ("21:9", "../etc"):
        with pytest.raises(ValueError):
            get_format(bad)
    with pytest.raises(ValueError):
        get_template("fancy")


def test_ass_colour():
    assert ass_colour("#FFD60A") == "&H000AD6FF"
    assert ass_colour("#000000", 0x80) == "&H80000000"
    with pytest.raises(ValueError):
        ass_colour("red")


@pytest.mark.parametrize("tkey", list(TEMPLATES))
@pytest.mark.parametrize("fkey", list(FORMATS))
def test_ass_styles_per_template_and_format(tkey, fkey):
    fmt, tpl = FORMATS[fkey], TEMPLATES[tkey]
    ass = build_ass(SCRIPT, [2, 2], fmt, tpl)
    assert f"PlayResX: {fmt.width}" in ass and f"PlayResY: {fmt.height}" in ass
    cap = re.search(r"^Style: Caption,DejaVu Sans,(\d+),([^,]+),([^,]+)", ass, re.M)
    assert int(cap.group(1)) == round(tpl.caption.size * fmt.scale)
    assert cap.group(2) == ass_colour(tpl.highlight) and cap.group(3) == ass_colour(
        tpl.caption.colour
    )
    assert cap.group(2) != cap.group(3), "highlight must differ from the base caption colour"
    has_title = "Style: Title" in ass and ",Title,," in ass
    assert has_title == (tpl.title is not None)


def _probe(path: Path) -> tuple[int, int, float]:
    err = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True
    ).stderr
    w, h = map(int, re.search(r"Video: .*?, (\d{3,4})x(\d{3,4})", err).groups())
    hh, mm, ss = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err).groups()
    return w, h, int(hh) * 3600 + int(mm) * 60 + float(ss)


@pytest.mark.parametrize("tkey", list(TEMPLATES))
def test_every_template_renders_each_format_at_the_right_size(tkey, tmp_path):
    clip = tmp_path / "stock.mp4"
    subprocess.run(
        [
            ffmpeg_exe(),
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=640x360:r=30:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
    )
    for fkey in ("9:16", "16:9") if tkey != "bold" else FORMATS:
        fmt = FORMATS[fkey]
        out = render(
            SCRIPT,
            [BeatMedia(clip=clip), BeatMedia()],
            tmp_path / f"{tkey}-{fmt.slug}.mp4",
            fmt=fmt,
            template=TEMPLATES[tkey],
        )
        w, h, d = _probe(out)
        assert (w, h) == (fmt.width, fmt.height)
        assert abs(d - 4.0) < 0.15, d  # progress-bar overlay must not change the length
