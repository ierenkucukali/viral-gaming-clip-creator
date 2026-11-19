"""Auto-post (local fork): rendered clips go out by themselves, N a day per platform, best first.

Off by default. When it is on, every publish-loop tick (web/publishing.py, about once a minute):

  1. for each switched-on platform (youtube / tiktok / reels) and each of the next `days_ahead`
     local days, the day's slots are the first `per_day` posting times (`times`, spread like the
     YouTube plan when more are needed, at least 45 min apart);
  2. a free slot (in the future, that day still under per_day, no own post within 45 min) gets the
     best clip that is not on that platform yet: rendered 9:16 file, not "weak", score >= min_score
     (AI story score, else the viral score, else the hype score), best score first;
  3. the clip is planned through the normal one-button publish code (web/multipost.start), so the
     uploads, captions (English drafts), retries, "never twice" checks and records are the existing
     ones: YouTube gets publishAt, TikTok is posted by the app (API / TikTok Studio browser) at that
     time, Instagram is in Manual mode (upstream 1.2.50: Meta automation off) -> a reminder at that
     time with the file + caption ready, you press Post in the Instagram app.

A platform is paused (and says why) when it is not connected, signed out, needs a login / CAPTCHA
(TikTok Studio), or the plan refuses with problems; "Resume" clears it. A clip that failed to plan
is skipped on that platform for 6 hours. Hard cap: platforms.DAILY_POST_MAX a day per platform.

State: <settings>/autopost.json {"planned": [...], "paused": {platform: {...}}, "failed": {...}, "log": [...]}
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

from . import config
from .errors import SharkyClipsError
from .project import write_json_atomic

PLATFORMS = ("youtube", "tiktok", "reels")
LABELS = {"youtube": "YouTube Shorts", "tiktok": "TikTok", "reels": "Instagram Reels"}
DEFAULT_TIMES = ["12:00", "18:00", "21:00"]
GAP_MIN = 45
LEAD_MIN = 20  # a slot must be at least this far ahead
FAIL_SKIP_S = 6 * 3600
MAX_PER_TICK = 6
LOG_MAX = 200
POSTED = ("scheduled", "preparing", "uploading", "processing", "published", "reminded", "shared")

_lock = threading.RLock()


# ------------------------------------------------------------------ settings + state


def _cap() -> int:
    from .platforms import DAILY_POST_MAX

    return DAILY_POST_MAX


def settings() -> dict:
    raw = config.get("autopost") or {}
    plats = raw.get("platforms") or {}
    out = {"enabled": bool(raw.get("enabled")),
           "platforms": {p: {"enabled": bool((plats.get(p) or {}).get("enabled")),
                             "per_day": _clamp((plats.get(p) or {}).get("per_day"), 1, _cap(), 1)}
                         for p in PLATFORMS},
           "times": _times(raw.get("times")),
           "min_score": _clamp(raw.get("min_score"), 0, 100, 55),
           "days_ahead": _clamp(raw.get("days_ahead"), 1, 7, 2),
           "tiktok_consent": bool(raw.get("tiktok_consent")),
           "cap": _cap()}
    return out


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _times(raw) -> list[str]:
    from .publishing import _hhmm

    out = sorted({t for t in (_hhmm(x) for x in (raw or [])) if t})
    return out or list(DEFAULT_TIMES)


def save_settings(body: dict) -> dict:
    cur = settings()
    new = {"enabled": bool(body.get("enabled", cur["enabled"])),
           "platforms": {p: {**cur["platforms"][p], **{k: v for k, v in ((body.get("platforms") or {}).get(p) or {}).items()
                                                       if k in ("enabled", "per_day")}} for p in PLATFORMS},
           "times": body.get("times", cur["times"]), "min_score": body.get("min_score", cur["min_score"]),
           "days_ahead": body.get("days_ahead", cur["days_ahead"]),
           "tiktok_consent": bool(body.get("tiktok_consent", cur["tiktok_consent"]))}
    config.update(autopost=new)
    s = settings()
    yt = s["platforms"]["youtube"]
    if s["enabled"] and yt["enabled"]:  # the YouTube plan uses the same daily amount
        config.update(publish_daily_limit=yt["per_day"])
    return s


def _state_path():
    return config.config_dir() / "autopost.json"


def load_state() -> dict:
    try:
        d = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    return {"planned": list(d.get("planned") or []), "paused": dict(d.get("paused") or {}),
            "failed": dict(d.get("failed") or {}), "log": list(d.get("log") or [])}


def _save_state(st: dict) -> None:
    st["log"] = st["log"][-LOG_MAX:]
    write_json_atomic(_state_path(), st)


def _log(st: dict, platform: str, kind: str, message: str, **extra) -> None:
    st["log"].append({"at": time.time(), "platform": platform, "kind": kind, "message": message, **extra})


def resume(platform: str) -> dict:
    with _lock:
        st = load_state()
        st["paused"].pop(platform, None)
        st["failed"] = {k: v for k, v in st["failed"].items() if not k.startswith(platform + ":")}
        _log(st, platform, "resumed", "Resumed by you.")
        _save_state(st)
    return view()


# ------------------------------------------------------------------ connections


def connection(platform: str) -> dict:
    """{"connected", "mode": "auto" | "reminder", "problem"}. reminder = the app makes the file +
    caption and reminds you at the time; you press Post (Instagram always, TikTok without API/browser)."""
    from . import platforms as PF

    if platform == "youtube":
        from . import youtube

        try:
            ok = bool(youtube.connected())
        except Exception:  # noqa: BLE001
            ok = False
        return {"connected": ok, "mode": "auto", "problem": None if ok else "not_connected"}
    m = PF.mode(platform)
    prob = PF.problem(platform)
    if platform == "tiktok" and m == "browser":
        return {"connected": True, "mode": "auto", "problem": (prob or {}).get("kind")}
    if m == "api":
        return {"connected": True, "mode": "auto", "problem": (prob or {}).get("kind")}
    return {"connected": PF.connected(platform) or platform == "reels", "mode": "reminder",
            "problem": (prob or {}).get("kind")}


# ------------------------------------------------------------------ what can go out


def _app():
    from .web import app as A

    return A


def _score(c: dict) -> int:
    story = c.get("story") or {}
    if story.get("score") is not None:
        return int(story["score"])
    pub = c.get("publish") or {}
    if pub.get("viral_score") is not None:
        return int(pub["viral_score"])
    return int(c.get("hype_score") or 0)


def candidates(min_score: int) -> list[dict]:
    """Rendered clips, best first: [{"project_id", "cid", "score", "title", "rendered_at"}]."""
    from .web import state

    A = _app()
    out: list[dict] = []
    for project in A.list_projects():
        if getattr(project, "posts_only", False):
            continue
        try:
            clips = state.load_clips(project) if (project.dir / "clips.json").is_file() else {}
            hl = state.load_highlights(project) or {}
        except Exception:  # noqa: BLE001 - a broken project never stops the others
            continue
        by_id = {c["id"]: c for c in hl.get("candidates") or []}
        for cid, clip in clips.items():
            c = by_id.get(cid)
            if c is None or not (project.dir / "clips" / clip.get("file", "")).is_file():
                continue
            if not _same_cut(clip, c):  # a render of an older cut with the same id (highlights re-run)
                continue
            if c.get("quality") == "weak":
                continue
            sc = _score(c)
            if sc < min_score:
                continue
            title = (c.get("story") or {}).get("title") or ((c.get("publish") or {}).get("titles") or [""])[0]
            out.append({"project_id": project.id, "cid": cid, "score": sc, "title": title,
                        "rendered_at": clip.get("rendered_at") or ""})
    out.sort(key=lambda x: (-x["score"], str(x["rendered_at"])))
    return out


def _same_cut(clip: dict, cand: dict) -> bool:
    """The rendered file is (mostly) the candidate's moment: trims move edges, a re-run moves all."""
    try:
        a0, a1 = float(clip["start"]), float(clip["end"])
        b0, b1 = float(cand["start"]), float(cand["end"])
    except (KeyError, TypeError, ValueError):
        return True  # no times recorded: trust the id, as the Publish button does
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    return inter >= 0.5 * min(a1 - a0, b1 - b0)


