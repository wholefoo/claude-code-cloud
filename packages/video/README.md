# redblue-video

Trending-video creation for RedBlue: find what's rising in your niche, research it with cited
sources, script an original short, render it with licensed footage and AI narration, and hand
it to a human for review. **Nothing is published automatically.**

```
sweep ──► score ──► brief ──► script ──► assets ──► render ──► review ──► (you upload)
YouTube    velocity  Tavily     Claude     Pexels     FFmpeg     admin
Tavily     freshness Firecrawl  (cited)    ElevenLabs + captions approval
Reddit     relevance
Google     opportunity
Trends
```

## Quickstart

```bash
make dev                         # installs redblue-video with the rest of the monorepo
export RB_VIDEO_NICHE="AI chips" RB_VIDEO_KEYWORDS='["semiconductors","gpu"]'
export RB_VIDEO_SUBREDDITS='["hardware","technology"]'
export YOUTUBE_API_KEY=... TAVILY_API_KEY=... FIRECRAWL_API_KEY=...
export REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=...
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
| `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` (+ `RB_VIDEO_SUBREDDITS`) | Rising/hot post titles, votes and comment counts from your subreddits (official API; create an app at reddit.com/prefs/apps) | No Reddit signals |
| *(none)* | **Google Trends** "Trending now" searches for `RB_VIDEO_GOOGLE_TRENDS_GEO` (default: `RB_VIDEO_REGION`) via Google's public RSS feed | Always on |
| `FIRECRAWL_API_KEY` | Full-text extraction of the top sources | Tavily snippets only |
| `PEXELS_API_KEY` | Licensed stock footage per beat | Brand-colour backgrounds |
| `ELEVENLABS_API_KEY` | AI narration | Silent video with captions |
| `ANTHROPIC_API_KEY` | Brief and script writing (structured, cited) | Deterministic templates |

Settings (`RB_VIDEO_*`): `NICHE`, `KEYWORDS` (JSON list), `REGION`, `SUBREDDITS` (JSON list),
`GOOGLE_TRENDS_GEO`, `REDDIT_USER_AGENT`, `OUTPUT_DIR`, `TARGET_SECONDS`, `WIDTH`/`HEIGHT`
(default 1080×1920), `VOICE_ID`, `CAPTIONS` (`whisper` or `even`), `WHISPER_MODEL`.

### How sources are compared

Each trend's *reach* is views (YouTube), approximate searches (Google Trends), or, for Reddit,
an estimate of `(upvotes + 2 × comments) × 40` viewers. Velocity is reach per hour on a log
scale. Relevance to your niche and keywords also gates the total score, so a viral search
about something else (say, football scores) ranks below a smaller trend in your niche.

## Guardrails (built in)

- **No footage scraping.** YouTube/TikTok/Instagram and Reddit are read only through
  official APIs (titles and counts); Google Trends through its public feed. Firecrawl
  refuses video-platform URLs, and clip downloads are limited to Pexels
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
in a title and word-grouped captions from a generated ASS subtitle file.

### Word-level captions (Whisper)

```bash
pip install 'redblue-video[whisper]'     # faster-whisper; runs locally, no key
export RB_VIDEO_WHISPER_MODEL=base.en     # tiny.en is faster, small.en more accurate
```

With narration, each beat's audio is transcribed locally by Whisper with word timestamps.
The script's words are aligned to what Whisper heard, so captions keep the script's spelling
("2x", "Wi-Fi") but use the audio's timing. Words Whisper missed or heard differently get
timings interpolated from their neighbours. Captions appear in short groups (split on
punctuation, pauses, four words or 1.6 s) and each word fills in karaoke-style as it's spoken.

The model downloads from Hugging Face on first use (~150 MB for `base.en`) and is cached.
Without faster-whisper, without narration, if the model can't be loaded, or with
`RB_VIDEO_CAPTIONS=even`, captions fall back to timing spread by word length.

A 45-second video renders in well under a minute on a small VPS (Whisper adds a few seconds
per beat on CPU with `base.en`). For richer motion graphics, swap in Remotion later:
`render.render()` is the only function to replace.

## Next steps (not in the MVP)

Multiple templates and aspect ratios, performance tracking from platform analytics to tune
scoring, and optional direct upload after approval.
