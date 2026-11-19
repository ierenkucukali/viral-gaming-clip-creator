"""Buffer publishing (local fork): you pick clips when this app is open and they go to Buffer right
away; Buffer then posts them to Instagram Reels, TikTok and YouTube Shorts at their times, also while
this PC is off. Nothing here waits for the PC: no background sending.

  * Keys: the Buffer API key (publish.buffer.com > Settings > API) and a Cloudinary account (cloud
    name + API key + API secret), stored encrypted in <settings>/publish_keys.sec.
  * Buffer's API has no file upload: each video must be at a public, stable https link until it is
    published. So every clip is uploaded once to the user's own Cloudinary (folder "sharkyclips")
    when it is sent, and deleted there after it was posted (checked the next time the app is open).
  * Room: Buffer's free plan holds 10 scheduled posts per channel. "Pick & send" reads how many are
    in Buffer now (one request) and sends at most that many more.
  * Times: every post gets its own time from the posting plan (`per_day` a day per platform: spread
    between window_start and window_end, every `gap_min` minutes from `start_time`, or fixed `times`),
    after the posts already in Buffer, or from a date the user picks. Created with mode
    customScheduled + dueAt (the API can't change Buffer's own posting slots).
  * While the app is open, the publish loop checks posts whose time has passed (posted / failed) and
    deletes their hosted copies. Closed: nothing happens, nothing fails.

Docs (2026-11-19): developers.buffer.com (GraphQL at https://api.buffer.com, Bearer key). The queries
and post inputs are checked against Buffer's schema in tests/data/buffer_schema.graphql.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, secretstore
from .errors import SharkyClipsError
from .project import write_json_atomic

# tests / the local end-to-end check point these at a fake Buffer + Cloudinary on 127.0.0.1
API = os.environ.get("SHARKYCLIPS_BUFFER_API") or "https://api.buffer.com"
CLOUDINARY = os.environ.get("SHARKYCLIPS_CLOUDINARY_API") or "https://api.cloudinary.com/v1_1"
KEY_FILE = "publish_keys.sec"
STATE_FILE = "buffer_outbox.json"
PLATFORMS = ("instagram", "tiktok", "youtube")
LABELS = {"instagram": "Instagram Reels", "tiktok": "TikTok", "youtube": "YouTube Shorts"}
YT_PRIVACY = ("public", "unlisted", "private")
YT_CATEGORIES = {"20": "Gaming", "24": "Entertainment", "23": "Comedy", "22": "People & Blogs", "17": "Sports"}
LEAD_MIN = 10  # a new slot is at least this far ahead
SYNC_EVERY_S = 15 * 60
DAILY_BUDGET = 500  # Buffer free plan: 600 requests / 24 h (shared with Buffer's MCP); keep a margin
SEND_MAX = 200  # clips in one "Send" (the room in Buffer limits it anyway)
ROOM_CACHE_S = 60
MAX_UPLOAD_BYTES = 99 * 1024 * 1024  # Cloudinary free plan: 100 MB per video
CAPTION_MAX = {"instagram": 2196, "tiktok": 2200, "youtube": 5000}
LOG_MAX = 300
FINAL = ("sent", "cancelled", "error")
TR_ONLY_TAGS = {"oyun", "twitchtr", "kicktr", "komikanlar", "oyunculuk", "oyunvideolari", "türkiye", "turkiye"}
MODES = ("spread", "interval", "times")

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
    # Buffer's own posts-a-day limit per channel (read by Test: free plan YouTube 2, TikTok 25, Instagram 50)
    "daily_limits": {p: 0 for p in PLATFORMS},
    "platforms": {p: True for p in PLATFORMS},
    "per_day": {p: 10 for p in PLATFORMS},
    # when: "spread" = per_day evenly between window_start and window_end; "interval" = from
    # start_time every gap_min minutes (per_day a day, the rest the next day); "times" = fixed times
    "mode": "spread",
    "window_start": "10:00",
    "window_end": "23:00",
    "start_time": "10:00",
    "gap_min": 60,
    "times": [],
    # scheduled posts Buffer holds per channel (free plan: 10)
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
    s["daily_limits"] = {p: _int(s["daily_limits"].get(p), 0, 1000, 0) for p in PLATFORMS}
    s["per_day_wanted"] = {p: _int(s["per_day"].get(p), 1, 50, 10) for p in PLATFORMS}
    # never more a day than Buffer itself publishes on that channel (more would fail at posting time)
    s["per_day"] = {p: min(s["per_day_wanted"][p], s["daily_limits"][p] or 1000) for p in PLATFORMS}
    s["platforms"] = {p: bool(s["platforms"].get(p)) for p in PLATFORMS}
    s["window_start"] = _hhmm(s["window_start"]) or "10:00"
    s["window_end"] = _hhmm(s["window_end"]) or "23:00"
    if s["window_end"] <= s["window_start"]:
        s["window_end"] = "23:59"
    s["times"] = sorted({t for t in (_hhmm(x) for x in (s["times"] or [])) if t})
    s["mode"] = s["mode"] if s["mode"] in MODES else "spread"
    if s["mode"] == "times" and not s["times"]:
        s["mode"] = "spread"
    s["start_time"] = _hhmm(s["start_time"]) or "10:00"
    s["gap_min"] = _int(s["gap_min"], 5, 720, 60)
    s["queue_cap"] = _int(s["queue_cap"], 1, SEND_MAX, 10)
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
        if k not in DEFAULTS or k in ("organization_name", "channel_names", "daily_limits"):
            continue
        if isinstance(DEFAULTS[k], dict) and isinstance(v, dict):
            new[k] = {**(cur.get(k) or {}), **v}
        else:
            new[k] = v
    config.update(buffer=new)
    _ROOM.update(at=0.0)
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
M_EDIT = """mutation($input: EditPostInput!) { editPost(input: $input) {
  ... on PostActionSuccess { post { id dueAt status } }
  ... on MutationError { message } } }"""
Q_SCHEDULED = """query($org: OrganizationId!, $ch: [ChannelId!]) {
  posts(first: 100, input: {organizationId: $org, filter: {status: [scheduled], channelIds: $ch}}) {
    edges { node { id channelId dueAt } } } }"""
Q_DAILY = """query($ids: [ChannelId!]!) { dailyPostingLimits(input: {channelIds: $ids}) { channelId limit } }"""
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
        limits = {p: 0 for p in PLATFORMS}
        ids = [c for c in mapping.values() if c]
        if ids:
            try:
                by = {v: k for k, v in mapping.items()}
                for x in gql(Q_DAILY, {"ids": ids}).get("dailyPostingLimits") or []:
                    if by.get(x.get("channelId")) and x.get("limit"):
                        limits[by[x["channelId"]]] = int(x["limit"])
            except SharkyClipsError:
                pass
        raw = dict(config.get("buffer") or {})
        raw.update(organization_id=org["id"], organization_name=org.get("name") or "", channels=mapping,
                   channel_names=names, daily_limits=limits)
        plan = int((org.get("limits") or {}).get("scheduledPosts") or 0)
        if plan > 0:  # Buffer's real queue limit (free 10; paid plans much more): "Pick & send" follows it
            raw["queue_cap"] = min(plan, SEND_MAX)
        config.update(buffer=raw)
        with _lock:
            st = load_state()
            st["paused"] = None
            st["channels"] = chans
            _save_state(st)
        _CHAN_CACHE.update(at=time.time(), channels=chans)
        out["buffer"] = {"ok": True, "organization": org.get("name"), "scheduled_limit": (org.get("limits") or {}).get("scheduledPosts"),
                         "channels": [_chan_view(c) for c in chans], "mapping": mapping,
                         "daily_limits": limits}
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
    # meta "streamer": the streamer's name when the video came from someone else's channel (a re-upload)
    up = str(meta.get("streamer") or ((meta.get("source") or {}).get("uploader") or "")).strip()
    # the AI package (Gaming > Setup > "extra titles + description per clip"): its description and
    # hashtags join the caption; without it: the headline + the hashtags of the settings
    from .publishing import split_hashtags

    body, ai_tags = split_hashtags(str(pub.get("description") or ""))
    ai_tags = [str(t) for t in (pub.get("hashtags") or [])] + ai_tags
    if str(config.get("output_language") or "en") != "tr":  # the generic game bank has Turkish tags
        ai_tags = [t for t in ai_tags if t.lower().lstrip("#") not in TR_ONLY_TAGS]
    tags = _tags(" ".join(ai_tags[:8]) + " " + s["hashtags"])[:12]
    if up:
        ut = "#" + re.sub(r"[^\w]", "", up, flags=re.UNICODE).lower()
        if len(ut) > 2 and ut.lower() not in (t.lower() for t in tags):
            tags.insert(0, ut)
    body = _clean(body)
    caption = head + (f"\n\n{body}" if body and body.lower() != head.lower() else "") + ("\n\n" + " ".join(tags) if tags else "")
    yt_title = head if len(head) <= 100 else head[:99].rstrip() + "…"
    return {"title": yt_title, "caption": caption}


# ===================================================================== slots


def _local(dt: datetime) -> datetime:
    return dt.astimezone()


def _day_times(day, n: int, s: dict) -> list[datetime]:
    """The posting times of one local day (at most n)."""
    tz = datetime.now().astimezone().tzinfo

    def at(hhmm: str) -> datetime:
        return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time(), tz)

    if s["mode"] == "times":
        return [at(t) for t in s["times"][:n]]
    if s["mode"] == "interval":
        first = at(s["start_time"])
        end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)
        out = [first + timedelta(minutes=s["gap_min"] * i) for i in range(n)]
        return [t for t in out if t < end]  # never runs into the next day
    a, b = at(s["window_start"]), at(s["window_end"])
    if n == 1:
        return [a + (b - a) / 2]
    step = (b - a) / (n - 1)
    return [(a + step * i).replace(second=0, microsecond=0) for i in range(n)]


def _taken(st: dict, platform: str) -> list[datetime]:
    return sorted(d for d in (_parse(i.get("due_at")) for i in st["items"]
                              if i["platform"] == platform and i["state"] in ("scheduled", "sent")) if d)


def next_slots(st: dict, platform: str, count: int, now: datetime | None = None, s: dict | None = None,
               start: datetime | None = None) -> list[datetime]:
    """The next `count` free time slots of a platform (local schedule), not before now + LEAD_MIN
    (and not before `start`, a time the user picked)."""
    s = s or settings()
    now = now or datetime.now(timezone.utc)
    taken = _taken(st, platform)
    earliest = max(now + timedelta(minutes=LEAD_MIN), start or now)
    per_day = s["per_day"][platform]
    out: list[datetime] = []
    day = _local(earliest).date()
    for _ in range(800):
        times = _day_times(day, per_day, s)
        used = sum(1 for x in taken + out if _local(x).date() == day)  # pinned / moved posts count too
        for t in times:
            if used >= per_day:
                break
            if t < earliest or any(abs((t - x).total_seconds()) < 60 for x in taken + out):
                continue
            out.append(t)
            used += 1
            if len(out) >= count:
                return out
        day += timedelta(days=1)
    return out


def set_time(item_id: str, when: str, now: datetime | None = None) -> dict:
    """Move one post that is in Buffer to another time (Buffer is updated too)."""
    now = now or datetime.now(timezone.utc)
    t = _parse(when)
    if t is None:
        raise SharkyClipsError("That time doesn't look right.")
    if t.tzinfo is None:
        t = t.astimezone()
    if t < now + timedelta(minutes=LEAD_MIN):
        raise SharkyClipsError(f"Pick a time at least {LEAD_MIN} minutes from now.")
    with _lock:
        st = load_state()
        it = next((i for i in st["items"] if i["id"] == item_id), None)
        if it is None or it["state"] != "scheduled" or not it.get("post_id"):
            raise SharkyClipsError("Only a post that is in Buffer can be moved.")
        d = gql(M_EDIT, {"input": {"id": it["post_id"], "dueAt": _iso(t), "mode": "customScheduled"}}, st).get("editPost") or {}
        if not (d.get("post") or {}).get("id"):
            raise SharkyClipsError(f"Buffer: {d.get('message') or 'the time could not be changed'}")
        it.update(due_at=_iso(t), pinned=True, updated_at=time.time())
        _log(st, "moved", f"{it['cid']} moved by hand to {it['due_at']}", it["platform"])
        _save_state(st)
    return view()


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
            d = on.setdefault((i["project_id"], i["cid"]), {})
            if d.get(i["platform"]) not in ("scheduled", "sent"):  # in Buffer / posted wins over a failed try
                d[i["platform"]] = i["state"]
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


_ROOM: dict = {"at": 0.0, "room": {}, "in_buffer": {}, "error": None}


def room(fresh: bool = False) -> dict:
    """How many more posts Buffer takes now per platform: queue_cap minus what is scheduled there
    (also posts made in Buffer itself). One request, cached for a minute."""
    s = settings()
    if not fresh and time.time() - _ROOM["at"] < ROOM_CACHE_S and _ROOM["room"]:
        return dict(_ROOM)
    chans = {p: s["channels"].get(p) for p in PLATFORMS if s["platforms"].get(p) and s["channels"].get(p)}
    counts = {p: 0 for p in chans}
    err = None
    if chans and s["organization_id"] and _keys().get("buffer"):
        try:
            with _lock:
                st = load_state()
                d = gql(Q_SCHEDULED, {"org": s["organization_id"], "ch": list(chans.values())}, st)
                _save_state(st)
            by_chan = {v: k for k, v in chans.items()}
            for e in ((d.get("posts") or {}).get("edges") or []):
                p = by_chan.get((e.get("node") or {}).get("channelId"))
                if p:
                    counts[p] += 1
        except SharkyClipsError as exc:  # unknown: count our own posts in Buffer instead
            err = str(exc)
            st = load_state()
            counts = {p: sum(1 for i in st["items"] if i["platform"] == p and i["state"] == "scheduled") for p in chans}
    _ROOM.update(at=time.time(), in_buffer=counts, error=err,
                 room={p: max(0, s["queue_cap"] - counts.get(p, 0)) for p in chans})
    return dict(_ROOM)


_SEND: dict = {"running": False, "done": 0, "total": 0, "sent": 0, "failed": 0, "skipped": 0,
               "message": None, "finished_at": None}


def sending() -> dict:
    return dict(_SEND)


def send(picks: list[dict], start: str | None = None, wait: bool = False) -> dict:
    """picks: [{"project_id", "cid", "platforms": [...], "title"?, "caption"?}] -> straight into Buffer
    (in the background; `sending()` tells the progress). Per platform at most the room Buffer has now;
    each post gets the next time of the posting plan (or from `start`). Never twice on one platform."""
    if _SEND["running"]:
        raise SharkyClipsError("Still sending the last pick. Wait a moment.")
    s = settings()
    if not _keys().get("buffer") or not s["organization_id"]:
        raise SharkyClipsError("Set up Buffer first.", "Gaming page > Setup > Buffer: paste the key, press Save and test.")
    _cloud()
    begin = _parse(start) if start else None
    if begin is not None and begin.tzinfo is None:
        begin = begin.astimezone()
    left = dict(room(fresh=True)["room"])
    A = _app()
    from .web import state

    st = load_state()
    on = {(i["project_id"], i["cid"], i["platform"]) for i in st["items"] if i["state"] in ("scheduled", "sent")}
    todo: list[dict] = []
    skipped = 0
    for pk in picks[:SEND_MAX]:
        project = A.get_project(str(pk.get("project_id") or ""))
        cid = str(pk.get("cid") or "")
        f = _clip_file(project, cid)
        if f is None:
            raise SharkyClipsError(f"Clip {cid} isn't rendered (or was deleted).")
        cand = next((c for c in (state.load_highlights(project) or {}).get("candidates") or [] if c["id"] == cid), {})
        texts = default_texts(project, cand)
        base = {"project_id": project.id, "cid": cid, "file": str(f),
                "title": _clean(str(pk.get("title") or texts["title"]))[:100],
                "caption": str(pk.get("caption") or texts["caption"]).strip(), "peak_ms": _peak_ms(project, cid, cand)}
        for p in [p for p in (pk.get("platforms") or PLATFORMS) if p in left]:
            if (project.id, cid, p) in on:
                continue  # already in Buffer / posted there
            if left[p] <= 0:
                skipped += 1  # no room in Buffer now: next time
                continue
            left[p] -= 1
            todo.append({**base, "platform": p, "caption": base["caption"][: CAPTION_MAX[p]]})
    if not todo:
        msg = ("Buffer is full right now (the free plan holds 10 posts per channel). Send more when some are posted."
               if skipped else "These clips are already in Buffer.")
        raise SharkyClipsError(msg)
    _SEND.update(running=True, done=0, total=len(todo), sent=0, failed=0, skipped=skipped, message=None, finished_at=None)
    t = threading.Thread(target=_send_all, args=(todo, begin, s), name="sharkyclips-buffer-send", daemon=True)
    t.start()
    if wait:
        t.join()
    return {"sending": sending(), "count": len(todo), "skipped": skipped}


def _send_all(todo: list[dict], begin: datetime | None, s: dict) -> None:
    try:
        for it in todo:
            with _lock:
                st = load_state()
                try:
                    r = _send_one(st, it, begin, s)
                except BufferError as exc:
                    r = "stop" if exc.code in ("UNAUTHORIZED", "FORBIDDEN", "NOKEY", "NOCLOUD", "RATE_LIMIT_EXCEEDED",
                                               "BUDGET", "NETWORK") else "error"
                    _record(st, it, "error", error=str(exc))
                    _SEND["message"] = str(exc)
                _save_state(st)
            _SEND["done"] += 1
            _SEND["sent" if r == "ok" else "failed"] += r != "skip"
            if r == "stop":
                _SEND["failed"] += _SEND["total"] - _SEND["done"]
                break
    finally:
        _SEND.update(running=False, finished_at=time.time())
        _ROOM.update(at=0.0)


def _record(st: dict, it: dict, state_: str, **extra) -> dict:
    item = {"id": uuid.uuid4().hex[:10], "project_id": it["project_id"], "cid": it["cid"], "platform": it["platform"],
            "file": it["file"], "title": it["title"], "caption": it["caption"], "peak_ms": it.get("peak_ms"),
            "due_at": it.get("due_at"), "state": state_, "post_id": None, "error": None,
            "created_at": time.time(), "updated_at": time.time(), **extra}
    st["items"].append(item)
    return item


def _send_one(st: dict, it: dict, begin: datetime | None, s: dict) -> str:
    p = it["platform"]
    slot = next_slots(st, p, 1, None, s, begin)
    if not slot:
        raise BufferError("No free time found in the posting plan.", code="NOSLOT")
    it = {**it, "due_at": _iso(slot[0])}
    m = _media(st, it)
    d = gql(M_CREATE, {"input": _input(it, m["url"], s, s["channels"][p])}, st).get("createPost") or {}
    post = d.get("post")
    if post and post.get("id"):
        _record(st, it, "scheduled", post_id=post["id"], media=m["key"])
        _log(st, "scheduled", f"{it['cid']} in Buffer for {it['due_at']}", p)
        return "ok"
    msg = d.get("message") or "Buffer didn't accept the post"
    if re.search(r"limit", msg, re.I):
        msg = "Buffer is full for this channel (free plan: 10). Send it again when some are posted."
    _record(st, it, "error", error=msg)
    _log(st, "error", f"{it['cid']}: {msg}", p)
    return "error"


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
    """A failed / cancelled post: send it to Buffer again now (if there is room)."""
    st = load_state()
    it = next((i for i in st["items"] if i["id"] == item_id), None)
    if it is None or it["state"] not in ("error", "cancelled"):
        raise SharkyClipsError("Only a failed or cancelled post can be sent again.")
    with _lock:
        st = load_state()
        st["items"] = [i for i in st["items"] if i["id"] != item_id]
        _save_state(st)
    send([{"project_id": it["project_id"], "cid": it["cid"], "platforms": [it["platform"]],
           "title": it["title"], "caption": it["caption"]}], wait=True)
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
    """While the app is open: check posts whose time has passed (posted / failed) and delete the hosted
    copies of posted clips. Sends nothing by itself."""
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
            acts += _cleanup(st, s, now)
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
        if not force and now.timestamp() - float(it.get("checked_at") or 0) < SYNC_EVERY_S:
            continue
        it["checked_at"] = now.timestamp()
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


def _cleanup(st: dict, s: dict, now: datetime) -> list[str]:
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
        if now - last < timedelta(minutes=30):
            continue
        if cloud_delete(m["public_id"]):
            m["deleted"] = time.time()
            acts.append(f"buffer: media {key} deleted")
    return acts


# ===================================================================== view


def example_day(s: dict | None = None, platform: str = "tiktok") -> list[str]:
    """The posting times of a day with the current settings (local HH:MM)."""
    s = s or settings()
    day = (datetime.now().astimezone() + timedelta(days=1)).date()
    return [t.strftime("%H:%M") for t in _day_times(day, s["per_day"][platform], s)]


def view() -> dict:
    s = settings()
    st = load_state()
    now = datetime.now(timezone.utc)
    items = sorted(st["items"], key=lambda i: i.get("due_at") or "")
    rm = dict(_ROOM)
    per = {}
    for p in PLATFORMS:
        mine = [i for i in items if i["platform"] == p and i["state"] != "cancelled"]
        nxt = next_slots(st, p, 3, now, s) if s["platforms"][p] else []
        last = max((i["due_at"] for i in mine if i["state"] == "scheduled"), default=None)
        per[p] = {"label": LABELS[p], "enabled": s["platforms"][p], "per_day": s["per_day"][p],
                  "per_day_wanted": s["per_day_wanted"][p], "daily_limit": s["daily_limits"][p] or None,
                  "channel": s["channels"].get(p) or "", "channel_name": s["channel_names"].get(p) or "",
                  "scheduled": sum(1 for i in mine if i["state"] == "scheduled"),
                  "sent": sum(1 for i in mine if i["state"] == "sent"),
                  "errors": sum(1 for i in mine if i["state"] == "error"),
                  "room": (rm.get("room") or {}).get(p), "in_buffer": (rm.get("in_buffer") or {}).get(p),
                  "next_free": [_iso(x) for x in nxt], "planned_until": last, "day": example_day(s, p)}
    return {"settings": s, "keys": keys_view(), "platforms": per, "paused": st["paused"],
            "requests_24h": len(st["requests"]), "budget": DAILY_BUDGET, "sending": sending(),
            "room_checked_at": rm.get("at") or None, "room_error": rm.get("error"),
            "items": [{k: v for k, v in i.items() if k not in ("file",)} for i in items][-600:],
            "log": st["log"][-60:][::-1], "youtube_categories": YT_CATEGORIES}


def preview(count: int, platforms: list[str] | None = None, start: str | None = None) -> dict:
    """The times `count` clips would get now: per platform the first and the last, and over how many days."""
    s = settings()
    st = load_state()
    begin = _parse(start) if start else None
    if begin is not None and begin.tzinfo is None:
        begin = begin.astimezone()
    out = {}
    for p in platforms or PLATFORMS:
        if p not in PLATFORMS or not s["platforms"][p]:
            continue
        slots = next_slots(st, p, max(0, min(count, SEND_MAX)), None, s, begin)
        out[p] = {"first": _iso(slots[0]) if slots else None, "last": _iso(slots[-1]) if slots else None,
                  "days": len({_local(x).date() for x in slots})}
    return out
