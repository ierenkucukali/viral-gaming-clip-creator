"""Buffer publishing (local fork): picked clips go to Buffer, Buffer posts them to Instagram Reels,
TikTok and YouTube Shorts at the times this app gives them.

How it works
  * Keys: the Buffer API key (publish.buffer.com > Settings > API) and a Cloudinary account
    (cloud name + API key + API secret) are stored encrypted in <settings>/publish_keys.sec.
  * Buffer's API has no file upload: a video must be at a public, stable https link until it is
    published. So each clip is uploaded once to the user's own Cloudinary (folder "sharkyclips")
    right before it goes into Buffer, and deleted there after every platform has published it.
  * You pick clips on the Gaming page ("Send to Buffer"). Every pick becomes one item per platform
    in the outbox (<settings>/buffer_outbox.json) with its own time slot: `per_day` posts a day per
    platform, spread evenly between `window_start` and `window_end` (local time) or on fixed
    `times`. Buffer's own posting schedule is not used (the API can't change it): every post is
    created with mode customScheduled + dueAt.
  * Buffer's free plan keeps at most 10 scheduled posts per channel, so only the next `queue_cap`
    items per platform are in Buffer at once; the rest wait here and go in as slots free up
    (the publish loop calls tick() about once a minute; Buffer keeps posting while this PC is off).
  * Request budget: the free plan allows 600 requests a day. A tick only calls Buffer when an item
    goes in (1 request) or a scheduled item is past its time (status check, at most every 15 min).

Docs (2026-11-19): developers.buffer.com (GraphQL at https://api.buffer.com, Bearer key; createPost
with assets[].video.url, metadata.youtube{title, categoryId, privacy, madeForKids, notifySubscribers},
metadata.instagram{type: reel, shouldShareToFeed, firstComment}, metadata.tiktok{isAiGenerated}).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, secretstore
from .errors import SharkyClipsError
from .project import write_json_atomic

API = "https://api.buffer.com"
CLOUDINARY = "https://api.cloudinary.com/v1_1"
KEY_FILE = "publish_keys.sec"
STATE_FILE = "buffer_outbox.json"
PLATFORMS = ("instagram", "tiktok", "youtube")
LABELS = {"instagram": "Instagram Reels", "tiktok": "TikTok", "youtube": "YouTube Shorts"}
YT_PRIVACY = ("public", "unlisted", "private")
YT_CATEGORIES = {"20": "Gaming", "24": "Entertainment", "23": "Comedy", "22": "People & Blogs", "17": "Sports"}
LEAD_MIN = 10  # a new slot is at least this far ahead
SYNC_EVERY_S = 15 * 60
DAILY_BUDGET = 500  # Buffer free plan: 600 requests / 24 h (shared with Buffer's MCP); keep a margin
MAX_PUSH_PER_TICK = 4
MAX_UPLOAD_BYTES = 99 * 1024 * 1024  # Cloudinary free plan: 100 MB per video
CAPTION_MAX = {"instagram": 2196, "tiktok": 2200, "youtube": 5000}
LOG_MAX = 300
FINAL = ("sent", "cancelled", "error")

_lock = threading.RLock()


class BufferError(SharkyClipsError):
    def __init__(self, message: str, hint: str | None = None, code: str = ""):
        super().__init__(message, hint)
        self.code = code


# ===================================================================== keys


def _key_path() -> Path:
    return config.config_dir() / KEY_FILE


def _keys() -> dict:
    try:
        return secretstore.load(_key_path()) or {}
    except Exception:  # noqa: BLE001 - a broken key file counts as "no keys"
        return {}


def _mask(v: str | None) -> str | None:
    return f"{v[:4]}…{v[-4:]}" if v and len(v) > 10 else ("•••" if v else None)


def save_keys(body: dict) -> dict:
    """{"buffer": "<key>"|"" , "cloudinary": {"cloud_name", "api_key", "api_secret"}|{}}: "" forgets."""
    keys = _keys()
    if "buffer" in body:
        k = str(body.get("buffer") or "").strip()
        if k:
            if len(k) < 16 or any(c.isspace() for c in k):
                raise SharkyClipsError("That doesn't look like a Buffer API key.",
                                       "publish.buffer.com > Settings > API > copy the whole key.")
            keys["buffer"] = k
        else:
            keys.pop("buffer", None)
    if "cloudinary" in body:
        c = body.get("cloudinary") or {}
        if not any(str(c.get(f) or "").strip() for f in ("cloud_name", "api_key", "api_secret")):
            keys.pop("cloudinary", None)
        else:
            old = keys.get("cloudinary") or {}
            new = {f: str(c.get(f) or "").strip() or old.get(f, "") for f in ("cloud_name", "api_key", "api_secret")}
            if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", new["cloud_name"] or ""):
                raise SharkyClipsError("The Cloudinary cloud name looks wrong.",
                                       "Cloudinary console > Dashboard: 'Cloud name' (letters, digits, - and _).")
            if not new["api_key"] or not new["api_secret"]:
                raise SharkyClipsError("Enter the Cloudinary API key and API secret.",
                                       "Cloudinary console > Settings > API Keys.")
            keys["cloudinary"] = new
    secretstore.save(_key_path(), keys)
    if "buffer" in body:
        with _lock:
            st = load_state()
            st["paused"] = None
            _save_state(st)
    return keys_view()


def keys_view() -> dict:
    k = _keys()
    c = k.get("cloudinary") or {}
    return {"buffer": _mask(k.get("buffer")), "has_buffer": bool(k.get("buffer")),
            "cloudinary": {"cloud_name": c.get("cloud_name") or None, "api_key": _mask(c.get("api_key")),
                           "has_secret": bool(c.get("api_secret"))},
            "has_cloudinary": bool(c.get("cloud_name") and c.get("api_key") and c.get("api_secret"))}


# ===================================================================== settings


DEFAULTS = {
    "enabled": True,
    "organization_id": "",
    "organization_name": "",
    "channels": {p: "" for p in PLATFORMS},
    "channel_names": {p: "" for p in PLATFORMS},
    "platforms": {p: True for p in PLATFORMS},
    "per_day": {p: 10 for p in PLATFORMS},
    "window_start": "10:00",
    "window_end": "23:00",
    "times": [],
    "queue_cap": 10,
    "hashtags": "#gta6 #gta #gaming #streamer #fyp",
    "youtube": {"privacy": "public", "category": "20", "made_for_kids": False, "notify": True},
    "instagram": {"share_to_feed": True, "first_comment": ""},
    "ai_label": False,
    "delete_after": True,
}


def _hhmm(v) -> str | None:
    m = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", str(v or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def settings() -> dict:
    raw = config.get("buffer") or {}
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k in s and isinstance(s[k], dict) and isinstance(v, dict):
            s[k].update({kk: vv for kk, vv in v.items() if kk in s[k]})
        elif k in s:
            s[k] = v
    s["enabled"] = s["enabled"] is not False
    s["per_day"] = {p: _int(s["per_day"].get(p), 1, 50, 10) for p in PLATFORMS}
    s["platforms"] = {p: bool(s["platforms"].get(p)) for p in PLATFORMS}
    s["window_start"] = _hhmm(s["window_start"]) or "10:00"
    s["window_end"] = _hhmm(s["window_end"]) or "23:00"
    if s["window_end"] <= s["window_start"]:
        s["window_end"] = "23:59"
    s["times"] = sorted({t for t in (_hhmm(x) for x in (s["times"] or [])) if t})
    s["queue_cap"] = _int(s["queue_cap"], 1, 100, 10)
    yt = s["youtube"]
    yt["privacy"] = yt["privacy"] if yt.get("privacy") in YT_PRIVACY else "public"
    yt["category"] = str(yt.get("category") or "20") if str(yt.get("category") or "20").isdigit() else "20"
    yt["made_for_kids"], yt["notify"] = bool(yt.get("made_for_kids")), yt.get("notify") is not False
    s["instagram"]["share_to_feed"] = s["instagram"].get("share_to_feed") is not False
    s["instagram"]["first_comment"] = str(s["instagram"].get("first_comment") or "")[:2200]
    s["hashtags"] = " ".join(_tags(str(s["hashtags"] or "")))
    s["ai_label"], s["delete_after"] = bool(s["ai_label"]), s["delete_after"] is not False
    return s


def save_settings(body: dict) -> dict:
    cur = config.get("buffer") or {}
    new = dict(cur)
    for k, v in (body or {}).items():
        if k not in DEFAULTS or k in ("organization_name", "channel_names"):
            continue
        if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
            new[k] = {**(cur.get(k) or {}), **v}
        else:
            new[k] = v
    config.update(buffer=new)
    return settings()


def _tags(text: str) -> list[str]:
    out: list[str] = []
    for w in re.findall(r"#?[\w\-]+", text, flags=re.UNICODE):
        t = "#" + w.lstrip("#")
        if len(t) > 1 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:20]


# ===================================================================== state (outbox)


def _state_path() -> Path:
    return config.config_dir() / STATE_FILE


def load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"items": list(d.get("items") or []), "media": dict(d.get("media") or {}),
            "log": list(d.get("log") or []), "paused": d.get("paused") or None,
            "requests": [t for t in (d.get("requests") or []) if t > time.time() - 86400]}


def _save_state(st: dict) -> None:
    st["log"] = st["log"][-LOG_MAX:]
    keep = [i for i in st["items"] if i["state"] not in FINAL]
    done = sorted((i for i in st["items"] if i["state"] in FINAL), key=lambda i: i.get("updated_at") or 0)[-500:]
    st["items"] = keep + done
    write_json_atomic(_state_path(), st)


def _log(st: dict, kind: str, message: str, platform: str = "") -> None:
    st["log"].append({"at": time.time(), "kind": kind, "platform": platform, "message": message[:400]})


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ===================================================================== HTTP: Buffer + Cloudinary


def _http():  # the `requests` module (tests swap in a fake)
    import requests

    return requests


def _budget(st: dict | None) -> None:
    if st is None:
        return
    now = time.time()
    st["requests"] = [t for t in st["requests"] if t > now - 86400] + [now]
    if len(st["requests"]) > DAILY_BUDGET:
        raise BufferError("Paused: close to Buffer's daily request limit (600 a day on the free plan).",
                          "It goes on by itself within 24 hours.", code="BUDGET")


def gql(query: str, variables: dict | None = None, st: dict | None = None, key: str | None = None) -> dict:
    key = key or _keys().get("buffer")
    if not key:
        raise BufferError("No Buffer API key yet.", "Gaming page > Setup > Buffer: paste the API key.", code="NOKEY")
    _budget(st)
    try:
        r = _http().post(API, json={"query": query, "variables": variables or {}}, timeout=60,
                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                  "User-Agent": "SharkyClips-local"})
    except Exception as exc:  # noqa: BLE001 - network
        raise BufferError(f"Buffer can't be reached: {exc.__class__.__name__}.", "Check the internet connection.",
                          code="NETWORK") from exc
    if r.status_code == 401:
        raise BufferError("Buffer refused the API key.", "Create a new key: publish.buffer.com > Settings > API.",
                          code="UNAUTHORIZED")
    if r.status_code == 429:
        wait = r.headers.get("Retry-After") or "?"
        raise BufferError(f"Buffer's rate limit was hit (wait {wait} s).", "It goes on by itself.",
                          code="RATE_LIMIT_EXCEEDED")
    try:
        d = r.json()
    except ValueError as exc:
        raise BufferError(f"Buffer answered with HTTP {r.status_code}.", code="UNEXPECTED") from exc
    errs = d.get("errors") or []
    if errs and not d.get("data"):
        e = errs[0]
        code = str((e.get("extensions") or {}).get("code") or "UNEXPECTED")
        hint = {"UNAUTHORIZED": "Create a new key: publish.buffer.com > Settings > API.",
                "FORBIDDEN": "The key has no access to that organization / channel.",
                "RATE_LIMIT_EXCEEDED": "It goes on by itself."}.get(code)
        raise BufferError(f"Buffer: {e.get('message') or code}", hint, code=code)
    return d.get("data") or {}


Q_ACCOUNT = "query { account { id name timezone organizations { id name channelCount limits { scheduledPosts } } } }"
Q_CHANNELS = """query($org: OrganizationId!) { channels(input: {organizationId: $org}) {
  id name displayName service avatar isDisconnected isLocked isQueuePaused timezone
  postingSchedule { day times paused } } }"""
M_CREATE = """mutation($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id dueAt status } }
  ... on MutationError { message } } }"""
M_DELETE = """mutation($id: PostId!) { deletePost(input: {id: $id}) {
  ... on DeletePostSuccess { id }
  ... on MutationError { message } } }"""
Q_POST = """query($id: PostId!) { post(input: {id: $id}) { id status dueAt sentAt externalLink
  error { message } } }"""


def _cloud() -> dict:
    c = _keys().get("cloudinary") or {}
    if not (c.get("cloud_name") and c.get("api_key") and c.get("api_secret")):
        raise BufferError("No Cloudinary account yet (Buffer needs a public link to each video).",
                          "Gaming page > Setup > Video hosting: paste the cloud name, API key and secret.",
                          code="NOCLOUD")
    return c


def _sign(params: dict, secret: str) -> str:
    base = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hashlib.sha1((base + secret).encode("utf-8")).hexdigest()


def cloud_upload(path: Path, public_id: str) -> dict:
    c = _cloud()
    params = {"folder": "sharkyclips", "public_id": public_id, "timestamp": str(int(time.time()))}
    data = {**params, "api_key": c["api_key"], "signature": _sign(params, c["api_secret"])}
    url = f"{CLOUDINARY}/{c['cloud_name']}/video/upload"
    try:
        with open(path, "rb") as fh:
            r = _http().post(url, data=data, files={"file": (path.name, fh, "video/mp4")}, timeout=900)
    except OSError as exc:
        raise BufferError(f"Upload to Cloudinary failed: {exc.__class__.__name__}.", "Check the connection.",
                          code="NETWORK") from exc
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200 or not d.get("secure_url"):
        msg = ((d.get("error") or {}).get("message") if isinstance(d.get("error"), dict) else None) or f"HTTP {r.status_code}"
        raise BufferError(f"Cloudinary refused the upload: {msg}",
                          "Check the cloud name / API key / secret (Setup > Test).", code="CLOUD")
    return {"url": d["secure_url"], "public_id": d.get("public_id") or f"sharkyclips/{public_id}",
            "bytes": d.get("bytes")}


def cloud_delete(public_id: str) -> bool:
    c = _cloud()
    params = {"public_id": public_id, "timestamp": str(int(time.time()))}
    data = {**params, "api_key": c["api_key"], "signature": _sign(params, c["api_secret"])}
    try:
        r = _http().post(f"{CLOUDINARY}/{c['cloud_name']}/video/destroy", data=data, timeout=60)
        return r.status_code == 200 and (r.json() or {}).get("result") in ("ok", "not found")
    except Exception:  # noqa: BLE001
        return False


def cloud_test() -> dict:
    c = _cloud()
    try:
        r = _http().get(f"{CLOUDINARY}/{c['cloud_name']}/usage", auth=(c["api_key"], c["api_secret"]), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Cloudinary can't be reached: {exc.__class__.__name__}."}
    if r.status_code != 200:
        return {"ok": False, "error": f"Cloudinary refused the keys (HTTP {r.status_code}).",
                "hint": "Cloudinary console > Settings > API Keys: copy the key and the secret again."}
    d = r.json() or {}
    cred = d.get("credits") or {}
    return {"ok": True, "plan": d.get("plan"), "credits_used": cred.get("usage"), "credits_limit": cred.get("limit")}


# ===================================================================== test + channels


def test() -> dict:
    """Checks both keys, reads the Buffer organization + channels and links one channel per platform
    (the first connected one of that service, unless one was chosen). Saves the result."""
    out: dict = {"buffer": None, "cloudinary": None}
    try:
        acc = gql(Q_ACCOUNT).get("account") or {}
        orgs = acc.get("organizations") or []
        if not orgs:
            raise BufferError("This Buffer account has no organization.", code="NOORG")
        s = settings()
        org = next((o for o in orgs if o["id"] == s["organization_id"]), orgs[0])
        chans = gql(Q_CHANNELS, {"org": org["id"]}).get("channels") or []
        mapping = dict(s["channels"])
        names = {}
        for p in PLATFORMS:
            mine = [c for c in chans if c.get("service") == p]
            if mapping.get(p) not in [c["id"] for c in mine]:
                ok = [c for c in mine if not c.get("isDisconnected") and not c.get("isLocked")]
                mapping[p] = (ok or mine or [{"id": ""}])[0]["id"]
            ch = next((c for c in mine if c["id"] == mapping[p]), None)
            names[p] = (ch.get("displayName") or ch.get("name")) if ch else ""
        raw = dict(config.get("buffer") or {})
        raw.update(organization_id=org["id"], organization_name=org.get("name") or "", channels=mapping,
                   channel_names=names)
        config.update(buffer=raw)
        with _lock:
            st = load_state()
            st["paused"] = None
            st["channels"] = chans
            _save_state(st)
        _CHAN_CACHE.update(at=time.time(), channels=chans)
        out["buffer"] = {"ok": True, "organization": org.get("name"), "scheduled_limit": (org.get("limits") or {}).get("scheduledPosts"),
                         "channels": [_chan_view(c) for c in chans], "mapping": mapping}
    except SharkyClipsError as exc:
        out["buffer"] = {"ok": False, "error": str(exc), "hint": getattr(exc, "hint", None)}
    try:
        out["cloudinary"] = cloud_test()
    except SharkyClipsError as exc:
        out["cloudinary"] = {"ok": False, "error": str(exc), "hint": getattr(exc, "hint", None)}
    out["ok"] = bool(out["buffer"] and out["buffer"].get("ok") and out["cloudinary"] and out["cloudinary"].get("ok"))
    return out


_CHAN_CACHE: dict = {"at": 0.0, "channels": []}


def _chan_view(c: dict) -> dict:
    return {"id": c.get("id"), "service": c.get("service"), "name": c.get("displayName") or c.get("name"),
            "avatar": c.get("avatar"), "disconnected": bool(c.get("isDisconnected")), "locked": bool(c.get("isLocked")),
            "queue_paused": bool(c.get("isQueuePaused")), "timezone": c.get("timezone")}


# ===================================================================== captions


def _clean(text: str) -> str:
    return re.sub(r"[<>]", "", text or "").strip()


def default_texts(project, cand: dict) -> dict:
    """Title + caption for one clip: the AI's viral headline, then the hashtags (+ the streamer's tag)."""
    s = settings()
    story = cand.get("story") or {}
    pub = cand.get("publish") or {}
    head = _clean(story.get("headline") or (pub.get("titles") or [""])[0] or story.get("title") or "")
    meta = project.load_meta()
    up = str(((meta.get("source") or {}).get("uploader") or "")).strip()
    tags = _tags(s["hashtags"])
    if up:
        ut = "#" + re.sub(r"[^\w]", "", up, flags=re.UNICODE).lower()
        if len(ut) > 2 and ut.lower() not in (t.lower() for t in tags):
            tags.insert(0, ut)
    caption = head + ("\n\n" + " ".join(tags) if tags else "")
    yt_title = head if len(head) <= 100 else head[:99].rstrip() + "…"
    return {"title": yt_title, "caption": caption}


# ===================================================================== slots


def _local(dt: datetime) -> datetime:
    return dt.astimezone()


def _day_times(day, n: int, s: dict) -> list[datetime]:
    tz = datetime.now().astimezone().tzinfo
    if s["times"]:
        hh = s["times"][:n]
        return [datetime.combine(day, datetime.strptime(t, "%H:%M").time(), tz) for t in hh]
    a = datetime.combine(day, datetime.strptime(s["window_start"], "%H:%M").time(), tz)
    b = datetime.combine(day, datetime.strptime(s["window_end"], "%H:%M").time(), tz)
    if n == 1:
        return [a + (b - a) / 2]
    step = (b - a) / (n - 1)
    return [(a + step * i).replace(second=0, microsecond=0) for i in range(n)]


def next_slots(st: dict, platform: str, count: int, now: datetime | None = None, s: dict | None = None) -> list[datetime]:
    """The next `count` free time slots of a platform (local schedule, not before now + LEAD_MIN)."""
    s = s or settings()
    now = now or datetime.now(timezone.utc)
    taken = sorted(d for d in (_parse(i.get("due_at")) for i in st["items"]
                               if i["platform"] == platform and i["state"] in ("waiting", "scheduled", "sent")) if d)
    earliest = now + timedelta(minutes=LEAD_MIN)
    out: list[datetime] = []
    day = _local(now).date()
    for _ in range(400):
        for t in _day_times(day, s["per_day"][platform], s):
            if t < earliest:
                continue
            if any(abs((t - x).total_seconds()) < 60 for x in taken + out):
                continue
            out.append(t)
            if len(out) >= count:
                return out
        day += timedelta(days=1)
    return out


# ===================================================================== picker + queue


def _app():
    from .web import app as A

    return A


def _clip_file(project, cid: str) -> Path | None:
    from .web import state

    try:
        clips = state.load_clips(project)
    except Exception:  # noqa: BLE001
        return None
    clip = clips.get(cid) or {}
    p = project.dir / "clips" / str(clip.get("file") or "")
    return p if clip.get("file") and p.is_file() else None


def picker() -> list[dict]:
    """Every rendered clip (newest project first) with its texts and where it is queued / posted."""
    from .web import state

    st = load_state()
    on: dict[tuple, dict] = {}
    for i in st["items"]:
        if i["state"] != "cancelled":
            on.setdefault((i["project_id"], i["cid"]), {})[i["platform"]] = i["state"]
    out = []
    for project in _app().list_projects():
        if getattr(project, "posts_only", False):
            continue
        try:
            clips = state.load_clips(project) if (project.dir / "clips.json").is_file() else {}
            hl = state.load_highlights(project) or {}
            meta = project.load_meta()
        except Exception:  # noqa: BLE001 - a broken project never stops the others
            continue
        by_id = {c["id"]: c for c in hl.get("candidates") or []}
        for cid, clip in clips.items():
            f = project.dir / "clips" / str(clip.get("file") or "")
            c = by_id.get(cid)
            if not clip.get("file") or not f.is_file() or c is None:
                continue
            story = c.get("story") or {}
            out.append({"project_id": project.id, "project_title": meta.get("title") or project.id,
                        "style": meta.get("clip_style") or "streamer", "cid": cid, "rank": clip.get("rank"),
                        "duration": round(float(clip.get("duration") or 0), 1), "score": story.get("score"),
                        "type": story.get("type"), "size_mb": round(f.stat().st_size / 1e6, 1),
                        "rendered_at": clip.get("rendered_at") or meta.get("updated_at") or "",
                        "created_at": meta.get("created_at") or "", **default_texts(project, c),
                        "queued": on.get((project.id, cid), {})})
    out.sort(key=lambda x: int(x["rank"] or 99))
    out.sort(key=lambda x: str(x["created_at"]), reverse=True)  # newest video first, its clips by rank
    return out


def enqueue(picks: list[dict]) -> dict:
    """picks: [{"project_id", "cid", "platforms": [...], "title"?, "caption"?}] -> outbox items with slots."""
    s = settings()
    A = _app()
    added = []
    with _lock:
        st = load_state()
        for pk in picks:
            project = A.get_project(str(pk.get("project_id") or ""))
            cid = str(pk.get("cid") or "")
            f = _clip_file(project, cid)
            if f is None:
                raise SharkyClipsError(f"Clip {cid} isn't rendered (or was deleted).")
            from .web import state

            cand = next((c for c in (state.load_highlights(project) or {}).get("candidates") or [] if c["id"] == cid), {})
            texts = default_texts(project, cand)
            title = _clean(str(pk.get("title") or texts["title"]))[:100]
            caption = str(pk.get("caption") or texts["caption"]).strip()
            plats = [p for p in (pk.get("platforms") or PLATFORMS) if p in PLATFORMS and s["platforms"].get(p)]
            for p in plats:
                if any(i["project_id"] == project.id and i["cid"] == cid and i["platform"] == p
                       and i["state"] not in ("cancelled", "error") for i in st["items"]):
                    continue  # never twice on one platform
                slot = next_slots(st, p, 1, s=s)
                if not slot:
                    continue
                item = {"id": uuid.uuid4().hex[:10], "project_id": project.id, "cid": cid, "platform": p,
                        "file": str(f), "title": title, "caption": caption[: CAPTION_MAX[p]],
                        "peak_ms": _peak_ms(project, cid, cand), "due_at": _iso(slot[0]), "state": "waiting",
                        "post_id": None, "error": None, "created_at": time.time(), "updated_at": time.time()}
                st["items"].append(item)
                added.append(item)
        if added:
            _log(st, "queued", f"{len(added)} post(s) planned")
        _save_state(st)
    return {"added": len(added), "items": added}


def _peak_ms(project, cid: str, cand: dict) -> int | None:
    from .web import state

    try:
        clip = state.load_clips(project).get(cid) or {}
        off = float(cand.get("peak") or 0) - float(clip.get("start") or 0)
        dur = float(clip.get("duration") or 0)
        return int(max(0.5, min(off, dur - 0.5)) * 1000) if dur > 1 and off > 0 else None
    except Exception:  # noqa: BLE001
        return None


def cancel(item_id: str) -> dict:
    with _lock:
        st = load_state()
        it = next((i for i in st["items"] if i["id"] == item_id), None)
        if it is None:
            raise SharkyClipsError("No such post.")
        if it["state"] == "scheduled" and it.get("post_id"):
            d = gql(M_DELETE, {"id": it["post_id"]}, st).get("deletePost") or {}
            if d.get("message") and not d.get("id"):
                raise SharkyClipsError(f"Buffer: {d['message']}")
        if it["state"] not in ("sent",):
            it.update(state="cancelled", updated_at=time.time())
            _log(st, "cancelled", f"{it['cid']} cancelled", it["platform"])
        _save_state(st)
    return view()


def retry(item_id: str) -> dict:
    with _lock:
        st = load_state()
        it = next((i for i in st["items"] if i["id"] == item_id), None)
        if it is None or it["state"] not in ("error", "cancelled"):
            raise SharkyClipsError("Only a failed or cancelled post can be tried again.")
        it["state"] = "planning"  # out of the slot list while the new slot is found
        slot = next_slots(st, it["platform"], 1)
        it.update(state="waiting", due_at=_iso(slot[0]), error=None, post_id=None, updated_at=time.time())
        _save_state(st)
    return view()


def clear_done() -> dict:
    with _lock:
        st = load_state()
        st["items"] = [i for i in st["items"] if i["state"] not in FINAL]
        _save_state(st)
    return view()


# ===================================================================== the tick


def _input(it: dict, url: str, s: dict, channel: str) -> dict:
    p = it["platform"]
    video: dict = {"url": url}
    if it.get("peak_ms") and p in ("instagram", "tiktok"):
        video["metadata"] = {"thumbnailOffset": int(it["peak_ms"])}
    text = it["caption"]
    meta: dict = {}
    if p == "youtube":
        text = _clean(text)
        if "#shorts" not in text.lower():
            text = (text + " #Shorts").strip()
        yt = s["youtube"]
        meta["youtube"] = {"title": _clean(it["title"])[:100] or "Gaming clip", "categoryId": yt["category"],
                           "privacy": yt["privacy"], "madeForKids": yt["made_for_kids"],
                           "notifySubscribers": yt["notify"], "isAiGenerated": s["ai_label"]}
    elif p == "instagram":
        ig = {"type": "reel", "shouldShareToFeed": s["instagram"]["share_to_feed"], "isAiGenerated": s["ai_label"]}
        if s["instagram"]["first_comment"].strip():
            ig["firstComment"] = s["instagram"]["first_comment"].strip()
        meta["instagram"] = ig
    elif p == "tiktok":
        meta["tiktok"] = {"isAiGenerated": s["ai_label"]}
    return {"channelId": channel, "text": text[: CAPTION_MAX[p]], "schedulingType": "automatic",
            "mode": "customScheduled", "dueAt": it["due_at"], "assets": [{"video": video}], "metadata": meta,
            "source": "sharkyclips"}


def _media(st: dict, it: dict) -> dict:
    """The clip's public link (uploaded once, shared by the platforms)."""
    f = Path(it["file"])
    try:
        stt = f.stat()
    except OSError as exc:
        raise BufferError("The clip file is gone (deleted or moved).", code="NOFILE") from exc
    key = hashlib.sha1(f"{f}|{stt.st_size}|{int(stt.st_mtime)}".encode()).hexdigest()[:16]
    m = st["media"].get(key)
    if m and m.get("url") and not m.get("deleted"):
        return {**m, "key": key}
    src = f
    if stt.st_size > MAX_UPLOAD_BYTES:
        src = _smaller(f)
    pid = re.sub(r"[^a-z0-9_-]+", "-", f"{it['project_id'][:40]}-{it['cid']}-{key[:6]}".lower())
    up = cloud_upload(src, pid)
    st["media"][key] = {**up, "file": str(f), "uploaded_at": time.time()}
    _log(st, "uploaded", f"{f.name} -> Cloudinary ({round((up.get('bytes') or 0) / 1e6, 1)} MB)")
    return {**st["media"][key], "key": key}


