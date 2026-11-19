"""How long a video takes on THIS PC, from add to ready-to-pick clips (Gaming page).

Every number comes from what this PC measured on earlier videos, with a stated default when
nothing was measured yet:
  * transcription: transcript.json "timing" (Whisper speed vs real time),
  * story finder: highlights.json "stories.seconds" (local model or cloud API),
  * clip packages (titles / description): workspace/_speeds.json package_* (seconds per clip),
  * rendering: workspace/_speeds.json "render" (seconds of work per second of clip).
The download (URL imports) depends on the internet line and is shown as a size only.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from . import config

DEFAULTS = {
    "transcribe_rtf": 25.0,  # x real time
    "signals_s_per_min": 1.5,
    "story_local_s_per_min": 8.0,
    "story_cloud_s_per_min": 1.2,
    "story_cloud_min_s": 40.0,
    "facecam_s": 30.0,
    "facecam_s_per_clip": 4.0,
    "package_local_s_per_clip": 45.0,
    "package_cloud_s_per_clip": 12.0,
    "render_s_per_s": 0.6,
}
GB_PER_HOUR_1080P60 = 1.35  # measured: 35 min AV1 1080p60 = 0.77 GB
SIZES_MIN = (30, 60, 180, 300, 600)


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def measured() -> dict:
    ws = config.workspace_dir()
    rtf, story_local, story_cloud = [], [], []
    for d in sorted(ws.iterdir()) if ws.is_dir() else []:
        if not d.is_dir() or d.name.startswith("_"):
            continue
        t = _read(d / "transcript.json") if (d / "transcript.json").is_file() else None
        tm = (t or {}).get("timing") or {}
        if tm.get("realtime_factor"):
            rtf.append(float(tm["realtime_factor"]))
        if (d / "highlights.json").is_file():
            h = _read(d / "highlights.json") or {}
            st = h.get("stories") or {}
            dur = float(h.get("duration") or 0)
            if st.get("seconds") and dur > 60:
                (story_cloud if st.get("cloud") else story_local).append(float(st["seconds"]) / (dur / 60))
    sp = _read(ws / "_speeds.json") or {}
    out: dict = {}
    if rtf:
        out["transcribe_rtf"] = statistics.median(rtf)
    if story_local:
        out["story_local_s_per_min"] = statistics.median(story_local)
    if story_cloud:
        out["story_cloud_s_per_min"] = statistics.median(story_cloud)
    for k, key in (("package_local", "package_local_s_per_clip"), ("package_cloud", "package_cloud_s_per_clip"),
                   ("render", "render_s_per_s")):
        if (sp.get(k) or {}).get("per_second"):
            out[key] = float(sp[k]["per_second"])
    return out


def clips_for(minutes: float) -> int:
    """About how many story clips the AI returns (storyfind: ~6 an hour + 2, capped by the count)."""
    try:
        cap = int(config.get("candidate_count") or config.get("story_count") or 15)
    except (TypeError, ValueError):
        cap = 15
    return max(2, min(cap, round(minutes / 60 * 6 + 2)))


def plan(minutes: float, cloud: bool, streamer: bool, r: dict) -> dict:
    n = clips_for(minutes)
    ideal = float(((config.get("mode_tuning") or {}).get("gaming") or {}).get("clip_ideal") or 55)
    st = {"transcribe": 15 + minutes * 60 / r["transcribe_rtf"],
          "audio": minutes * r["signals_s_per_min"],
          "story": max(r["story_cloud_min_s"], minutes * r["story_cloud_s_per_min"]) if cloud
          else minutes * r["story_local_s_per_min"],
          "facecam": (r["facecam_s"] + n * r["facecam_s_per_clip"]) if streamer else 0.0,
          "package": n * (r["package_cloud_s_per_clip"] if cloud else r["package_local_s_per_clip"]),
          "render": n * ideal * r["render_s_per_s"]}
    return {"minutes": minutes, "clips": n, "stages": {k: round(v) for k, v in st.items()},
            "total_s": round(sum(st.values())), "download_gb": round(minutes / 60 * GB_PER_HOUR_1080P60, 1)}


def view() -> dict:
    from . import llmcloud

    m = measured()
    r = {**DEFAULTS, **m}
    cloud = llmcloud.active() is not None
    style = str(config.get("clip_style") or "streamer")
    return {"measured": sorted(m), "rates": {k: round(v, 3) for k, v in r.items()}, "cloud": cloud,
            "provider": llmcloud.provider(), "style": style,
            "rows": [{"minutes": mm, "now": plan(mm, cloud, style != "direct", r),
                      "local": plan(mm, False, style != "direct", r), "cloud": plan(mm, True, style != "direct", r)}
                     for mm in SIZES_MIN]}
