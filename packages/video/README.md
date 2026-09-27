# redblue-video

Trending-video creation for RedBlue: find what's rising in your niche, research it with cited
sources, script an original short, render it with licensed footage and AI narration, and hand
it to a human for review. **Nothing is published automatically.**

```
sweep ──► score ──► brief ──► script ──► assets ──► render ──► review ──► (you upload) ──► track
YouTube    velocity  Tavily     Claude     Pexels     FFmpeg     admin                     YouTube APIs
Tavily     freshness Firecrawl  (cited)    ElevenLabs + captions approval                  CSV / by hand
Reddit     relevance                                                                            │
Google     opportunity ◄──────────────── track record (what worked for you) ◄────────────────────┘
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
| `YOUTUBE_OAUTH_CLIENT_ID` + `YOUTUBE_OAUTH_CLIENT_SECRET` + `YOUTUBE_OAUTH_REFRESH_TOKEN` | Retention and shares for your uploads (YouTube Analytics, read-only) | Public view/like/comment counts only |

Settings (`RB_VIDEO_*`): `NICHE`, `KEYWORDS` (JSON list), `REGION`, `SUBREDDITS` (JSON list),
`GOOGLE_TRENDS_GEO`, `REDDIT_USER_AGENT`, `OUTPUT_DIR`, `TARGET_SECONDS`, `TEMPLATE`
(default `bold`), `FORMATS` (JSON list, default `["9:16"]`), `VOICE_ID`, `CAPTIONS`
(`whisper` or `even`), `WHISPER_MODEL`, `PERF_WINDOW_HOURS` (default 72), `TRACK_DAYS`
(default 30), `LEARN_FROM_PERFORMANCE` (default true), `LEARN_MIN_VIDEOS` (default 5),
`SCORE_WEIGHTS` (JSON object, e.g. `{"velocity": 0.2, "relevance": 0.45}`).

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
- **Read-only analytics.** Performance tracking reads your own videos' numbers; the YouTube
  Analytics token is read-only, and nothing is ever posted.
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

### Templates and formats

One script, one set of footage and narration, rendered in every format you pick, each
framed separately (footage is scaled and cropped, text sizes scale with the frame).

| Format | Size | For |
|---|---|---|
| `9:16` | 1080×1920 | Shorts, Reels, TikTok |
| `4:5` | 1080×1350 | Instagram/Facebook/LinkedIn feed |
| `1:1` | 1080×1080 | Square feed |
| `16:9` | 1920×1080 | YouTube, websites |

| Template | Look |
|---|---|
| `bold` | Big title up top, boxed captions, yellow word highlight |
| `clean` | Unboxed shadowed captions, darkened footage with vignette, small lower-third title, thin cyan progress bar at the top |
| `news` | Red lower-third title banner, boxed captions, red progress bar |
| `minimal` | No title; large centred captions over blurred, darkened footage |

Pick them per project in the admin (**Look** panel), from the CLI
(`redblue video make --topic "…" --template clean -f 9:16 -f 16:9`), or site-wide with
`RB_VIDEO_TEMPLATE` / `RB_VIDEO_FORMATS`. Stock footage is searched in the first format's
orientation. `redblue video templates` lists everything. Templates are plain dataclasses in
`templates.py`, so adding a brand look is a few lines.

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

## Performance tracking

After you upload an approved video yourself, record where: on the project page
(**Published** panel) or with `redblue video publish PROJECT_ID URL -f 9:16`. A video can be
recorded on several platforms, one entry per URL.

| Platform | How numbers arrive |
|---|---|
| YouTube | Fetched every 6 hours (and with `redblue video track` or **Fetch YouTube stats now**): views, likes and comments via `YOUTUBE_API_KEY`; with the optional OAuth settings, also shares and retention (average % viewed) from YouTube Analytics |
| TikTok, Instagram, Facebook, LinkedIn, X, other | Type them in on the project page, or import a CSV (admin **Performance** page or `redblue video import-stats file.csv`) |

The CSV needs `url` and `views` columns; `likes`, `comments`, `shares`, `avg_view_pct` and
`date` are optional, and common export headers ("Video views", "Average percentage viewed")
are recognized. Rows are matched to recorded publications by URL.

**Comparing fairly.** Each video is measured by its views at the same age (72 hours by
default, interpolated between snapshots) and compared with your median on the same
platform. That ratio is its *lift* (×2.0 means twice your usual). The **Performance** page
and `redblue video performance` show every video's lift and engagement, plus lift by trend
source, topic word, template and format.

**Tuning scoring.** Once 5 videos have mature numbers, each sweep multiplies a trend's score
by its *track record*: the learned effect of its source and topic words, bounded to
×0.8–1.25, and shown in the trend's breakdown. Effects are averaged in log space and shrunk
toward zero, and a source or word needs two videos before it counts, so one viral hit
doesn't take over. The page also shows how well each scoring signal (velocity, freshness,
relevance, opportunity) predicted your results; adjust `RB_VIDEO_SCORE_WEIGHTS` if one isn't
helping. Turn learning off with `RB_VIDEO_LEARN_FROM_PERFORMANCE=false`.

**Getting a YouTube Analytics refresh token (optional).** In Google Cloud, enable the
YouTube Analytics API and create an OAuth client (Desktop app). Authorize the channel owner
account once with the `https://www.googleapis.com/auth/yt-analytics.readonly` scope, for
example with the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground)
using your own client, and keep the refresh token in your environment or secret manager.
The scope is read-only. Analytics data lags two to three days, so public counts are used
for views when they're higher.

Only your own published videos are read. Nothing is posted, liked or commented.

## Next steps

Optional direct upload after approval (it would still need a human to press the button).