def _smaller(f: Path) -> Path:
    """A copy under the 100 MB Cloudinary limit (H.264, bitrate from the length)."""
    import subprocess

    from .errors import require_tool
    from .procs import no_window
    from .audio import probe

    dest = f.with_name(f.stem + "_upload.mp4")
    if dest.is_file() and dest.stat().st_size <= MAX_UPLOAD_BYTES and dest.stat().st_mtime >= f.stat().st_mtime:
        return dest
    dur = max(1.0, float(probe(f).get("duration") or 60))
    kbps = int((MAX_UPLOAD_BYTES * 8 * 0.9 / dur) / 1000) - 160
    cmd = [require_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-i", str(f), "-c:v", "libx264",
           "-preset", "veryfast", "-b:v", f"{max(kbps, 1500)}k", "-maxrate", f"{max(kbps, 1500)}k",
           "-bufsize", f"{2 * max(kbps, 1500)}k", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dest)]
    subprocess.run(cmd, capture_output=True, timeout=1800, check=False, **no_window())
    if not dest.is_file():
        raise BufferError("The clip is over 100 MB and couldn't be made smaller.", code="TOOBIG")
    return dest


def tick(now: datetime | None = None, force_sync: bool = False) -> list[str]:
    """Push waiting posts into free Buffer slots, check posts whose time has passed, clean up media."""
    now = now or datetime.now(timezone.utc)
    acts: list[str] = []
    if not _keys().get("buffer"):
        return acts
    s = settings()
    if not s["enabled"]:
        return acts
    with _lock:
        st = load_state()
        if st["paused"] and st["paused"].get("until", 0) > time.time():
            return acts
        st["paused"] = None
        try:
            acts += _sync(st, now, force_sync)
            acts += _push(st, now, s)
            acts += _cleanup(st, s)
        except BufferError as exc:
            if exc.code in ("UNAUTHORIZED", "FORBIDDEN", "NOKEY", "NOCLOUD"):
                st["paused"] = {"reason": str(exc), "hint": exc.hint, "until": time.time() + 7 * 86400}
            elif exc.code in ("RATE_LIMIT_EXCEEDED", "BUDGET", "NETWORK"):
                st["paused"] = {"reason": str(exc), "hint": exc.hint, "until": time.time() + 15 * 60}
            _log(st, "paused", str(exc))
        finally:
            _save_state(st)
    return acts


