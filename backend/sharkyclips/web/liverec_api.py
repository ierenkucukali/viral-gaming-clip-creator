"""Web API of the live recorder (local gaming fork) + the bridge into ordinary projects.

A live clip ("Make clip" on a candidate, Mark moment, Clip last N s):
  1. io lane:  the WHOLE segments covering the window are concatenated (stream copy) into
               <session>/clips_src/<name>.mp4 and imported as an ordinary SharkyClips project
               (hard link: no extra disk). Its meta keeps the session id, the session time and
               the UTC wall clock of its first frame, and the channel (facecam profile memory).
               The window's pin is released once the project owns its copy.
  2. gpu lane: Whisper (word timestamps, keywords) + the normal highlight analysis on that
               small file, then the exact window is added as a manual clip and accepted. The
               GPU lane runs one job at a time, so Whisper and the local LLM never share VRAM;
               if Whisper fails (no GPU, OOM...) the clip is still made from the audio signals.
  3. facecam from the channel's remembered profile (or detection); optional render right away.
Recording never waits for any of this.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import config, liverec
from ..errors import SharkyClipsError
from ..project import get_project, read_json, write_json_atomic
from . import state
from .jobs import Job

router = APIRouter()
CLIP_LAST = (30, 60, 90, 120)


def _app():
    from . import app as appmod

    return appmod


def _m() -> liverec.Manager:
    return liverec.manager()


def install() -> None:
    """Server start: load sessions (recordings interrupted by a restart are recovered and wait
    for Resume), start the analysis thread."""
    if os.environ.get("SHARKYCLIPS_LIVE_LOOP") == "0":
        return
    _m().ensure_analysis()


# ================================================================== the bridge into projects


def _clip_name(s: liverec.Session, t0: float, t1: float, kind: str) -> str:
    return f"{s.id}_{kind}_{int(t0):06d}-{int(t1):06d}"


def submit_live_clip(sid: str, win: tuple[float, float], kind: str, render: bool = False,
                     ref: str | None = None, label: str = "") -> Job:
    """win = the clip itself (session seconds). The assembled file adds whole segments around it."""
    A = _app()
    s = _m().get(sid)
    w0, w1 = max(float(s.info.get("first_t") or 0), float(win[0])), float(win[1])
    if w1 - w0 < 3:
        raise SharkyClipsError("A clip needs at least 3 seconds.")
    if w1 > s.recorded + 0.01:
        raise SharkyClipsError("That moment isn't fully recorded yet.",
                               f"Wait about {int(w1 - s.recorded) + 1} s and try again.")
    pin = s.pin(w0, w1, "clip_job", ref=f"job:{kind}:{int(w0)}")
    name = _clip_name(s, w0, w1, kind)

    def run(job: Job):
        from ..ingest import ingest

        A.jobs.update(job, stage="assembling", progress=None)
        try:
            got = s.assemble(w0, w1, s.dir / "clips_src" / f"{name}.mp4")
            project = ingest(got["path"], mode="gaming")
        finally:
            s.unpin(pin["id"])  # the project has its own (hard-linked) copy now
        off = float(got["offset"])
        wall0 = _wall_of(s, off)
        meta = project.load_meta()
        title = f"{s.info.get('channel') or 'Live'} LIVE {_hms(w0)}" + (f" - {label}" if label else "")
        meta["title"] = title
        meta["source"] = {**(meta.get("source") or {}), "type": "live", "uploader": s.info.get("channel"),
                          "channel": s.info.get("channel"), "value": s.info.get("url"), "title": title}
        meta["live"] = {"session": s.id, "kind": kind, "offset": off, "window": [w0, w1],
                        "file_wall_start": wall0, "segments": got["segments"],
                        "discontinuity": got["discontinuity"], "ref": ref}
        project.save_meta(meta)
        from .. import clipstyle

        clipstyle.set_style(project, clipstyle.default())
        write_json_atomic(project.dir / "markers.json", {"markers": [
            {"t": round(min(w1, max(w0, (w0 + w1) / 2)) - off, 2), "source": "live", "by": kind, "id": ref or kind,
             "exact": True}], "recording_start": None})
        job.project_id = project.id
        return {"project_id": project.id, "window": [round(w0 - off, 2), round(w1 - off, 2)]}

    def then(job: Job):
        pid = job.result["project_id"]
        a, b = job.result["window"]
        _submit_analyze_window(pid, a, b, render, ref, sid)

    return A.jobs.submit("live_clip", "io", None, {"session": sid, "window": [w0, w1], "kind": kind,
                                                    "title": name}, run, then, dedupe=False)


def _submit_analyze_window(pid: str, a: float, b: float, render: bool, ref: str | None, sid: str) -> Job:
    A = _app()

    def run(job: Job):
        from .. import facecam
        from ..edit import manual
        from ..highlights import run_highlights
        from ..transcribe import transcribe_project

        project = get_project(pid)
        notes = []
        A.jobs.update(job, stage="transcribing", progress=None)
        try:
            transcribe_project(project, lang=config.get("transcribe_language") or "auto",
                               model=config.get("transcribe_model") or "auto")
        except Exception as exc:  # noqa: BLE001 - audio signals + the manual window still work
            notes.append(f"Whisper skipped: {exc}")
        A.jobs.update(job, stage="analyzing", progress=None)
        run_highlights(project, fetch_chat=False, stories_wanted=False)  # the window IS the clip
        cand = manual.add(project, a, b)
        review = state.load_review(project)
        state.save_review(project, {"accepted": sorted(set(review["accepted"]) | {cand["id"]})})
        from .. import clipstyle

        if not clipstyle.is_direct(project):  # direct style: no picture analysis
            A.jobs.update(job, stage="detecting_facecam", progress=None)
            try:
                res = facecam.detect_for_project(project, use_memory=True)
                state.save_review(project, {"facecam": res["facecam"]})
            except Exception as exc:  # noqa: BLE001 - no facecam = gameplay-only layout
                notes.append(f"facecam: {exc}")
        try:
            s = _m().get(sid)
            if ref:
                s.set_candidate(ref, status="clipped", project_id=pid, clip_id=cand["id"])
        except SharkyClipsError:
            pass
        return {"project_id": pid, "clip_id": cand["id"], "notes": notes}

    def then(job: Job):
        if render:
            project = get_project(pid)
            review = state.load_review(project)
            from ..clips import ClipOptions

            A.submit_render(pid, [job.result["clip_id"]], ClipOptions.from_dict(review["options"]), review["hooks"])

    return A.jobs.submit("live_analyze", "gpu", pid, {"window": [a, b]}, run, then, front=True)


def _wall_of(s: liverec.Session, t: float) -> str | None:
    for seg in s.segments:
        if seg.t0 - 0.01 <= t <= seg.t1:
            ws = datetime.fromisoformat(seg.wall_start)
            return (ws + timedelta(seconds=t - seg.t0)).isoformat(timespec="seconds")
    return None


def _hms(t: float) -> str:
    t = int(max(0, t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


# ================================================================== routes


@router.get("/api/liverec")
def api_liverec() -> dict:
    return {"sessions": _m().list(), "settings": liverec.settings(), "tools": liverec.tool_status(),
            "clip_last": list(CLIP_LAST),
            "disk_estimate_gb": {"7h_4mbps": round(liverec.estimate_bytes(7, 4) / 1e9, 1),
                                 "7h_8mbps": round(liverec.estimate_bytes(7, 8) / 1e9, 1)}}


@router.put("/api/liverec/settings")
def api_liverec_settings(body: dict) -> dict:
    cur = dict(config.get("liverec") or {})
    for k in liverec.DEFAULTS:
        if k in body and body[k] is not None:
            cur[k] = body[k]
    config.update(liverec=cur)
    return liverec.settings()


@router.post("/api/liverec/sessions")
def api_start(body: dict) -> dict:
    s = _m().start(str(body.get("url") or ""), name=str(body.get("name") or ""), quality=body.get("quality"),
                   retention=body.get("retention"), keep_minutes=body.get("keep_minutes"))
    return s.view()


@router.get("/api/liverec/sessions/{sid}")
def api_session(sid: str, timeline: int = 1) -> dict:
    return _m().get(sid).view(timeline=bool(timeline))


@router.post("/api/liverec/sessions/{sid}/stop")
def api_stop(sid: str) -> dict:
    return _m().stop(sid, wait=0).view()


@router.post("/api/liverec/sessions/{sid}/pause")
def api_pause(sid: str) -> dict:
    return _m().pause(sid).view()


@router.post("/api/liverec/sessions/{sid}/resume")
def api_resume(sid: str) -> dict:
    return _m().resume(sid).view()


@router.put("/api/liverec/sessions/{sid}/retention")
def api_retention(sid: str, body: dict) -> dict:
    s = _m().get(sid)
    st = dict(s.info.get("settings") or liverec.settings())
    if body.get("retention") in ("rolling", "archive"):
        st["retention"] = body["retention"]
    if body.get("keep_minutes"):
        st["keep_minutes"] = int(min(max(int(body["keep_minutes"]), 30), 120))
    s.info["settings"] = st
    s.save_info()
    s.apply_retention()
    return s.view()


@router.post("/api/liverec/sessions/{sid}/mark")
def api_mark(sid: str, body: dict | None = None) -> dict:
    s = _m().get(sid)
    if not s.segments and s.info.get("state") not in liverec.ACTIVE_STATES:
        raise SharkyClipsError("Nothing recorded yet.")
    return s.add_mark(note=str((body or {}).get("note") or "")[:80])


@router.post("/api/liverec/sessions/{sid}/clip-last")
def api_clip_last(sid: str, body: dict) -> dict:
    s = _m().get(sid)
    secs = int(body.get("seconds") or 60)
    if secs not in CLIP_LAST:
        raise SharkyClipsError(f"Pick one of {', '.join(map(str, CLIP_LAST))} seconds.")
    end = s.recorded
    start = max(float(s.info.get("first_t") or 0), end - secs)
    if end - start < 3:
        raise SharkyClipsError("Not enough recorded yet.")
    job = submit_live_clip(sid, (start, end), f"last{secs}", render=bool(body.get("render")))
    return {"job": job.to_dict(), "window": [round(start, 2), round(end, 2)]}


@router.post("/api/liverec/sessions/{sid}/candidates/{cid}/clip")
def api_cand_clip(sid: str, cid: str, body: dict | None = None) -> dict:
    s = _m().get(sid)
    c = s.candidate(cid)
    job = submit_live_clip(sid, (c["start"], c["end"]), c.get("kind") or "auto", render=bool((body or {}).get("render")),
                           ref=cid)
    s.set_candidate(cid, status="clipping", job_id=job.id)
    return {"job": job.to_dict(), "candidate": s.candidate(cid)}


@router.post("/api/liverec/sessions/{sid}/candidates/{cid}/dismiss")
def api_cand_dismiss(sid: str, cid: str) -> dict:
    s = _m().get(sid)
    c = s.set_candidate(cid, status="dismissed")
    if c.get("pin"):
        s.unpin(c["pin"])
    return c


@router.get("/api/liverec/sessions/{sid}/window.mp4")
def api_window(sid: str, t0: float, t1: float):
    """Quick preview of a window (stream copy of whole segments, cached, max 3 min)."""
    s = _m().get(sid)
    if t1 - t0 > 180 or t1 <= t0:
        raise SharkyClipsError("Preview windows are 1 s .. 3 min.")
    cache = s.dir / "preview_cache"
    cache.mkdir(exist_ok=True)
    for old in sorted(cache.glob("*.mp4"), key=lambda p: p.stat().st_mtime)[:-8]:
        old.unlink(missing_ok=True)  # keep the cache small
    dest = cache / f"w_{int(t0 * 10)}_{int(t1 * 10)}.mp4"
    if not dest.is_file():
        info = s.assemble(t0, t1, dest)
        write_json_atomic(dest.with_suffix(".json"), info)
    return FileResponse(dest, media_type="video/mp4")


@router.get("/api/liverec/sessions/{sid}/window.json")
def api_window_info(sid: str, t0: float, t1: float) -> dict:
    s = _m().get(sid)
    dest = s.dir / "preview_cache" / f"w_{int(t0 * 10)}_{int(t1 * 10)}.json"
    return read_json(dest) if dest.is_file() else {}


@router.post("/api/liverec/sessions/{sid}/backfill")
def api_backfill(sid: str) -> dict:
    return _m().start_backfill(sid)


@router.delete("/api/liverec/sessions/{sid}/backfill")
def api_backfill_cancel(sid: str) -> dict:
    return _m().cancel_backfill(sid)


@router.post("/api/liverec/sessions/{sid}/backfill/import")
def api_backfill_import(sid: str, request: Request) -> dict:
    s = _m().get(sid)
    bf = s.info.get("backfill") or {}
    if bf.get("state") != "finished" or not bf.get("file"):
        raise SharkyClipsError("No finished backfill to import.")
    path = s.dir / "backfill" / bf["file"]
    return _app().submit_import(str(path), "gaming", "auto", f"{s.info.get('title') or 'Live'} (backfill)").to_dict()


@router.post("/api/liverec/sessions/{sid}/finalize")
def api_finalize(sid: str) -> dict:
    """The recording on disk -> one ordinary project (stream copy concat of what is kept)."""
    s = _m().get(sid)
    if s.info.get("state") in liverec.ACTIVE_STATES:
        raise SharkyClipsError("Stop the recording first.")
    if not s.segments:
        raise SharkyClipsError("Nothing recorded.")
    need = s.bytes_on_disk() * 1.05
    if liverec.disk_free(s.dir) < need + 2e9:
        raise SharkyClipsError(f"Not enough free disk space for the full file (~{need / 1e9:.1f} GB).")
    A = _app()

    def run(job: Job):
        from ..ingest import ingest

        A.jobs.update(job, stage="assembling", progress=None)
        dest = s.dir / f"recording_{s.id}.mp4"
        got = s.assemble(s.segments[0].t0, s.recorded, dest)
        project = ingest(got["path"], mode="gaming")
        meta = project.load_meta()
        meta["title"] = f"{s.info.get('channel') or 'Live'} - {s.info.get('title') or s.id}"
        meta["source"] = {**(meta.get("source") or {}), "type": "live", "uploader": s.info.get("channel"),
                          "channel": s.info.get("channel"), "value": s.info.get("url")}
        meta["live"] = {"session": s.id, "kind": "full", "offset": got["offset"],
                        "file_wall_start": _wall_of(s, got["offset"]), "discontinuity": got["discontinuity"]}
        project.save_meta(meta)
        from .. import clipstyle

        clipstyle.set_style(project, clipstyle.default())
        s.info["project_id"] = project.id
        s.save_info()
        job.project_id = project.id
        return {"project_id": project.id}

    def then(job: Job):
        A.submit_transcribe(job.result["project_id"], "auto")

    return A.jobs.submit("live_finalize", "io", None, {"session": sid, "title": f"recording {sid}"}, run, then,
                         dedupe=False).to_dict()


# ================================================================== export selected clips


def export_dir() -> Path:
    custom = config.get("export_dir")
    path = Path(custom).expanduser() if custom else config.workspace_dir().parent / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def export_name(project_id: str, cid: str, start: float, end: float) -> str:
    """Reproducible: <project id>_<clip id>_<start>-<end>.mp4 (source seconds, HHhMMmSSs)."""
    def hms(t: float) -> str:
        t = int(max(0, t))
        return f"{t // 3600:02d}h{t % 3600 // 60:02d}m{t % 60:02d}s"

    return f"{project_id}_{cid}_{hms(start)}-{hms(end)}.mp4"


@router.post("/api/gaming/export-selected")
def api_export_selected(body: dict) -> dict:
    """Copy (hard link when possible) rendered clips into <export folder>/<date>/ with
    reproducible names. body: {"items": [{"project_id", "clip_id"}]} or {"all_rendered": true}.
    Nothing is published anywhere: the review gate is you."""
    items = list(body.get("items") or [])
    if body.get("all_rendered"):
        from ..project import list_projects

        for p in list_projects():
            for cid in (state.load_clips(p) or {}):
                items.append({"project_id": p.id, "clip_id": cid})
    day = datetime.now().strftime("%Y-%m-%d")
    out_dir = export_dir() / day
    out_dir.mkdir(parents=True, exist_ok=True)
    done, missing = [], []
    for it in items[:200]:
        p = get_project(str(it["project_id"]))
        clip = (state.load_clips(p) or {}).get(str(it["clip_id"]))
        src = p.dir / "clips" / clip["file"] if clip and clip.get("file") else None
        if src is None or not src.is_file():
            missing.append(it)
            continue
        dest = out_dir / export_name(p.id, str(it["clip_id"]), float(clip.get("start") or 0), float(clip.get("end") or 0))
        if not dest.exists():
            try:
                os.link(src, dest)
            except OSError:
                shutil.copy2(src, dest)
        done.append(str(dest))
    return {"folder": str(out_dir), "exported": done, "missing": missing, "at": time.strftime("%H:%M:%S"),
            "utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}


# ================================================================== gaming settings (clip shape, targets)


@router.get("/api/gaming/settings")
def api_gaming_settings() -> dict:
    from ..highlights.modes import get_mode

    p = get_mode("gaming")
    return {"tuning": {"clip_min": p.clip_min, "clip_ideal": p.clip_ideal, "clip_max": p.clip_max,
                       "pre_roll": list(p.pre_roll), "min_gap": p.min_gap},
            "candidate_count": config.get("candidate_count"), "daily_target": config.get("daily_target"),
            "weak_score": config.get("weak_score"), "export_dir": str(export_dir()),
            "story_clips": config.get("story_clips") is not False,
            "facecam_per_clip": config.get("facecam_per_clip") is not False,
            "clip_style": config.get("clip_style") or "streamer",
            "direct_fit_bg": config.get("direct_fit_bg") or "black",
            "story_per_hour": _story()["per_hour"], "story_min_score": _story()["min_score"],
            "story_package": config.get("story_package") is True,
            "story_auto_length": config.get("story_auto_length") is not False}


def _story() -> dict:
    from .. import storyfind

    return storyfind.settings()


@router.put("/api/gaming/settings")
def api_gaming_settings_put(body: dict) -> dict:
    from ..highlights.modes import MODES, tuned

    changes: dict = {}
    if isinstance(body.get("tuning"), dict):
        p = tuned(MODES["gaming"], body["tuning"])  # clamped + consistent
        mt = dict(config.get("mode_tuning") or {})
        mt["gaming"] = {"clip_min": p.clip_min, "clip_ideal": p.clip_ideal, "clip_max": p.clip_max,
                        "pre_roll": list(p.pre_roll), "min_gap": p.min_gap}
        changes["mode_tuning"] = mt
    if "candidate_count" in body:
        v = body["candidate_count"]
        changes["candidate_count"] = None if v in (None, "", 0) else int(min(max(int(v), 1), 1000))
    if body.get("daily_target") is not None:
        changes["daily_target"] = int(min(max(int(body["daily_target"]), 1), 60))
    if body.get("weak_score") is not None:
        changes["weak_score"] = int(min(max(int(body["weak_score"]), 0), 100))
    if body.get("story_per_hour") is not None:
        changes["story_per_hour"] = int(min(max(int(body["story_per_hour"]), 2), 60))
    if body.get("story_min_score") is not None:
        changes["story_min_score"] = int(min(max(int(body["story_min_score"]), 0), 90))
    for k in ("story_clips", "facecam_per_clip", "story_package", "story_auto_length"):
        if k in body:
            changes[k] = bool(body[k])
    if body.get("clip_style") in ("streamer", "direct", "both"):
        changes["clip_style"] = body["clip_style"]
    if body.get("direct_fit_bg") in ("black", "blur", "white"):
        changes["direct_fit_bg"] = body["direct_fit_bg"]
    if changes:
        config.update(**changes)
    return api_gaming_settings()


# ================================================================== AI provider (llmcloud.py)


@router.get("/api/gaming/ai")
def api_ai() -> dict:
    from .. import llmcloud

    return llmcloud.settings_view()


@router.put("/api/gaming/ai")
def api_ai_put(body: dict) -> dict:
    """{provider?, key?: {provider: "<key>" | ""}, model?: {provider: name}, fallback?, effort?}.
    Keys are write-only: they are never sent back (only a masked hint)."""
    from .. import llmcloud

    changes: dict = {}
    for p, key in (body.get("key") or {}).items():
        if p in ("anthropic", "gemini") and key is not None:
            llmcloud.save_key(p, str(key))
    if isinstance(body.get("model"), dict):
        models = dict(config.get("llm_cloud_models") or {})
        for p, m in body["model"].items():
            if p == "anthropic" and m in llmcloud.ANTHROPIC_MODELS:
                models[p] = m
            elif p == "gemini" and (m == "auto" or (isinstance(m, str) and m.startswith("gemini-") and len(m) < 60)):
                models[p] = m
        changes["llm_cloud_models"] = models
    if body.get("provider") in llmcloud.PROVIDERS:
        p = body["provider"]
        if p != "ollama" and not llmcloud.has_key(p):
            raise HTTPException(400, "Save an API key for this provider first.")
        changes["llm_provider"] = p
    if "fallback" in body:
        changes["llm_cloud_fallback"] = bool(body["fallback"])
    if body.get("effort") in ("low", "medium", "high"):
        changes["llm_cloud_effort"] = body["effort"]
    if changes:
        config.update(**changes)
    return llmcloud.settings_view()


@router.post("/api/gaming/ai/test")
def api_ai_test(body: dict) -> dict:
    from .. import llmcloud

    p = body.get("provider")
    if p not in ("anthropic", "gemini"):
        raise HTTPException(400, "provider must be anthropic or gemini")
    try:
        return llmcloud.test(p)
    except SharkyClipsError as exc:
        return {"ok": False, "error": exc.message, "hint": exc.hint}
