"""Visual templates and output formats for rendered videos.

A *format* is an aspect ratio and pixel size. A *template* is a look: where the title and
captions sit, their colours and box style, how background footage is treated, and whether a
progress bar runs along the edge. Any template works in any format; sizes scale with the
frame so the same template reads well at 9:16 and 16:9.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Format:
    key: str  # "9:16"
    width: int
    height: int
    orientation: Literal["portrait", "landscape", "square"]  # stock-footage search hint
    label: str

    @property
    def slug(self) -> str:  # safe for filenames and URLs
        return self.key.replace(":", "x")

    @property
    def scale(self) -> float:  # font/outline sizes are designed for a 1080 px short side
        return min(self.width, self.height) / 1080

    @property
    def words_per_line(self) -> int:
        return 4 if self.height > self.width else (6 if self.width > self.height else 5)


FORMATS: dict[str, Format] = {
    f.key: f
    for f in (
        Format("9:16", 1080, 1920, "portrait", "Vertical: Shorts, Reels, TikTok"),
        Format("4:5", 1080, 1350, "portrait", "Portrait feed: Instagram, Facebook, LinkedIn"),
        Format("1:1", 1080, 1080, "square", "Square feed"),
        Format("16:9", 1920, 1080, "landscape", "Landscape: YouTube, websites"),
    )
}


def get_format(key: str) -> Format:
    key = key.strip().replace("x", ":")
    try:
        return FORMATS[key]
    except KeyError:
        raise ValueError(f"Unknown format {key!r}; choose from {', '.join(FORMATS)}") from None


def ass_colour(hex_colour: str, alpha: int = 0) -> str:
    """'#RRGGBB' → ASS '&HAABBGGRR' (alpha 0 = opaque, 255 = transparent)."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_colour):
        raise ValueError(f"Colours must be #RRGGBB, got {hex_colour!r}")
    r, g, b = hex_colour[1:3], hex_colour[3:5], hex_colour[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


@dataclass(frozen=True)
class TextStyle:
    size: int  # px at a 1080 px short side
    align: Literal[2, 5, 8]  # ASS numpad: 2 bottom, 5 middle, 8 top (all centred)
    margin: float  # vertical margin as a fraction of frame height
    colour: str = "#FFFFFF"
    box: bool = False  # opaque box behind text (BorderStyle 3) vs outline (1)
    box_colour: str = "#000000"
    box_alpha: int = 0x60
    outline: int = 5
    shadow: int = 2
    bold: bool = True


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    description: str
    caption: TextStyle
    highlight: str  # colour a word turns as it is spoken
    title: TextStyle | None = None  # None: no title overlay
    footage_filter: str = ""  # appended to the per-beat video filter for stock clips
    palette: tuple[str, ...] = ("#1f4fd1", "#0f1115", "#c2185b", "#1b7a3d", "#8a5a00")
    progress: str | None = None  # progress-bar colour, None for no bar
    progress_edge: Literal["top", "bottom"] = "bottom"


TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (
        Template(
            "bold",
            "Bold",
            "Big title up top and boxed captions with a yellow word highlight. Loud and legible.",
            title=TextStyle(86, 8, 0.135, outline=6),
            caption=TextStyle(64, 2, 0.22, box=True, box_alpha=0x60, outline=4, shadow=0),
            highlight="#FFD60A",
        ),
        Template(
            "clean",
            "Clean",
            "Unboxed captions with a soft shadow over slightly darkened footage, a small "
            "lower-third title and a thin accent progress bar.",
            title=TextStyle(54, 2, 0.12, outline=3, shadow=1),
            caption=TextStyle(70, 5, 0.0, outline=4, shadow=3),
            highlight="#4CC9F0",
            footage_filter="eq=brightness=-0.07:saturation=0.9,vignette=PI/5",
            palette=("#14213d", "#1d3557", "#264653", "#2b2d42"),
            progress="#4CC9F0",
            progress_edge="top",
        ),
        Template(
            "news",
            "News",
            "Explainer look: a red title banner in the lower third, boxed captions above it and "
            "a red progress bar.",
            title=TextStyle(
                58, 2, 0.08, box=True, box_colour="#C1121F", box_alpha=0, outline=14, shadow=0
            ),
            caption=TextStyle(
                60, 2, 0.24, colour="#AEB4BE", box=True, box_alpha=0x40, outline=4, shadow=0
            ),
            highlight="#FFFFFF",
            footage_filter="eq=brightness=-0.03",
            palette=("#0b132b", "#1c2541", "#3a506b"),
            progress="#C1121F",
            progress_edge="bottom",
        ),
        Template(
            "minimal",
            "Minimal",
            "No title: large centred captions over blurred, darkened footage so the words carry "
            "the video.",
            caption=TextStyle(88, 5, 0.0, colour="#9AA0A6", outline=0, shadow=0),
            highlight="#FFFFFF",
            footage_filter="gblur=sigma=18,eq=brightness=-0.18",
            palette=("#111111", "#1a1a2e", "#16213e"),
        ),
    )
}


def get_template(key: str) -> Template:
    try:
        return TEMPLATES[key.strip().lower()]
    except KeyError:
        raise ValueError(f"Unknown template {key!r}; choose from {', '.join(TEMPLATES)}") from None
