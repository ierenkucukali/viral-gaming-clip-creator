"""Preview a video link WITHOUT downloading it (local fork: trim a 24 h VOD before the download).

probe(url): yt-dlp reads the page only (title, length, channel) and the video's storyboard (the
small preview pictures YouTube shows when you hover the progress bar: sprite sheets, a picture every
few seconds). frame(token, t): one sprite sheet is fetched (~50 KB) and the picture for t is cut out.
(Reading the video stream itself is refused by YouTube's servers, and downloading only a part of a
VOD runs at about real-time speed, so the app downloads the whole video at full speed after you pick
the part, and cuts it locally.) Only addresses found here are ever opened (a token in memory), never
one the browser sends, and each is checked to be public first (netguard). Tokens live an hour.
"""

from __future__ import annotations

import secrets
import threading
import time

from .errors import SharkyClipsError, require_module

TTL_S = 3600
_lock = threading.Lock()
_links: dict[str, dict] = {}
_frames: dict[tuple[str, int], bytes] = {}


def _storyboard(info: dict) -> dict | None:
    sb = [f for f in info.get("formats") or [] if "storyboard" in str(f.get("format_note") or "")
          and f.get("fragments") and f.get("rows") and f.get("columns")]
    if not sb:
        return None
    f = max(sb, key=lambda f: f.get("width") or 0)
    return {"frags": [{"url": x["url"], "duration": float(x.get("duration") or 0)} for x in f["fragments"]],
            "rows": int(f["rows"]), "cols": int(f["columns"]), "w": int(f.get("width") or 160),
            "h": int(f.get("height") or 90), "headers": f.get("http_headers") or {}}


def probe(url: str) -> dict:
    from . import netguard
    from .ingest import _YdlLogger, js_runtimes

    netguard.check_public_url(url)
    yt_dlp = require_module("yt_dlp", "yt-dlp[default]")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True, "socket_timeout": 30,
                               "retries": 3, "logger": _YdlLogger(),
                               **({"js_runtimes": js_runtimes()} if js_runtimes() else {})}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        from .ingest import _download_error

        raise _download_error(exc, url) from exc
    if info.get("_type") == "playlist":
        raise SharkyClipsError("That link is a playlist or channel.", "Paste the link of one video.")
    if info.get("is_live"):
        raise SharkyClipsError("This stream is live right now.", "Use the YouTube LIVE card, or wait until it ends.")
    stream = _storyboard(info)
    if stream:
        try:
            for fr in stream["frags"][:1]:
                netguard.check_public_url(fr["url"])
        except SharkyClipsError:
            stream = None
    token = secrets.token_urlsafe(12)
    with _lock:
        now = time.time()
        for k in [k for k, v in _links.items() if v["at"] < now - TTL_S]:
            _links.pop(k, None)
        _links[token] = {"url": url, "stream": stream, "at": now, "duration": float(info.get("duration") or 0)}
    return {"token": token, "title": info.get("title") or info.get("id") or "video",
            "duration": float(info.get("duration") or 0), "uploader": info.get("uploader") or info.get("channel"),
            "thumbnail": info.get("thumbnail"), "frames": bool(stream)}


def frame(token: str, t: float) -> bytes:
    with _lock:
        link = _links.get(token)
    if not link or link["at"] < time.time() - TTL_S:
        raise SharkyClipsError("This preview expired.", "Press Preview again.")
    sb = link["stream"]
    if not sb:
        raise SharkyClipsError("No preview pictures for this link.")
    t = max(0.0, min(float(t), (link["duration"] or float(t)) - 0.5))
    per = sb["rows"] * sb["cols"]
    start = 0.0
    for n, fr in enumerate(sb["frags"]):  # the sprite sheet that covers t
        if t < start + fr["duration"] or n == len(sb["frags"]) - 1:
            break
        start += fr["duration"]
    step = (fr["duration"] or 1.0) / per
    i = max(0, min(per - 1, int((t - start) / step)))
    key = (token, n * 1000 + i)
    with _lock:
        if key in _frames:
            return _frames[key]
    sheet = _sheet(token, n, fr["url"], sb["headers"])
    import io

    from PIL import Image

    im = Image.open(io.BytesIO(sheet)).convert("RGB")
    w, h = im.width // sb["cols"], im.height // sb["rows"]
    tile = im.crop(((i % sb["cols"]) * w, (i // sb["cols"]) * h, (i % sb["cols"] + 1) * w, (i // sb["cols"] + 1) * h))
    if tile.width < 480:
        tile = tile.resize((480, round(480 * tile.height / max(tile.width, 1))), Image.LANCZOS)
    buf = io.BytesIO()
    tile.save(buf, "JPEG", quality=82)
    with _lock:
        if len(_frames) > 600:
            _frames.clear()
        _frames[key] = buf.getvalue()
    return _frames[key]


_sheets: dict[tuple[str, int], bytes] = {}


def _sheet(token: str, n: int, url: str, headers: dict) -> bytes:
    with _lock:
        if (token, n) in _sheets:
            return _sheets[(token, n)]
    import requests

    from . import netguard

    netguard.check_public_url(url)
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200 or not r.content:
        raise SharkyClipsError("Couldn't load that picture.")
    with _lock:
        if len(_sheets) > 200:
            _sheets.clear()
        _sheets[(token, n)] = r.content
    return r.content
