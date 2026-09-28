"""Baseline: the schema as of the first release with migrations.

Written to work on any database. On an empty one it creates everything. On a database made
before migrations existed (by ``create_all`` plus additive column upgrades) it creates only
the tables and indexes that are missing and adds missing *nullable* columns, then Alembic
records this revision so later migrations apply normally. A missing NOT NULL column can't be
filled in safely, so that stops the upgrade with a clear error instead of guessing.

Generated with Alembic's autogenerate from the models, then wrapped in ``_table`` and
``_index``. Later migrations are ordinary Alembic migrations: ``redblue db revision``.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def _table(name: str, *items, **kw) -> None:
    """Create the table, or bring an existing (pre-migration) one up to this revision."""
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(name):
        op.create_table(name, *items, **kw)
        return
    present = {c["name"] for c in insp.get_columns(name)}
    for col in items:
        if not isinstance(col, sa.Column) or col.name in present:
            continue
        if not col.nullable:
            raise RuntimeError(
                f"{name}.{col.name} is missing and required; add it by hand, then upgrade again."
            )
        op.add_column(name, col)


def _index(name: str, table: str, columns: list[str], **kw) -> None:
    if name not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns, **kw)


def upgrade() -> None:
    _table(
        "rb_agent_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("agent", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("task", sa.String(length=200), nullable=False),
        sa.Column("page", sa.String(length=500), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_agent_calls_agent"), "rb_agent_calls", ["agent"], unique=False)
    _index(op.f("ix_rb_agent_calls_created_at"), "rb_agent_calls", ["created_at"], unique=False)
    _table(
        "rb_cms_media",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("mime", sa.String(length=100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("alt", sa.String(length=300), nullable=False),
        sa.Column("caption", sa.String(length=500), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    _table(
        "rb_cms_redirects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("from_path", sa.String(length=500), nullable=False),
        sa.Column("to_path", sa.String(length=500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("automatic", sa.Boolean(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("from_path"),
    )
    _table(
        "rb_growth_banners",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(length=300), nullable=False),
        sa.Column("cta_label", sa.String(length=60), nullable=False),
        sa.Column("cta_url", sa.String(length=500), nullable=False),
        sa.Column("path_prefix", sa.String(length=200), nullable=False),
        sa.Column("frequency_days", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _table(
        "rb_growth_experiments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=True),
        sa.Column("variants", sa.JSON(), nullable=False),
        sa.Column("goal", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    _table(
        "rb_growth_goals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("goal", sa.String(length=100), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("visitor", sa.String(length=32), nullable=False),
        sa.Column("variants", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_growth_goals_day"), "rb_growth_goals", ["day"], unique=False)
    _index(op.f("ix_rb_growth_goals_goal"), "rb_growth_goals", ["goal"], unique=False)
    _index(op.f("ix_rb_growth_goals_ts"), "rb_growth_goals", ["ts"], unique=False)
    _index(op.f("ix_rb_growth_goals_visitor"), "rb_growth_goals", ["visitor"], unique=False)
    _table(
        "rb_growth_leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source_path", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("handled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_growth_leads_created_at"), "rb_growth_leads", ["created_at"], unique=False)
    _table(
        "rb_growth_pageviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("visitor", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("referrer_host", sa.String(length=255), nullable=False),
        sa.Column("utm_source", sa.String(length=100), nullable=False),
        sa.Column("utm_medium", sa.String(length=100), nullable=False),
        sa.Column("utm_campaign", sa.String(length=200), nullable=False),
        sa.Column("ai_assistant", sa.String(length=50), nullable=False),
        sa.Column("device", sa.String(length=10), nullable=False),
        sa.Column("variants", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_growth_pageviews_day"), "rb_growth_pageviews", ["day"], unique=False)
    _index(op.f("ix_rb_growth_pageviews_path"), "rb_growth_pageviews", ["path"], unique=False)
    _index(op.f("ix_rb_growth_pageviews_source"), "rb_growth_pageviews", ["source"], unique=False)
    _index(op.f("ix_rb_growth_pageviews_ts"), "rb_growth_pageviews", ["ts"], unique=False)
    _index(op.f("ix_rb_growth_pageviews_visitor"), "rb_growth_pageviews", ["visitor"], unique=False)
    _table(
        "rb_growth_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(
        op.f("ix_rb_growth_reports_created_at"), "rb_growth_reports", ["created_at"], unique=False
    )
    _table(
        "rb_growth_salts",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("salt", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )
    _table(
        "rb_growth_search_console",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("query", sa.String(length=500), nullable=False),
        sa.Column("page", sa.String(length=500), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(
        op.f("ix_rb_growth_search_console_day"), "rb_growth_search_console", ["day"], unique=False
    )
    _index(
        op.f("ix_rb_growth_search_console_page"), "rb_growth_search_console", ["page"], unique=False
    )
    _index(
        op.f("ix_rb_growth_search_console_query"),
        "rb_growth_search_console",
        ["query"],
        unique=False,
    )
    _table(
        "rb_growth_social_drafts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=30), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _table(
        "rb_growth_subscribers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("list_name", sa.String(length=50), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_at", sa.DateTime(), nullable=False),
        sa.Column("consent_source", sa.String(length=500), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("unsubscribed_at", sa.DateTime(), nullable=True),
        sa.Column("lead_magnet", sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_growth_subscribers_email"), "rb_growth_subscribers", ["email"], unique=True)
    _table(
        "rb_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("run_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_jobs_name"), "rb_jobs", ["name"], unique=False)
    _index(op.f("ix_rb_jobs_run_at"), "rb_jobs", ["run_at"], unique=False)
    _index(op.f("ix_rb_jobs_status"), "rb_jobs", ["status"], unique=False)
    _table(
        "rb_obs_deploys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_obs_deploys_created_at"), "rb_obs_deploys", ["created_at"], unique=False)
    _table(
        "rb_obs_error_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("exc_type", sa.String(length=200), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("top_frame", sa.String(length=500), nullable=False),
        sa.Column("first_seen", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("sample_traceback", sa.Text(), nullable=False),
        sa.Column("sample_request_id", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(
        op.f("ix_rb_obs_error_groups_fingerprint"),
        "rb_obs_error_groups",
        ["fingerprint"],
        unique=True,
    )
    _index(
        op.f("ix_rb_obs_error_groups_last_seen"), "rb_obs_error_groups", ["last_seen"], unique=False
    )
    _index(op.f("ix_rb_obs_error_groups_status"), "rb_obs_error_groups", ["status"], unique=False)
    _table(
        "rb_obs_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("route", sa.String(length=300), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("deploy_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    _index("ix_rb_obs_requests_route_ts", "rb_obs_requests", ["route", "ts"], unique=False)
    _index(op.f("ix_rb_obs_requests_ts"), "rb_obs_requests", ["ts"], unique=False)
    _table(
        "rb_obs_uptime_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=300), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Float(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_obs_uptime_checks_path"), "rb_obs_uptime_checks", ["path"], unique=False)
    _index(op.f("ix_rb_obs_uptime_checks_ts"), "rb_obs_uptime_checks", ["ts"], unique=False)
    _table(
        "rb_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column(
            "role",
            sa.Enum("viewer", "writer", "editor", "publisher", "admin", name="role"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("session_version", sa.Integer(), nullable=False),
        sa.Column("bio", sa.String(length=2000), nullable=False),
        sa.Column("credentials", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_users_email"), "rb_users", ["email"], unique=True)
    _table(
        "rb_video_trends",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("topic", sa.String(length=300), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("signal", sa.JSON(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_video_trends_fetched_at"), "rb_video_trends", ["fetched_at"], unique=False)
    _index(op.f("ix_rb_video_trends_score"), "rb_video_trends", ["score"], unique=False)
    _index(op.f("ix_rb_video_trends_topic"), "rb_video_trends", ["topic"], unique=False)
    _table(
        "rb_api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["rb_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_api_keys_prefix"), "rb_api_keys", ["prefix"], unique=False)
    _table(
        "rb_cms_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("collection", sa.String(length=50), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("translation_of", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("seo", sa.JSON(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "in_review",
                "approved",
                "scheduled",
                "published",
                "archived",
                name="status",
            ),
            nullable=False,
        ),
        sa.Column("live", sa.JSON(), nullable=True),
        sa.Column("live_slug", sa.String(length=200), nullable=True),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("publish_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("content_updated_at", sa.DateTime(), nullable=True),
        sa.Column("ai_generated", sa.Boolean(), nullable=False),
        sa.Column("ai_label", sa.Boolean(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["rb_users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["translation_of"],
            ["rb_cms_entries.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("collection", "slug", "locale"),
    )
    _index(op.f("ix_rb_cms_entries_collection"), "rb_cms_entries", ["collection"], unique=False)
    _index(op.f("ix_rb_cms_entries_slug"), "rb_cms_entries", ["slug"], unique=False)
    _index(op.f("ix_rb_cms_entries_status"), "rb_cms_entries", ["status"], unique=False)
    _table(
        "rb_oauth_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["rb_users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _table(
        "rb_obs_error_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("route", sa.String(length=300), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["rb_obs_error_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(
        op.f("ix_rb_obs_error_events_group_id"), "rb_obs_error_events", ["group_id"], unique=False
    )
    _index(op.f("ix_rb_obs_error_events_ts"), "rb_obs_error_events", ["ts"], unique=False)
    _table(
        "rb_video_projects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trend_id", sa.Integer(), nullable=True),
        sa.Column("topic", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("brief", sa.JSON(), nullable=True),
        sa.Column("script", sa.JSON(), nullable=True),
        sa.Column("assets", sa.JSON(), nullable=False),
        sa.Column("render_path", sa.String(length=1000), nullable=True),
        sa.Column("template", sa.String(length=40), nullable=True),
        sa.Column("formats", sa.JSON(), nullable=True),
        sa.Column("renders", sa.JSON(), nullable=True),
        sa.Column("problems", sa.JSON(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("ai_generated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["trend_id"],
            ["rb_video_trends.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_video_projects_status"), "rb_video_projects", ["status"], unique=False)
    _table(
        "rb_cms_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entry_id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=True),
        sa.Column("by_agent", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["entry_id"], ["rb_cms_entries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_cms_revisions_entry_id"), "rb_cms_revisions", ["entry_id"], unique=False)
    _table(
        "rb_video_publications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=True),
        sa.Column("format", sa.String(length=10), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=200), nullable=True),
        sa.Column("privacy", sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["rb_video_projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("url"),
    )
    _index(
        op.f("ix_rb_video_publications_platform"),
        "rb_video_publications",
        ["platform"],
        unique=False,
    )
    _index(
        op.f("ix_rb_video_publications_project_id"),
        "rb_video_publications",
        ["project_id"],
        unique=False,
    )
    _table(
        "rb_video_uploads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("external_ref", sa.String(length=200), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("privacy", sa.String(length=40), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=200), nullable=False),
        sa.Column("published_by", sa.String(length=200), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["rb_video_projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(op.f("ix_rb_video_uploads_project_id"), "rb_video_uploads", ["project_id"], unique=False)
    _index(op.f("ix_rb_video_uploads_status"), "rb_video_uploads", ["status"], unique=False)
    _table(
        "rb_video_metrics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("publication_id", sa.Integer(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("avg_view_pct", sa.Float(), nullable=True),
        sa.Column("avg_view_seconds", sa.Float(), nullable=True),
        sa.Column("no_views", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["publication_id"], ["rb_video_publications.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    _index(
        op.f("ix_rb_video_metrics_publication_id"),
        "rb_video_metrics",
        ["publication_id"],
        unique=False,
    )
    _index(op.f("ix_rb_video_metrics_taken_at"), "rb_video_metrics", ["taken_at"], unique=False)


def downgrade() -> None:
    raise NotImplementedError("The baseline can't be downgraded: it would drop every table.")
