# RedBlue security gate: GitHub Action

Every pull request is scanned (static analysis, dependency audit, and dynamic checks against a
preview of your app that the action launches itself). Verified fixes, each with a regression
test that fails before and passes after, arrive as pull requests for you to review.

## Quickstart (60 seconds)

1. Add `ANTHROPIC_API_KEY` as a repository secret (optional; use a spend-limited key).
2. Add `.redblue.yml` to your repo:

   ```yaml
   stack: fastapi
   app: myapp.main:app        # the ASGI app the gate launches as a preview
   severity_threshold: high   # findings at or above this fail the check
   ```

3. Add `.github/workflows/redblue.yml`:

   ```yaml
   name: RedBlue
   on: pull_request            # never pull_request_target
   permissions:
     contents: write           # push fix branches
     pull-requests: write      # open fix PRs and comment
     security-events: write    # upload SARIF to code scanning
   jobs:
     gate:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262  # v4.4.0
           with:
             fetch-depth: 0    # lets the Red agent diff against the base branch
         - uses: wholefoo/redblue/action@17275f33efd6116eafeadf65042d8682f44945ae  # main, 2026-09-27
           with:
             anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
   ```

4. Make the `RedBlue` check required in your branch protection rules.

### Using Postgres in the preview

```yaml
    services:
      postgres:
        image: postgres:16
        env: { POSTGRES_PASSWORD: postgres }
        ports: ["5432:5432"]
        options: --health-cmd pg_isready --health-interval 5s --health-retries 10
    env:
      RB_DATABASE_URL: postgresql+psycopg://postgres:postgres@localhost:5432/postgres
```

and set `seed_command: python -m myapp.seed` in `.redblue.yml`.

## Safety design

- **No target URLs.** The gate only scans the preview it launched on `127.0.0.1`. There is
  no input, config key or CLI flag for pointing it anywhere else.
- **Fork PRs skip AI steps.** Use the plain `pull_request` trigger. Fork PRs get no secrets,
  and the gate detects forks and runs scanners only. Never use `pull_request_target` with a
  checkout of PR code.
- **Fixes are never auto-merged.** Blue agent PRs wait for human review like any other PR.
- **Your key, your spend.** `spend_limit_usd` in `.redblue.yml` caps AI spend per run.

## Inputs

| Input | Default | Description |
|---|---|---|
| `anthropic_api_key` | `""` | Your Claude API key (optional) |
| `config` | `.redblue.yml` | Config path |
| `fail_on` | from config | Severity threshold |
| `open_fix_prs` | `true` | Open PRs for verified fixes |
| `upload_sarif` | `true` | Upload to code scanning |
| `python_version` | `3.12` | Python for the app under test |
