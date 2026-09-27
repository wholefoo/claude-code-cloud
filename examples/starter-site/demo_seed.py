"""Publish the platform's demo content so the starter site has real pages to browse.

    RB_ADMIN_EMAIL=you@example.com RB_ADMIN_PASSWORD='a long password' python demo_seed.py
"""

import os

from redblue.core.config import get_settings
from redblue.core.db import Database
from redblue.platform.seed import ensure_admin, seed

db = Database(get_settings().database_url)
db.create_all()
with db.session() as s:
    n = seed(s, ensure_admin(s, os.environ["RB_ADMIN_EMAIL"], os.environ["RB_ADMIN_PASSWORD"]))
print(f"Published {n} demo entries.")