def _on_platform(project, cid: str, platform: str) -> bool:
    """Already posted / planned / being posted there (any way)."""
    from . import platforms as PF
    from . import publishing as P

    if platform == "youtube":
        from .web import state

        it = P.get(P.key_of(project.id, cid)) or {}
        if it.get("publish_at") or it.get("video_id") or it.get("now"):
            return True
        try:
            return bool(state.live_upload(project, cid))
        except Exception:  # noqa: BLE001
            return False
    r = (PF.records(project).get(cid) or {}).get(platform) or {}
    if r.get("status") in POSTED or r.get("media_id") or r.get("publish_id") or r.get("post_id"):
        return True
    return bool(r.get("remind_at") and r.get("status") not in ("failed", "cancelled"))


# ------------------------------------------------------------------ slots


def _local_day(dt: datetime):
    return dt.astimezone().date()


def _day_slots(day, per_day: int, times: list[str]) -> list[datetime]:
    from .publishing import _spread, at_local

    return [at_local(day, t) for t in _spread(times, per_day)[:per_day]]


def _planned_on(st: dict, platform: str) -> list[datetime]:
    out = []
    for p in st["planned"]:
        if p["platform"] == platform and p.get("status") != "failed":
            t = _parse(p["when"])
            if t:
                out.append(t)
    return out


