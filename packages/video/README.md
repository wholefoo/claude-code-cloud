# redblue-video

Trending-video creation for RedBlue: find what's rising in your niche, research it with cited
sources, script an original short, render it with licensed footage and AI narration, and hand
it to a human for review. **Nothing is published automatically.**

```
sweep ──► score ──► brief ──► script ──► assets ──► render ──► review ──► (you upload)
YouTube    velocity  Tavily     Claude     Pexels     FFmpeg     admin
Data API   freshness Firecrawl  (cited)    ElevenLabs + captions approval
Tavily     relevance
           opportunity
```

## Quickstart

```bash
make dev                         # installs redblue-video with the rest of the monorepo
export RB_VIDEO_NICHE="AI chips" RB_VIDEO_KEYWORDS='["semiconductors","gpu"]'
export YOUTUBE_API_KEY=... TAVILY_API_KEY=... FIRECRAWL_API_KEY=...
export PEXELS_API_KEY=... ELEVENLABS_API_KEY=... ANTHROPIC_API_KEY=...   # all optional
redblue video sweep              # fetch + score trends
redblue video make --trend 12    # research → script → render, then stops for review
redblue dev                      # review at http://127.0.0.1:8000/admin/video
```

Every key is optional. Without keys the pipeline still runs end to end: a topic you type,
template briefs and scripts, brand-colour backgrounds, silent audio and burned-in captions.
That's useful for testing, and you add keys as you go.

| Key | Used for | Without it |
|---|---|---|
| `YOUTUBE_API_KEY` | Trending and niche-search video **metadata** (views, velocity) | Tavily-only trends |
| `TAVILY_API_KEY` | News trend signals and research sources | No sourced facts; write the script by hand |
| `FIRECRAWL_API_KEY` | Full-text extraction of the top sources | Tavily snippets only |
| `PEXELS_API_KEY` | Licensed stock footage per beat | Brand-colour backgrounds |
| `ELEVENLABS_API_KEY` | AI narration | Silent video with captions |
| `ANTHROPIC_API_KEY` | Brief and script writing (structured, cited) | Deterministic templates |

Settings (`RB_VIDEO_*`): `NICHE`, `KEYWORDS` (JSON list), `REGION`, `OUTPUT_DIR`,
`TARGET_SECONDS`, `WIDTH`/`HEIGHT` (default 1080×1920), `VOICE_ID`.

## Guardrails (built in)

- **No footage scraping.** YouTube/TikTok/Instagram are read only through official APIs
  (metadata). Firecrawl refuses video-platform URLs, and clip downloads are limited to Pexels
  hosts. Scripts are original; transcripts and other creators' videos are never reused.
- **Cited facts.** Brief facts must cite a fetched source. A script beat that states a
  number without a source, or cites a missing one, blocks rendering until a human fixes it.
- **Untrusted input.** Scraped pages are fenced before they reach the model, so text like
  "ignore previous instructions" is treated as data.
- **Licensing.** Every asset records its provider, license and attribution. The generated
  upload description lists sources, credits and an AI-assistance note.
- **Human in the loop.** Renders wait in review; approval only marks them ready for *you*
  to upload. Platform posting APIs are deliberately not wired up.

## Rendering

FFmpeg (the static binary from `imageio-ffmpeg`, or your system `ffmpeg`) renders each beat
from its stock clip (scaled/cropped to 9:16) or a brand colour, mixes the narration, and burns
in a title and word-grouped captions from a generated ASS subtitle file. A 45-second video
renders in well under a minute on a small VPS. For richer motion graphics, swap in Remotion
later: `render.render()` is the only function to replace.

## Next steps (not in the MVP)

Whisper word-level caption timing, multiple templates and aspect ratios, performance
tracking from platform analytics to tune scoring, Reddit/Google Trends signals, and optional
direct upload after approval.
