"""Local fork: Gaming page routes for Buffer publishing, the AI prompts and the time estimate."""

from __future__ import annotations

import subprocess

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .. import bufferpost, config
from ..errors import SharkyClipsError

router = APIRouter()


# ===================================================================== Buffer


@router.get("/api/buffer")
def api_buffer() -> dict:
    return bufferpost.view()


@router.put("/api/buffer/settings")
def api_buffer_settings(body: dict) -> dict:
    bufferpost.save_settings(body or {})
    return bufferpost.view()


@router.put("/api/buffer/keys")
def api_buffer_keys(body: dict) -> dict:
    """{"buffer"?: "<key>" | "", "cloudinary"?: {...} | {}}. Write-only: only masked hints come back."""
    bufferpost.save_keys(body or {})
    return bufferpost.view()


@router.post("/api/buffer/test")
def api_buffer_test() -> dict:
    return {**bufferpost.test(), "view": bufferpost.view()}


@router.get("/api/buffer/clips")
def api_buffer_clips() -> dict:
    return {"clips": bufferpost.picker()}


@router.get("/api/buffer/thumb/{project_id}/{cid}.jpg")
def api_buffer_thumb(project_id: str, cid: str):
    """A 9:16 frame of the rendered clip (at the story's peak) for the picker."""
    from ..errors import require_tool
    from ..procs import no_window
    from . import app as A

    try:
        project = A.get_project(project_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(404, "No such project") from exc
    f = bufferpost._clip_file(project, cid)
    if f is None:
        raise HTTPException(404, "Clip not rendered")
    from . import state

    cand = next((c for c in (state.load_highlights(project) or {}).get("candidates") or [] if c["id"] == cid), {})
    off = (bufferpost._peak_ms(project, cid, cand) or 1500) / 1000
    dest = project.cache_dir / "frames" / f"clip_{cid}_{int(f.stat().st_mtime)}.jpg"
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        cmd = [require_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{off:.2f}", "-i", str(f),
               "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(dest)]
        subprocess.run(cmd, capture_output=True, timeout=60, **no_window())
    if not dest.is_file():
        raise HTTPException(500, "Could not extract a frame")
    return FileResponse(dest, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@router.get("/api/buffer/room")
def api_buffer_room() -> dict:
    """How many more posts Buffer takes now per platform (asks Buffer)."""
    bufferpost.room(fresh=True)
    return bufferpost.view()


@router.post("/api/buffer/send")
def api_buffer_send(body: dict) -> dict:
    """Send the picked clips to Buffer now (in the background: GET /api/buffer shows "sending")."""
    picks = (body or {}).get("picks") or []
    if not isinstance(picks, list) or not picks:
        raise HTTPException(400, "Pick at least one clip.")
    res = bufferpost.send(picks, start=body.get("start") or None)
    return {**res, "view": bufferpost.view()}


@router.get("/api/buffer/preview")
def api_buffer_preview(count: int = 1, platforms: str = "", start: str = "") -> dict:
    return bufferpost.preview(count, [p for p in platforms.split(",") if p] or None, start or None)


@router.put("/api/buffer/items/{item_id}/time")
def api_buffer_time(item_id: str, body: dict) -> dict:
    return bufferpost.set_time(item_id, str((body or {}).get("due_at") or ""))


@router.post("/api/buffer/items/{item_id}/cancel")
def api_buffer_cancel(item_id: str) -> dict:
    return bufferpost.cancel(item_id)


@router.post("/api/buffer/items/{item_id}/retry")
def api_buffer_retry(item_id: str) -> dict:
    return bufferpost.retry(item_id)


@router.post("/api/buffer/clear")
def api_buffer_clear() -> dict:
    return bufferpost.clear_done()


@router.post("/api/buffer/run")
def api_buffer_run() -> dict:
    """"Check now": which posts were published / failed (asks Buffer), and the room."""
    acts = bufferpost.tick(force_sync=True)
    bufferpost.room(fresh=True)
    return {"actions": acts, "view": bufferpost.view()}


@router.post("/api/buffer/resume")
def api_buffer_resume() -> dict:
    return bufferpost.resume()


# ===================================================================== AI prompts + time estimate


@router.get("/api/gaming/prompts")
def api_prompts() -> dict:
    from .. import analyze, storyfind

    return {"story": storyfind.prompt_preview(), "package_system": analyze.SYSTEM,
            "auto_render": config.get("gaming_auto_render") is not False}


@router.put("/api/gaming/prompts")
def api_prompts_put(body: dict) -> dict:
    changes: dict = {}
    if "extra" in (body or {}):
        text = str(body.get("extra") or "")
        if len(text) > 2000:
            raise SharkyClipsError("Keep the extra instructions under 2,000 characters.")
        changes["story_extra_prompt"] = text.strip()
    if "auto_render" in (body or {}):
        changes["gaming_auto_render"] = bool(body["auto_render"])
    if changes:
        config.update(**changes)
    return api_prompts()


@router.get("/api/gaming/estimate")
def api_estimate(minutes: float | None = None, style: str | None = None) -> dict:
    from .. import estimate

    if minutes is not None:
        return estimate.for_minutes(minutes, style if style in ("streamer", "direct", "both") else None)
    return estimate.view()


# ===================================================================== trim before clipping


@router.get("/api/gaming/trim")
def api_trim_list() -> dict:
    """Videos added with "trim first" that wait for the part to clip."""
    from . import app as A

    out = []
    for p in A.list_projects():
        try:
            m = p.load_meta()
        except Exception:  # noqa: BLE001
            continue
        if m.get("awaiting_trim"):
            style = "both" if m.get("both_styles") else (m.get("clip_style") or "streamer")
            out.append({"id": p.id, "title": m.get("title") or p.id, "duration": m.get("duration") or 0,
                        "style": style, "streamer": m.get("streamer")})
    busy = [j.project_id for j in A.jobs.jobs() if j.params.get("trim") and j.status in ("queued", "running")]
    return {"videos": [v for v in out if v["id"] not in busy]}


@router.post("/api/gaming/trim/{project_id}")
def api_trim(project_id: str, body: dict) -> dict:
    from . import app as A

    m = A.get_project(project_id).load_meta()
    if not m.get("awaiting_trim"):
        raise HTTPException(409, "This video is already being clipped.")
    start = float((body or {}).get("start") or 0)
    end = float((body or {}).get("end") or m.get("duration") or 0)
    return A.submit_trim(project_id, start, end).to_dict()