def resume() -> dict:
    with _lock:
        st = load_state()
        st["paused"] = None
        _save_state(st)
    tick(force_sync=True)
    return view()


def _sync(st: dict, now: datetime, force: bool) -> list[str]:
    acts = []
    for it in st["items"]:
        if it["state"] != "scheduled" or not it.get("post_id"):
            continue
        due = _parse(it["due_at"])
        if not force and (due is None or now < due + timedelta(minutes=3)):
            continue
        if not force and time.time() - float(it.get("checked_at") or 0) < SYNC_EVERY_S:
            continue
        it["checked_at"] = time.time()
        post = gql(Q_POST, {"id": it["post_id"]}, st).get("post") or {}
        status = post.get("status")
        if status == "sent":
            it.update(state="sent", sent_at=post.get("sentAt"), link=post.get("externalLink"), updated_at=time.time())
            _log(st, "sent", f"{it['cid']} published", it["platform"])
            acts.append(f"buffer: {it['platform']} {it['cid']} sent")
        elif status == "error":
            msg = ((post.get("error") or {}).get("message") or "Buffer couldn't publish it")
            it.update(state="error", error=msg, updated_at=time.time())
            _log(st, "error", f"{it['cid']}: {msg}", it["platform"])
        elif status in ("draft", "needs_approval"):
            it.update(state="error", error=f"It is a {status} in Buffer (approve / schedule it there).",
                      updated_at=time.time())
    return acts


