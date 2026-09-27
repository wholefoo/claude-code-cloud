# Rules for AI coding sessions on this site

- This is a RedBlue site (FastAPI + Jinja2 + HTMX). Keep Jinja autoescape on; never `|safe`.
- Content lives in the CMS, not in templates. Agents create drafts only; humans publish.
- Every change must pass `pytest` and `redblue gate scan`.
- Secrets go in `.env` / the host's secret manager, never in the repo.
- White-hat SEO only: no cloaking, doorway pages, fake reviews or auto-sent outreach.
