# RedBlue

**The platform where AI-built sites are adversarially hardened *and* built to be found.**

RedBlue is an open-source (Apache-2.0), self-hosted web development platform for Python/FastAPI.
It takes a site from "I have an idea" to "it's live, secure, and getting traffic". Agents do
most of the work, and humans approve anything public.

| Layer | What it does | Package |
|---|---|---|
| **Builder** | Intent → approved plan → real, ejectable FastAPI project; features land on branches | `redblue.builder` |
| **Backend** | Data, auth (roles, sessions, API keys, OAuth), storage, jobs, email, AI, webhooks | `redblue.core` |
| **CMS** | Block-based, validated content; writer → editor → publisher; revisions; headless API | `redblue.cms` |
| **Template library** | 60 page types, server-rendered, accessible, JSON-LD, CWV budgets | `redblue.templates` |
| **Security gate** | Red agent scans code and a preview it launches; Blue agent writes verified fixes | `redblue.gate` |
| **Observability** | Request metrics, error grouping, uptime, OTel export, ops agent explains regressions | `redblue.observe` |
| **Growth engine** | SEO, AEO/GEO (`llms.txt`, answer blocks, AI crawler controls), cookieless analytics, A/B tests, newsletter | `redblue.growth` |
| **Video** | Trend sweep → cited brief → script → licensed assets → FFmpeg render → human review | `redblue.video` |
| **Admin** | FastAPI + HTMX admin for all of the above | `redblue.admin` |

See [`docs/PLAN.md`](docs/PLAN.md) for the full plan and roadmap, and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for how the pieces fit together.

## Quickstart (5 minutes)

```bash
git clone https://github.com/wholefoo/redblue && cd redblue
python -m venv .venv && . .venv/bin/activate
make dev                                   # installs every package in editable mode

redblue new my-site --intent "A SaaS for dog groomers with a blog and FAQ"
cd my-site
RB_ADMIN_EMAIL=you@example.com RB_ADMIN_PASSWORD='a-long-password' python seed.py
redblue dev                                # http://127.0.0.1:8000 and /admin
```

The builder shows its plan and waits for your approval before generating anything. Planned
pages start as drafts with `TODO(editor)` notes; the publish guardrails keep placeholders off
the live site until a human writes them.

### Just the security gate

Add the GitHub Action to any FastAPI repo; see [`action/README.md`](action/README.md). Locally:

```bash
pip install -e packages/gate
redblue-gate scan --path examples/vulnerable-demo     # fails with the planted bugs
redblue-gate fix  --path examples/vulnerable-demo --output-dir /tmp/fixes
```

## How the promises are kept

**Security**
- Strict CSP with per-request nonces, CSRF on every state change, HSTS, secure cookies,
  argon2 passwords, upload sniffing (no SVG), signed URLs, safe redirects, rate limits.
- Content is structured JSON validated by Pydantic: no raw HTML, only safe links,
  allow-listed embeds, required alt text. Jinja autoescape stays on everywhere.
- The Red agent only scans previews it launches itself on loopback; there is no target-URL
  input anywhere. The Blue agent's fixes must include a regression test that fails before
  and passes after, and they are opened as PRs that are never auto-merged.
- Agents never publish, deploy, send or post. Their output is validated against schemas, and
  hostile input (CMS content, scanned code, logs) is fenced as untrusted data.

**Growth** (white-hat only)
- Server-rendered HTML, canonical URLs, sitemaps with hreflang, RSS, OG images, JSON-LD
  per page type (Article, FAQPage, HowTo, QAPage, Product, JobPosting, Event, LocalBusiness,
  DefinedTerm, BreadcrumbList…), `robots.txt` with per-crawler AI controls, `llms.txt`.
- Answer-first blocks, author bylines with credentials, "last updated" dates, citations.
- Cookieless analytics (daily-rotated salted hashes; DNT/GPC respected) with AI-assistant
  referral tracking, goals, funnels, and A/B tests with significance testing.
- Quality threshold blocks thin or near-duplicate programmatic pages and unresolved TODOs;
  comparison pages require a disclosure; only editor-verified testimonials become Review markup.
- Growth, content and repurposing agents produce drafts and recommendations only.

## Bring your own key

Agents use your own Claude API key (`ANTHROPIC_API_KEY`). Without one, everything still works
with deterministic fallbacks (heuristic planning, rule-based triage, built-in fixers). Default
models are set per agent in `redblue.core.config.AgentModels` and can be overridden with
`RB_MODEL_<ROLE>`. Each agent has a daily spend limit (`RB_AGENT_SPEND_LIMIT_USD`), and every
call is logged with tokens and cost (Admin → Observability).

### Expected monthly model cost

Rough estimates using prompt caching and the Batch API for nightly work; your mileage varies.

| Site size | Typical use | Estimated cost / month |
|---|---|---|
| Small (≤ 50 pages, 1–2 PRs/week) | gate triage + fixes, weekly growth report, a few drafts | $2–$8 |
| Medium (≤ 500 pages, daily PRs) | above + content refreshes, ops explanations | $10–$40 |
| Large (programmatic, many contributors) | above at volume | $50+; set spend limits |

## Deploy

Self-hosted only: Docker Compose, bare metal (systemd), Fly.io, Render or Railway. See
[`deploy/README.md`](deploy/README.md). Run `redblue doctor` after deploying.

## Repository layout

```
packages/core        Backend SDK             packages/growth    SEO, AEO/GEO, analytics, marketing
packages/cms         CMS + headless API      packages/observe   Metrics, errors, uptime, ops agent
packages/templates   Page types + site       packages/builder   Builder agent + project template
packages/gate        Red/Blue security gate  packages/platform  Assembled app + seed
packages/cli         `redblue` command       admin/             Admin UI (FastAPI + HTMX)
packages/video       Trending video pipeline
action/              GitHub Action           deploy/            Compose, systemd, platform configs
examples/            vulnerable-demo, starter-site               evals/  Agent evals
```

## Development

```bash
make dev      # editable installs
pytest        # all packages
ruff check .
redblue gate scan --path .   # the project dogfoods its own gate
```

Admin pages in a real browser (skipped by plain `pytest` unless Chromium is available):

```bash
pip install playwright && python -m playwright install chromium
RB_SCREENSHOT_DIR=screenshots pytest packages/platform/tests/test_admin_screens.py
```

It opens every admin page at desktop and phone widths and fails on server errors, console
or CSP errors, pages wider than the screen, and broken upload cards. It saves a full-page
screenshot of each page. CI runs it on every pull request and keeps the screenshots as the
`admin-screenshots` artifact.

**Database migrations** (Alembic). The app upgrades its database at startup, and
`redblue db upgrade` does it by hand (back up first). A database made before migrations
existed is brought up to date by the baseline revision: missing tables, indexes and nullable
columns are added, existing rows are kept.

```bash
redblue db current                    # the database's revision
redblue db upgrade                    # apply pending migrations
redblue db revision -m "Add X to Y"   # after changing a model: write a migration, then review it
redblue db check                      # exit 1 if a model changed without a migration
```

Migrations live in `packages/core/redblue/core/migrations/versions/`. Autogenerate can't
tell a rename from a drop plus an add, so edit renames and data moves by hand. The test
suite fails if the models and migrations drift apart.

Contributors and AI coding sessions: read [`CLAUDE.md`](CLAUDE.md) first.

## License

Apache-2.0. See [LICENSE](LICENSE).