def _push(st: dict, now: datetime, s: dict) -> list[str]:
    acts = []
    pushed = 0
    for p in PLATFORMS:
        chan = s["channels"].get(p)
        if not s["platforms"].get(p) or not chan:
            continue
        in_buffer = sum(1 for i in st["items"] if i["platform"] == p and i["state"] == "scheduled")
        waiting = sorted((i for i in st["items"] if i["platform"] == p and i["state"] == "waiting"),
                         key=lambda i: i["due_at"])
        for it in waiting:
            if in_buffer >= s["queue_cap"] or pushed >= MAX_PUSH_PER_TICK:
                break
            due = _parse(it["due_at"])
            if due is None or due < now + timedelta(minutes=3):  # missed its slot (PC was off): a new one
                it["state"] = "planning"
                it["due_at"] = _iso(next_slots(st, p, 1, now, s)[0])
                it["state"] = "waiting"
                _log(st, "moved", f"{it['cid']} moved to {it['due_at']}", p)
            try:
                m = _media(st, it)
            except BufferError as exc:
                if exc.code in ("NOFILE", "TOOBIG"):
                    it.update(state="error", error=str(exc), updated_at=time.time())
                    continue
                raise
            d = gql(M_CREATE, {"input": _input(it, m["url"], s, chan)}, st).get("createPost") or {}
            pushed += 1
            post = d.get("post")
            if post and post.get("id"):
                it.update(state="scheduled", post_id=post["id"], media=m["key"], updated_at=time.time())
                in_buffer += 1
                _log(st, "scheduled", f"{it['cid']} in Buffer for {it['due_at']}", p)
                acts.append(f"buffer: {p} {it['cid']} scheduled")
                continue
            msg = d.get("message") or "Buffer didn't accept the post"
            if re.search(r"limit", msg, re.I):
                _log(st, "full", f"Buffer queue full: {msg}", p)
                break  # the channel's queue is full: try again on a later tick
            it.update(state="error", error=msg, updated_at=time.time())
            _log(st, "error", f"{it['cid']}: {msg}", p)
    return acts


