"""Users, roles, sessions, API keys and OAuth sign-in.

Sessions are signed, HttpOnly, SameSite=Lax cookies carrying the user id and a
per-user session version (bumped on logout-everywhere / password change).
"""

from __future__ import annotations

import enum
import hashlib
import secrets
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from redblue.core.context import get_db, get_platform
from redblue.core.db import Base, TimestampMixin, utcnow
from redblue.core.security import hash_password, verify_password
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

SESSION_COOKIE = "rb_session"


class Role(enum.IntEnum):
    """Ordered roles: each includes the permissions of those below it."""

    viewer = 10
    writer = 20
    editor = 30
    publisher = 40
    admin = 50


class User(Base, TimestampMixin):
    __tablename__ = "rb_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.viewer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    bio: Mapped[str] = mapped_column(String(2000), default="")
    credentials: Mapped[str] = mapped_column(String(500), default="")  # AEO author signal

    def has_role(self, role: Role) -> bool:
        return self.is_active and self.role >= role


class OAuthIdentity(Base):
    __tablename__ = "rb_oauth_identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("rb_users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))
    subject: Mapped[str] = mapped_column(String(255))


class ApiKey(Base):
    __tablename__ = "rb_api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("rb_users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------- user management


def create_user(
    db: Session, email: str, password: str | None, role: Role = Role.viewer, name: str = ""
) -> User:
    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise ValueError(f"User {email} already exists.")
    user = User(
        email=email,
        name=name or email.split("@")[0],
        password_hash=hash_password(password) if password else None,
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active or not user.password_hash:
        # Spend comparable time so response timing doesn't reveal which emails exist.
        verify_password(_DUMMY_HASH, password)
        return None
    return user if verify_password(user.password_hash, password) else None


_DUMMY_HASH = hash_password("not-a-real-password-000")


def create_api_key(db: Session, user: User, name: str) -> str:
    """Returns the plaintext key once; only a hash is stored."""
    raw = "rbk_" + secrets.token_urlsafe(32)
    db.add(
        ApiKey(
            user_id=user.id,
            name=name,
            prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        )
    )
    db.flush()
    return raw


# ---------------------------------------------------------------- sessions


def _serializer(request: Request) -> URLSafeTimedSerializer:
    secret = get_platform(request).settings.secret_key.get_secret_value()
    return URLSafeTimedSerializer(secret, salt="rb-session")


def login(request: Request, response: Response, user: User) -> None:
    settings = get_platform(request).settings
    token = _serializer(request).dumps({"uid": user.id, "v": user.session_version})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_max_age,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
        path="/",
    )


def logout(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _user_from_session(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    max_age = get_platform(request).settings.session_max_age
    try:
        data = _serializer(request).loads(token, max_age=max_age)
    except BadSignature:
        return None
    user = db.get(User, data.get("uid"))
    if user is None or not user.is_active or user.session_version != data.get("v"):
        return None
    return user


def _user_from_api_key(request: Request, db: Session) -> User | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer rbk_"):
        return None
    raw = auth.split(" ", 1)[1].strip()
    digest = hashlib.sha256(raw.encode()).hexdigest()
    key = db.scalar(select(ApiKey).where(ApiKey.prefix == raw[:12], ApiKey.revoked.is_(False)))
    if key is None or not secrets.compare_digest(key.key_hash, digest):
        return None
    key.last_used_at = utcnow()
    user = db.get(User, key.user_id)
    return user if user and user.is_active else None


def current_user_optional(request: Request, db: Annotated[Session, Depends(get_db)]) -> User | None:
    cached = getattr(request.state, "user", None)
    if cached is not None:
        return cached
    user = _user_from_api_key(request, db) or _user_from_session(request, db)
    request.state.user = user
    return user


def current_user(user: Annotated[User | None, Depends(current_user_optional)]) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return user


def require_role(role: Role):
    def dependency(user: Annotated[User, Depends(current_user)]) -> User:
        if not user.has_role(role):
            raise HTTPException(status_code=403, detail=f"Requires role {role.name}.")
        return user

    return dependency


# ---------------------------------------------------------------- OAuth


class OAuthProvider(BaseModel):
    name: str
    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    userinfo_url: str
    scope: str

    @classmethod
    def github(cls, client_id: str, client_secret: str) -> OAuthProvider:
        return cls(
            name="github",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",  # noqa: S106  # nosec B106 - URL, not a secret
            userinfo_url="https://api.github.com/user",
            scope="read:user user:email",
        )

    @classmethod
    def google(cls, client_id: str, client_secret: str) -> OAuthProvider:
        return cls(
            name="google",
            client_id=client_id,
            client_secret=client_secret,
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",  # noqa: S106  # nosec B106 - URL, not a secret
            userinfo_url="https://openidconnect.googleapis.com/v1/userinfo",
            scope="openid email profile",
        )

    def authorization_redirect(self, redirect_uri: str) -> tuple[str, str]:
        """Returns (url, state). Store ``state`` in a short-lived cookie and verify on callback."""
        state = secrets.token_urlsafe(24)
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scope,
                "state": state,
                "response_type": "code",
            }
        )
        return f"{self.authorize_url}?{query}", state

    def fetch_identity(self, code: str, redirect_uri: str) -> tuple[str, str, str]:
        """Exchange the code; returns (subject, email, name)."""
        with httpx.Client(timeout=10) as client:
            tok = client.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Accept": "application/json"},
            )
            tok.raise_for_status()
            access = tok.json()["access_token"]
            info = client.get(self.userinfo_url, headers={"Authorization": f"Bearer {access}"})
            info.raise_for_status()
            data = info.json()
        subject = str(data.get("sub") or data.get("id"))
        return subject, data.get("email") or "", data.get("name") or data.get("login") or ""


def upsert_oauth_user(db: Session, provider: str, subject: str, email: str, name: str) -> User:
    ident = db.scalar(
        select(OAuthIdentity).where(
            OAuthIdentity.provider == provider, OAuthIdentity.subject == subject
        )
    )
    if ident:
        return db.get(User, ident.user_id)
    if not email:
        raise ValueError("OAuth provider did not return a verified email address.")
    user = db.scalar(select(User).where(User.email == email.lower())) or create_user(
        db, email, None, Role.viewer, name
    )
    db.add(OAuthIdentity(user_id=user.id, provider=provider, subject=subject))
    db.flush()
    return user
