# redblue-video

Trending-video creation for RedBlue: find what's rising in your niche, research it with cited
sources, script an original short, render it with licensed footage and AI narration, and hand
it to a human for review. **Nothing is published automatically.**

```
sweep ──► score ──► brief ──► script ──► assets ──► render ──► review ──► you upload ─────► track
YouTube    velocity  Tavily     Claude     Pexels     FFmpeg     admin                     YouTube APIs
Tavily     freshness Firecrawl  (cited)    ElevenLabs + captions approval                  CSV / by hand
Reddit     relevance                                                                            │
Google     opportunity ◄──────────────── track record (what worked for you) ◄────────────────────┘
Trends
```

"You upload" means by hand, or with the optional upload buttons for YouTube, TikTok,
Instagram, Facebook and LinkedIn, which a person presses for each video.

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
| `YOUTUBE_UPLOAD_REFRESH_TOKEN` + `RB_VIDEO_UPLOAD_ENABLED=true` (same OAuth client) | Optional one-click upload of an approved render to YouTube | You upload the downloaded file yourself |
| `TIKTOK_CLIENT_KEY` + `TIKTOK_CLIENT_SECRET` + `TIKTOK_REFRESH_TOKEN` (+ `RB_VIDEO_UPLOAD_ENABLED`) | Send an approved render to your TikTok drafts (or post it directly) | You upload it yourself |
| `INSTAGRAM_ACCESS_TOKEN` + `INSTAGRAM_USER_ID` (+ `RB_VIDEO_UPLOAD_ENABLED`) | Upload an approved render as a Reel, then publish it with a second click | You upload it yourself |
| `FACEBOOK_PAGE_ACCESS_TOKEN` + `FACEBOOK_PAGE_ID` (+ `RB_VIDEO_UPLOAD_ENABLED`) | Upload an approved render as a Page Reel (a draft by default) | You upload it yourself |
| `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_AUTHOR_URN` (+ `RB_VIDEO_UPLOAD_ENABLED`) | Upload an approved render, then post it with a second click | You upload it yourself |

Settings (`RB_VIDEO_*`): `NICHE`, `KEYWORDS` (JSON list), `REGION`, `SUBREDDITS` (JSON list),
`GOOGLE_TRENDS_GEO`, `REDDIT_USER_AGENT`, `OUTPUT_DIR`, `TARGET_SECONDS`, `TEMPLATE`
(default `bold`), `FORMATS` (JSON list, default `["9:16"]`), `VOICE_ID`, `CAPTIONS`
(`whisper` or `even`), `WHISPER_MODEL`, `PERF_WINDOW_HOURS` (default 72), `TRACK_DAYS`
(default 30), `LEARN_FROM_PERFORMANCE` (default true), `LEARN_MIN_VIDEOS` (default 5),
`SCORE_WEIGHTS` (JSON object, e.g. `{"velocity": 0.2, "relevance": 0.45}`), `UPLOAD_ENABLED`
(default false), `UPLOAD_CATEGORY_ID` (default 28, Science & Technology), `TIKTOK_MODE`
(`inbox` or `direct`), `TIKTOK_USERNAME`, `TIKTOK_TOKEN_FILE`, `INSTAGRAM_GRAPH_HOST`
(`graph.facebook.com` or `graph.instagram.com`), `INSTAGRAM_API_VERSION` (default `v25.0`),
`FACEBOOK_API_VERSION` (default `v25.0`), `LINKEDIN_VERSION` (default `202606`).

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
  to upload. Optional direct upload to YouTube, TikTok, Instagram, Facebook and LinkedIn
  exists, but it's off by default and every upload is a person pressing a button (or
  confirming in the CLI): no job, sweep or pipeline step ever uploads or publishes. YouTube
  uploads are private unless the person chooses otherwise, TikTok and Facebook go to drafts
  by default, and Instagram and LinkedIn need a second click to publish.

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

**Getting a YouTube Analytics refresh token (optional).**

1. In Google Cloud, enable the YouTube Analytics API. On the OAuth consent screen, add the
   channel owner's Google account as a test user.
2. Create an OAuth client of type **Web application** with the authorized redirect URI
   `https://developers.google.com/oauthplayground`.
3. In the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground), open the
   settings (gear icon), tick "Use your own OAuth credentials" and enter your client's id
   and secret.
4. Authorize the scope `https://www.googleapis.com/auth/yt-analytics.readonly` as the channel
   owner, choosing the brand channel if you have one. Then exchange the code for tokens and
   keep the refresh token in your environment or secret manager.