def _cleanup(st: dict, s: dict) -> list[str]:
    if not s["delete_after"]:
        return []
    acts = []
    for key, m in st["media"].items():
        if m.get("deleted"):
            continue
        users = [i for i in st["items"] if i.get("media") == key or (i.get("file") == m.get("file") and i["state"] not in FINAL)]
        if not users or any(i["state"] not in FINAL for i in users):
            continue
        last = max((_parse(i.get("sent_at")) or datetime.fromtimestamp(i.get("updated_at") or 0, timezone.utc)) for i in users)
        if datetime.now(timezone.utc) - last < timedelta(minutes=30):
            continue
        if cloud_delete(m["public_id"]):
            m["deleted"] = time.time()
            acts.append(f"buffer: media {key} deleted")
    return acts


# ===================================================================== view


def view() -> dict:
    s = settings()
    st = load_state()
    now = datetime.now(timezone.utc)
    items = sorted(st["items"], key=lambda i: i.get("due_at") or "")
    per = {}
    for p in PLATFORMS:
        mine = [i for i in items if i["platform"] == p]
        nxt = next_slots(st, p, 3, now, s) if s["platforms"][p] else []
        last = max((i["due_at"] for i in mine if i["state"] in ("waiting", "scheduled")), default=None)
        per[p] = {"label": LABELS[p], "enabled": s["platforms"][p], "per_day": s["per_day"][p],
                  "channel": s["channels"].get(p) or "", "channel_name": s["channel_names"].get(p) or "",
                  "waiting": sum(1 for i in mine if i["state"] == "waiting"),
                  "scheduled": sum(1 for i in mine if i["state"] == "scheduled"),
                  "sent": sum(1 for i in mine if i["state"] == "sent"),
                  "errors": sum(1 for i in mine if i["state"] == "error"),
                  "next_free": [_iso(x) for x in nxt], "planned_until": last}
    return {"settings": s, "keys": keys_view(), "platforms": per, "paused": st["paused"],
            "requests_24h": len(st["requests"]), "budget": DAILY_BUDGET,
            "items": [{k: v for k, v in i.items() if k not in ("file",)} for i in items][-400:],
            "log": st["log"][-60:][::-1], "youtube_categories": YT_CATEGORIES}
