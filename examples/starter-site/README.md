# Starter Site

A SaaS product for small teams with a blog, docs, FAQ, glossary and newsletter

Built with [RedBlue](https://github.com/redblue-dev/redblue). This is a plain FastAPI
project: you own all of it.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
# .env was generated with a fresh secret key (it is git-ignored)
RB_ADMIN_EMAIL=you@example.com RB_ADMIN_PASSWORD='choose-a-long-password' python seed.py
redblue dev            # or: uvicorn main:app --reload
```

Open http://localhost:8000/admin, write the draft pages (placeholders marked
`TODO(editor)` can't be published), then submit → approve → publish.

## Plan

- **Home** (`home`): Explain the offer and route visitors
- **Blog** (`blog_index`): Latest articles
- **Documentation** (`knowledge_base`): Guides and reference
- **Pricing** (`pricing`): Plans and FAQs
- **Features** (`features`): What the product does
- **FAQ** (`faq`): Answers to common questions
- **Glossary** (`glossary_index`): Definitions of key terms
- **About** (`about`): Who is behind the site
- **Contact** (`contact`): How to get in touch
- **Privacy policy** (`privacy`): How personal data is handled

## Security

Every pull request runs the RedBlue gate (`.github/workflows/redblue.yml`). Check
`redblue gate scan` locally before pushing.