def _parse(v) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _external_count(platform: str, day) -> int:
    """Posts on that day made outside auto-post (by hand from the Publish button)."""
    from . import platforms as PF
    from . import publishing as P

    A = _app()
    n = 0
    if platform == "youtube":
        from .web import publishing as pub

        rel = pub.releases()
        return sum(1 for t in rel.values() if _local_day(t) == day)
    for project in A.list_projects():
        for plats in PF.records(project).values():
            r = (plats or {}).get(platform) or {}
            if r.get("autopost"):
                continue
            at = P.parse(r.get("publish_at") or r.get("remind_at") or r.get("posted_at"))
            if at is not None and r.get("status") in POSTED and _local_day(at) == day:
                n += 1
    return n


# ------------------------------------------------------------------ the tick


def tick(now: datetime | None = None) -> list[str]:
    s = settings()
    if not s["enabled"]:
        return []
    now = now or datetime.now(timezone.utc)
    acts: list[str] = []
    with _lock:
        st = load_state()
        cands = None
        done = 0
        for platform in PLATFORMS:
            ps = s["platforms"][platform]
            if not ps["enabled"] or platform in st["paused"]:
                continue
            con = connection(platform)
            if not con["connected"] or con["problem"] in ("signed_out", "login", "captcha", "verification"):
                reason = "Not connected." if not con["connected"] else f"Needs you: {con['problem']}."
                st["paused"][platform] = {"reason": reason, "at": time.time()}
                _log(st, platform, "paused", reason)
                acts.append(f"autopost_paused:{platform}")
                continue
            mine = _planned_on(st, platform)
            for d in range(s["days_ahead"]):
                day = _local_day(now) + timedelta(days=d)
                used = sum(1 for t in mine if _local_day(t) == day) + _external_count(platform, day)
                for slot in _day_slots(day, ps["per_day"], s["times"]):
                    if used >= ps["per_day"] or done >= MAX_PER_TICK or platform in st["paused"]:
                        break
                    if slot < now + timedelta(minutes=LEAD_MIN):
                        continue
                    if any(abs((slot - t).total_seconds()) < GAP_MIN * 60 for t in mine):
                        continue
                    if cands is None:
                        cands = candidates(s["min_score"])
                    for _try in range(3):  # a clip that fails to plan leaves the slot to the next one
                        pick = _pick(cands, platform, st, now)
                        if pick is None or platform in st["paused"]:
                            break
                        if _plan_one(st, platform, pick, slot, s, con):
                            mine.append(slot)
                            used += 1
                            done += 1
                            acts.append(f"autopost:{platform}:{pick['project_id']}:{pick['cid']}")
                            break
        _save_state(st)
    return acts