While the consent screen's publishing status is "Testing", Google expires refresh tokens
after 7 days; publish the consent screen to keep them. The scope is read-only. Analytics
data lags two to three days, so public counts are used for views when they're higher.

Only your own published videos are read. Nothing is posted, liked or commented.

## Upload to YouTube (optional)

Off by default. When it's on, an editor sees an **Upload to YouTube** panel on approved
projects: pick the render (format), check the pre-filled title, description (sources,
credits and AI note) and tags, choose visibility, answer "Made for kids?", and confirm.

- **Private by default.** Unlisted and public are one click away, but public asks for a
  second confirmation. You can also upload privately and make it public later in YouTube
  Studio, which is a good habit for the first few.
- **Disclosure.** "Altered or synthetic content" is ticked by default because the narration
  is an AI voice; untick it if you replaced the voice with your own.
- **Once per render.** Each format uploads once; the upload is recorded as a publication
  (with who confirmed it), so performance tracking starts automatically.
- **Only a person uploads.** There's no background job or schedule for it, the pipeline
  never calls it, and the CLI version (`redblue video upload 12 -f 9:16
  --not-made-for-kids`) shows what it will send and asks you to confirm, with no `--yes`
  flag, and refuses to run without a terminal.

**Setup.** In the Google Cloud project that has your OAuth client (see "Getting a YouTube
Analytics refresh token" above), enable the YouTube Data API v3. Authorize the channel
owner account once more with the `https://www.googleapis.com/auth/youtube.upload` scope and
keep that refresh token separate from the read-only one:

```bash
export YOUTUBE_OAUTH_CLIENT_ID=... YOUTUBE_OAUTH_CLIENT_SECRET=...
export YOUTUBE_UPLOAD_REFRESH_TOKEN=...      # youtube.upload scope
export RB_VIDEO_UPLOAD_ENABLED=true
```

Unverified Google Cloud apps can only upload **private** videos (YouTube locks public and
unlisted uploads from unaudited API projects), so pass Google's API audit before relying on
public uploads; until then, upload privately and publish from YouTube Studio. Each upload
uses a large share of the default daily API quota, so check your quota in Google Cloud.

## Upload to TikTok (optional)

Off by default. With credentials set, editors see an **Upload to TikTok** panel on approved
projects, and there's a CLI command: `redblue video tiktok 12 -f 9:16`.

- **Drafts mode (default, `RB_VIDEO_TIKTOK_MODE=inbox`, scope `video.upload`).** The
  video goes to your TikTok inbox as a draft. Open the notification in the TikTok app to add
  the caption, sound and privacy and post it. Then record the post's URL under
  **Published** (or set `RB_VIDEO_TIKTOK_USERNAME` so public posts are linked for you).
- **Direct mode (`RB_VIDEO_TIKTOK_MODE=direct`, scope `video.publish`).** Posts from
  RedBlue. As TikTok requires, the panel shows the account name and the privacy options
  TikTok allows for the account, with none preselected. Comments, Duet and Stitch start off.
  There are commercial-content disclosures and an "AI-generated" label (on by default).
  Choosing anything other than "Only me" needs a second confirmation. **Until TikTok audits
  your app, direct posts are private ("Only me") and limited to a few accounts.**

Status updates arrive via **Check status**, `redblue video uploads`, or the 6-hourly
tracking job, which only reads status.

**Setup.**

1. At developers.tiktok.com, create an app and add **Login Kit** and the **Content Posting
   API** (enable Direct Post only if you want direct mode). Request `user.info.basic` and
   `video.upload` (drafts) or `video.publish` (direct), and register a redirect URI you
   control.
2. Authorize your account once. Open
   `https://www.tiktok.com/v2/auth/authorize/?client_key=KEY&response_type=code&scope=user.info.basic,video.upload&redirect_uri=URI&state=anything`,
   approve, and copy the `code` from the address you're redirected to.
3. Exchange the code for tokens:

   ```bash
   curl -s https://open.tiktokapis.com/v2/oauth/token/ \
     -d client_key=KEY -d client_secret=SECRET -d grant_type=authorization_code \
     --data-urlencode redirect_uri=URI -d "code=CODE_AS_COPIED"
   ```

   Paste the code exactly as it appears in the address bar: it's already URL-encoded.
   Codes expire within minutes. Keep the `refresh_token` it returns (valid for a year).
   If your TikTok app is still in sandbox, add your account as a target user first.
4. Set the credentials:

   ```bash
   export TIKTOK_CLIENT_KEY=... TIKTOK_CLIENT_SECRET=... TIKTOK_REFRESH_TOKEN=...
   export RB_VIDEO_TIKTOK_TOKEN_FILE=/var/lib/redblue/tiktok.token   # optional, see below
   ```

TikTok may issue a new refresh token when RedBlue refreshes access. With
`RB_VIDEO_TIKTOK_TOKEN_FILE` set, the new one is written there (mode 600) and used from then
on. Otherwise RedBlue logs a warning and you re-authorize before the old one expires.

## Upload to Instagram (optional)

Off by default. Instagram has no private or draft posts, so it takes two steps, each a
person's click:

1. **Upload Reel (not public yet)** sends the render into an Instagram container
   (`redblue video instagram 12 -f 9:16`).
2. When Instagram has processed it (**Check status** shows "ready"), **Publish to
   Instagram** with its confirmation makes it public
   (`redblue video instagram-publish UPLOAD_ID`). The permalink is recorded, so tracking
   starts.

The tracking job refreshes the status of pending uploads but never publishes. Unpublished
containers expire after 24 hours; upload again if that happens. Instagram allows 100
API-published posts per account per day. The caption is pre-filled with the title, the AI
note and hashtags (at most 30).

**Setup.** You need an Instagram professional account (Business or Creator) and a Meta
developer app with the Instagram product.

- **Instagram Login** (simplest): add the `instagram_business_basic` and
  `instagram_business_content_publish` permissions, generate a token for your account in the
  app dashboard, and set `RB_VIDEO_INSTAGRAM_GRAPH_HOST=graph.instagram.com`.
- **Facebook Login** (account linked to a Facebook Page): `instagram_content_publish` with
  the default host `graph.facebook.com`.

```bash
export INSTAGRAM_ACCESS_TOKEN=...      # long-lived token; expires after 60 days
export INSTAGRAM_USER_ID=1784...       # the numeric Instagram account id
```

Long-lived Instagram tokens last 60 days; refresh or regenerate them before then. Until
Meta approves your app, only accounts with a role on the app can use it.

## Upload to Facebook (optional)

Page Reels only; the Graph API can't post to personal profiles or groups. With credentials
set, editors see an **Upload to Facebook** panel on approved projects
(`redblue video facebook 12 -f 9:16`).

- **Draft (default):** the Reel is saved as a draft on your Page. Publish it yourself in
  Meta Business Suite. **Check status** (or the tracking job) notices when it's published
  and records the link.
- **Publish now:** posts it to the Page right away; this needs a second confirmation
  (`--publish-now` in the CLI).

**Setup.** In a Meta developer app, request `pages_show_list`, `pages_read_engagement` and
`pages_manage_posts`. In the Graph API Explorer, get a user token with those permissions,
exchange it for a long-lived one (Access Token Debugger), then read `/me/accounts` for the
Page's access token and id. A Page token from a long-lived user token doesn't expire unless
you change your password or remove the app.

```bash
export FACEBOOK_PAGE_ACCESS_TOKEN=... FACEBOOK_PAGE_ID=1234567890
```

## Upload to LinkedIn (optional)

Two steps, each a person's click, like Instagram:

1. **Upload video (not posted yet)** sends the render to LinkedIn with the post text you
   entered (`redblue video linkedin 12 -f 16:9`).
2. When LinkedIn has processed it (**Check status** shows "ready"), choose **Anyone** or
   **Connections only** (no default) and press **Post to LinkedIn**
   (`redblue video linkedin-publish UPLOAD_ID --visibility PUBLIC`). Company pages can only
   post publicly. The post's link is recorded, so tracking starts.

Characters LinkedIn treats as formatting (`#`, `@`, brackets and so on) are escaped, so the
text appears exactly as typed; hashtags show as plain text rather than links.

**Setup.** In a LinkedIn developer app, add **Share on LinkedIn** (`w_member_social`) and
**Sign In with LinkedIn using OpenID Connect** (`openid profile`). Posting as a company page
needs the Community Management API (`w_organization_social`), which LinkedIn must approve.
Generate an access token with the developer portal's OAuth token tool (valid 60 days), then
find your member id:

```bash
curl -s -H "Authorization: Bearer $LINKEDIN_ACCESS_TOKEN" https://api.linkedin.com/v2/userinfo
export LINKEDIN_ACCESS_TOKEN=...
export LINKEDIN_AUTHOR_URN=urn:li:person:SUB_FROM_USERINFO   # or urn:li:organization:ID
```

LinkedIn retires API versions after about a year. If you see a version error, set
`RB_VIDEO_LINKEDIN_VERSION` to a recent month (YYYYMM).
