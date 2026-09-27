"""Signed inbound and outbound webhooks (HMAC-SHA256 over ``timestamp.body``)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import httpx

SIGNATURE_HEADER = "X-RedBlue-Signature"
TIMESTAMP_HEADER = "X-RedBlue-Timestamp"


def sign(secret: str, body: bytes, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp or int(time.time()))
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return ts, f"sha256={mac}"


def verify(secret: str, body: bytes, timestamp: str, signature: str, tolerance: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False  # replay protection
    _, expected = sign(secret, body, ts)
    return hmac.compare_digest(expected, signature or "")


def send(url: str, secret: str, event: str, data: dict, timeout: float = 10.0) -> int:
    body = json.dumps({"event": event, "data": data}, separators=(",", ":")).encode()
    ts, sig = sign(secret, body)
    resp = httpx.post(
        url,
        content=body,
        headers={"Content-Type": "application/json", TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sig},
        timeout=timeout,
        follow_redirects=False,
    )
    return resp.status_code