def _pick(cands: list[dict], platform: str, st: dict, now: datetime) -> dict | None:
    A = _app()
    for c in cands:
        key = f"{platform}:{c['project_id']}:{c['cid']}"
        if any(p["platform"] == platform and p["project_id"] == c["project_id"] and p["cid"] == c["cid"]
               and p.get("status") != "failed" for p in st["planned"]):
            continue
        if time.time() - float(st["failed"].get(key) or 0) < FAIL_SKIP_S:
            continue
        try:
            project = A.get_project(c["project_id"])
        except Exception:  # noqa: BLE001
            continue
        if _on_platform(project, c["cid"], platform):
            continue
        return c
    return None


def _plan_one(st: dict, platform: str, c: dict, slot: datetime, s: dict, con: dict) -> bool:
    from . import platforms as PF
    from .web import multipost as MP

    A = _app()
    key = f"{platform}:{c['project_id']}:{c['cid']}"
    try:
        project = A.get_project(c["project_id"])
        res = MP.start(project, c["cid"], {"platforms": [platform], "when": slot.isoformat(), "remember": False,
                                           "consent": s["tiktok_consent"]})
        r = (res.get("results") or {}).get(platform) or {}
    except SharkyClipsError as exc:
        probs = getattr(exc, "problems", None)
        if probs:  # account-level problems (not connected, scopes, ...): pause the platform
            reason = "; ".join(str(p.get("code") or p) for p in probs)[:200]
            st["paused"][platform] = {"reason": f"{exc.message} ({reason})", "at": time.time()}
            _log(st, platform, "paused", st["paused"][platform]["reason"])
        else:
            st["failed"][key] = time.time()
            _log(st, platform, "failed", exc.message, project_id=c["project_id"], cid=c["cid"])
        return False
    status = r.get("status")
    if status == "failed":
        st["failed"][key] = time.time()
        err = (r.get("error") or {}).get("message") or "failed"
        _log(st, platform, "failed", err, project_id=c["project_id"], cid=c["cid"])
        if "login" in err.lower() or "captcha" in err.lower() or "verification" in err.lower():
            st["paused"][platform] = {"reason": err, "at": time.time()}
        return False
    when = r.get("publish_at") or slot.isoformat()
    if platform != "youtube":
        try:
            PF.record(project, c["cid"], platform, autopost=True)
        except Exception:  # noqa: BLE001
            pass
    st["planned"].append({"platform": platform, "project_id": c["project_id"], "cid": c["cid"], "when": when,
                          "status": status or "scheduled", "mode": con["mode"], "score": c["score"],
                          "title": c["title"], "at": time.time()})
    _log(st, platform, "planned", f"{c['title'] or c['cid']} at {when}", project_id=c["project_id"], cid=c["cid"])
    return True


# ------------------------------------------------------------------ for the UI


def view() -> dict:
    s = settings()
    st = load_state()
    now = datetime.now(timezone.utc)
    upcoming = sorted((p for p in st["planned"] if (_parse(p["when"]) or now) >= now - timedelta(hours=1)),
                      key=lambda p: p["when"])
    today = _local_day(now)
    plats = {}
    for p in PLATFORMS:
        try:
            con = connection(p)
        except Exception as exc:  # noqa: BLE001
            con = {"connected": False, "mode": "reminder", "problem": str(exc)[:100]}
        plats[p] = {**s["platforms"][p], "label": LABELS[p], **con, "paused": st["paused"].get(p),
                    "today": sum(1 for x in st["planned"] if x["platform"] == p and _local_day(_parse(x["when"]) or now) == today),
                    "next": [x for x in upcoming if x["platform"] == p][:5]}
    return {"settings": s, "platforms": plats, "log": st["log"][-20:][::-1]}
