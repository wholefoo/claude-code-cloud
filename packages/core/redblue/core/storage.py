"""File storage with upload validation, safe names, and signed URLs.

Local disk in development; any S3-compatible store (MinIO, R2, S3) in production.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Allowlist by sniffed content, never by the client-supplied name or content type.
_SIGNATURES: list[tuple[bytes, int, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", 0, "image/png", ".png"),
    (b"\xff\xd8\xff", 0, "image/jpeg", ".jpg"),
    (b"GIF87a", 0, "image/gif", ".gif"),
    (b"GIF89a", 0, "image/gif", ".gif"),
    (b"%PDF-", 0, "application/pdf", ".pdf"),
]


class UploadRejected(ValueError):
    pass


def sniff(data: bytes) -> tuple[str, str]:
    for magic, offset, mime, ext in _SIGNATURES:
        if data[offset : offset + len(magic)] == magic:
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif", ".avif"
    raise UploadRejected("Unsupported file type. Allowed: PNG, JPEG, GIF, WebP, AVIF, PDF.")


def validate_upload(data: bytes, max_bytes: int) -> tuple[str, str]:
    """Returns (mime, extension). SVG is deliberately not accepted (it can carry script)."""
    if not data:
        raise UploadRejected("Empty upload.")
    if len(data) > max_bytes:
        raise UploadRejected(f"File too large (max {max_bytes // 1024 // 1024} MB).")
    return sniff(data)


def safe_filename(name: str) -> str:
    stem = Path(name).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:60]
    return stem or "file"


@dataclass
class StoredFile:
    key: str
    mime: str
    size: int
    sha256: str


class Storage(Protocol):
    def put(self, data: bytes, original_name: str) -> StoredFile: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def url(self, key: str) -> str: ...


class LocalStorage:
    def __init__(self, root: Path, max_bytes: int, secret: str, url_prefix: str = "/media"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.secret = secret.encode()
        self.url_prefix = url_prefix

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*\.[a-z0-9]{2,5}", key):
            raise UploadRejected("Invalid storage key.")
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise UploadRejected("Invalid storage key.")
        return path

    def put(self, data: bytes, original_name: str) -> StoredFile:
        mime, ext = validate_upload(data, self.max_bytes)
        key = f"{safe_filename(original_name)}-{secrets.token_hex(6)}{ext}"
        self._path(key).write_bytes(data)
        return StoredFile(key, mime, len(data), hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def url(self, key: str) -> str:
        return f"{self.url_prefix}/{key}"

    def signed_url(self, key: str, ttl: int = 3600) -> str:
        expires = int(time.time()) + ttl
        sig = hmac.new(self.secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()
        return f"{self.url_prefix}/private/{key}?expires={expires}&sig={sig}"

    def verify_signature(self, key: str, expires: int, sig: str) -> bool:
        if expires < time.time():
            return False
        good = hmac.new(self.secret, f"{key}:{expires}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig)


class S3Storage:
    """S3-compatible storage (requires ``redblue-core[s3]``)."""

    def __init__(self, bucket: str, max_bytes: int, endpoint_url: str | None = None):
        import boto3  # optional dependency

        self.client = boto3.client("s3", endpoint_url=endpoint_url)
        self.bucket = bucket
        self.max_bytes = max_bytes

    def put(self, data: bytes, original_name: str) -> StoredFile:
        mime, ext = validate_upload(data, self.max_bytes)
        key = f"{safe_filename(original_name)}-{secrets.token_hex(6)}{ext}"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=mime)
        return StoredFile(key, mime, len(data), hashlib.sha256(data).hexdigest())

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def url(self, key: str) -> str:
        return self.signed_url(key, ttl=86400)

    def signed_url(self, key: str, ttl: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl
        )


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort width/height for PNG/GIF/JPEG without Pillow (used for CLS-safe <img>)."""
    import struct

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2 : i + 4])[0]
    return None
