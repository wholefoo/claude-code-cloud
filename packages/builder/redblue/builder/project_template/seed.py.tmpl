"""Create the admin user and draft pages from content/plan.json.

    RB_ADMIN_EMAIL=you@example.com RB_ADMIN_PASSWORD=... python seed.py
"""

import os
from pathlib import Path

from redblue.builder.scaffold import plan_from_project, seed_from_plan
from redblue.core.config import get_settings
from redblue.core.db import Database
from redblue.platform.seed import ensure_admin

settings = get_settings()
db = Database(settings.database_url)
db.create_all()
with db.session() as s:
    admin = ensure_admin(s, os.environ["RB_ADMIN_EMAIL"], os.environ["RB_ADMIN_PASSWORD"])
    n = seed_from_plan(s, plan_from_project(Path(__file__).parent), admin)
print(f"Created {n} draft pages. Sign in at {settings.base_url}/admin to write them.")
