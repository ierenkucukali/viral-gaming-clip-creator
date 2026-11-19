"""Cloudflare R2 (S3-compatible) hosting for the Buffer videos (local fork).

Buffer's API takes no file: it fetches each video from a public link when the post is published.
R2 is the cheapest place for that: 10 GB storage free and NO download (egress) fees, however often
Buffer fetches a video. Each clip gets an unguessable name (sc/<32 hex>.mp4) under the bucket's
public r2.dev address (or a custom domain); bufferpost deletes it after it was posted.

Requests are signed with AWS Signature V4 (region "auto", service "s3"); no extra package needed.
The signer is checked against AWS's published example in the tests.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .errors import SharkyClipsError

REGION = "auto"
SERVICE = "s3"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def _http():  # the `requests` module (tests swap in a fake)
    import requests

    return requests


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: dict[str, str], headers: dict[str, str], payload_sha: str,
         access_key: str, secret_key: str, region: str = REGION, now: datetime | None = None) -> dict[str, str]:
    """The headers of a SigV4-signed request (Authorization, x-amz-date, x-amz-content-sha256, host)."""
    now = now or datetime.now(timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    hdrs = {k.lower(): str(v).strip() for k, v in headers.items()}
    hdrs.update({"host": host, "x-amz-content-sha256": payload_sha, "x-amz-date": amz_date})
    signed = ";".join(sorted(hdrs))
    canonical = "\n".join([
        method, quote(path, safe="/~"),
        "&".join(f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(query.items())),
        "".join(f"{k}:{hdrs[k]}\n" for k in sorted(hdrs)), signed, payload_sha])
    scope = f"{day}/{region}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), SERVICE), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k: v for k, v in hdrs.items() if k != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return out


class R2:
    def __init__(self, cfg: dict):
        self.account = str(cfg.get("account_id") or "").strip()
        self.access = str(cfg.get("access_key_id") or "").strip()
        self.secret = str(cfg.get("secret_access_key") or "").strip()
        self.bucket = str(cfg.get("bucket") or "sharkyclips").strip()
        self.public = str(cfg.get("public_url") or "").strip().rstrip("/")
        self.host = f"{self.account}.r2.cloudflarestorage.com"

    def _req(self, method: str, key: str, data=None, payload_sha: str = EMPTY_SHA, extra: dict | None = None,
             timeout: int = 60):
        path = f"/{self.bucket}/{key}" if key else f"/{self.bucket}"
        h = sign(method, self.host, path, {}, extra or {}, payload_sha, self.access, self.secret)
        url = f"https://{self.host}{quote(path, safe='/~')}"
        try:
            return _http().request(method, url, data=data, headers={**(extra or {}), **h}, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - network
            raise SharkyClipsError(f"Cloudflare R2 can't be reached: {exc.__class__.__name__}.",
                                   "Check the internet connection and the Account ID.") from exc

    def upload(self, path: Path) -> dict:
        """Upload a clip under a new unguessable name; returns {"url", "object", "bytes"}."""
        key = f"sc/{secrets.token_hex(16)}.mp4"
        sha = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                sha.update(chunk)
        size = path.stat().st_size
        with open(path, "rb") as fh:
            r = self._req("PUT", key, data=fh, payload_sha=sha.hexdigest(), timeout=1800,
                          extra={"Content-Type": "video/mp4", "Content-Length": str(size)})
        if r.status_code not in (200, 201):
            raise SharkyClipsError(f"Cloudflare R2 refused the upload (HTTP {r.status_code}).", _hint(r))
        return {"url": f"{self.public}/{key}", "object": key, "bytes": size}

    def delete(self, key: str) -> bool:
        try:
            r = self._req("DELETE", key)
        except SharkyClipsError:
            return False
        return r.status_code in (200, 204, 404)

    def test(self) -> dict:
        """Write a tiny file, read it back through the public address, delete it."""
        if not (self.account and self.access and self.secret and self.public):
            return {"ok": False, "error": "Enter the Account ID, Access Key ID, Secret Access Key and public r2.dev address."}
        if not self.public.startswith("https://"):
            return {"ok": False, "error": "The public address must start with https:// (e.g. https://pub-….r2.dev)."}
        key = f"sc/_test-{secrets.token_hex(6)}.txt"
        body = b"sharkyclips ok"
        try:
            r = self._req("PUT", key, data=body, payload_sha=hashlib.sha256(body).hexdigest(),
                          extra={"Content-Type": "text/plain", "Content-Length": str(len(body))})
        except SharkyClipsError as exc:
            return {"ok": False, "error": str(exc), "hint": exc.hint}
        if r.status_code not in (200, 201):
            return {"ok": False, "error": f"Cloudflare R2 refused the keys (HTTP {r.status_code}).", "hint": _hint(r)}
        try:
            g = _http().get(f"{self.public}/{key}", timeout=30)
            public_ok = g.status_code == 200 and g.content == body
        except Exception:  # noqa: BLE001
            public_ok = False
        self.delete(key)
        if not public_ok:
            return {"ok": False, "error": "Uploading works, but the public address doesn't serve the file.",
                    "hint": "R2 > sharkyclips > Settings > Public Development URL (r2.dev): Enable, then copy that address."}
        return {"ok": True, "plan": f"Cloudflare R2 · {self.bucket}"}


def _hint(r) -> str:
    text = (getattr(r, "text", "") or "")[:300]
    if "NoSuchBucket" in text:
        return "The bucket name doesn't exist: R2 > Create bucket 'sharkyclips' (or type your bucket's name)."
    if "SignatureDoesNotMatch" in text or "InvalidAccessKeyId" in text or r.status_code in (401, 403):
        return "Check the Access Key ID / Secret Access Key (R2 > Manage API tokens, Object Read & Write)."
    return "R2 > Manage API tokens: the token needs Object Read & Write on this bucket."
