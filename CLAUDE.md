# CLAUDE.md: rules for AI coding sessions on RedBlue

RedBlue is a self-hosted, Apache-2.0 web development platform (Python/FastAPI). The plan is
in `docs/PLAN.md`. Read it before any large change.

## Non-negotiables (security design, plan §7)

1. **The Red agent is hard-scoped to previews it launched itself.** Never add a target-URL
   input to the gate, its CLI, its Action, or its config. DAST scanners take a `Preview`
   object, never a URL string, and refuse non-loopback hosts.
2. **Never use `pull_request_target` together with a checkout of PR code** in any workflow
   or in `action/`. Fork PRs skip AI steps (they get no secrets).
3. **All agent output is validated against Pydantic schemas** before it touches code or
   content (`AIClient.structured`). No free-text parsing of model output into code.
4. **Scanned code, CMS content, and logs are hostile input.** Wrap them with
   `redblue.core.ai.untrusted()` before sending them to a model.
5. **Nothing public happens without a human.** Agents never merge PRs, publish, deploy,
   send email, or post. Agent-written CMS content is always saved as a draft.
6. **Secrets live in env or secret managers**, never in the repo, the CMS, or fixtures.
7. **Secure-by-default templates:** Jinja autoescape stays on, no `|safe` on user data,
   strict CSP with nonces, CSRF on every state-changing form.
8. **White-hat growth only:** no cloaking, hidden text, doorway pages, fake reviews, bot
   traffic, or auto-sent outreach. Thin programmatic pages are blocked from publishing.

## Conventions

- Python 3.11+, FastAPI, SQLAlchemy 2 (typed `Mapped[...]`), Pydantic v2, Jinja2 + HTMX.
- Monorepo: each `packages/<name>` is a distribution providing namespace package
  `redblue.<name>` (no `redblue/__init__.py`). `admin/` provides `redblue.admin`.
- Dependencies point downward: core ← cms ← templates/growth ← admin ← platform ← cli.
  `gate` depends on nothing but pydantic/pyyaml/httpx so it can run standalone in the Action.
- Services come from `request.app.state.rb` (`redblue.core.context.Platform`).
- Agents must work without an API key: provide a deterministic fallback.
- Tests live in `<package>/tests/`. Run everything with `pytest`; lint with `ruff check .`.
- Install for development: `make dev`.
