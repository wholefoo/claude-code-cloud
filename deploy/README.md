# Deploying RedBlue

RedBlue is self-hosted: the project runs no hosted service. Pick one:

| Option | Command | Notes |
|---|---|---|
| Docker Compose (most users) | `docker compose -f deploy/compose/docker-compose.yml up -d` | Postgres, worker and Caddy with automatic HTTPS |
| Bare metal (Debian/Ubuntu + systemd) | `sudo bash deploy/systemd/install.sh example.com` | No containers; hardened systemd units |
| Fly.io | `fly launch --copy-config -c deploy/platforms/fly.toml` | Add `fly postgres` |
| Render | Blueprint from `deploy/platforms/render.yaml` | |
| Railway | `deploy/platforms/railway.json` | Add a Postgres plugin |
| Gate only | See [`action/README.md`](../action/README.md) | GitHub Action, no server |

After deploying:

```bash
redblue createadmin you@example.com   # inside the container/venv
redblue doctor                        # checks config, DB, storage, secrets, scanners
redblue backup                        # database + media; schedule it with cron
redblue migrate                       # after upgrading: creates new tables
```

Required settings: `RB_ENV=prod`, `RB_BASE_URL=https://…`, a 32+ character `RB_SECRET_KEY`,
and a Postgres `RB_DATABASE_URL`. `redblue doctor` fails until they are set.
