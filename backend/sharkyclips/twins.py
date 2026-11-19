"""Both clip styles from one analysis (local fork).

A video imported with clip style "both" is analysed once (Whisper, sound, AI stories, AI captions)
as a streamer-style project. When that is done, a "twin" project is made next to it: the same
source (hard link, no copy), the same transcript and the same stories with their headlines and
captions, but the direct style (the whole 16:9 picture centred). Its clips render by themselves.
So every story exists twice, in two looks, without a second AI run.

meta.json: the original has "both_styles": true and "twin_id"; the twin has "twin_of".
"""

from __future__ import annotations

import os
import shutil

from .project import Project, get_project, read_json, write_json_atomic

COPY = ("transcript.json", "highlights.json", "hype_curve.json", "chat.json", "bleep_fixes.json")
LINK = ("audio.wav",)


def wanted(project: Project) -> bool:
    meta = project.load_meta()
    return bool(meta.get("both_styles")) and not meta.get("twin_of")


def _link(src, dest) -> None:
    try:
        os.link(src, dest)
    except OSError:  # another disk / no hard links: a copy
        shutil.copy2(src, dest)


def make_twin(project: Project) -> Project | None:
    """The direct-style twin of a "both" project (made once; an existing one gets the newest
    stories and captions). None when the project doesn't want one or isn't analysed yet."""
    if not wanted(project) or not (project.dir / "highlights.json").is_file():
        return None
    meta = project.load_meta()
    tid = meta.get("twin_id")
    if tid:
        try:
            twin = get_project(tid)
        except Exception:  # noqa: BLE001 - deleted: make it again
            twin = None
        if twin is not None:
            sync(project, twin)
            return twin
    src = project.source_path
    if src is None:
        return None
    base = f"{project.id[:60]}-direct"
    tid, n = base, 2
    while (project.dir.parent / tid).exists():
        tid, n = f"{base}{n}", n + 1
    tdir = project.dir.parent / tid
    tdir.mkdir()
    _link(src, tdir / src.name)
    for name in LINK:
        if (project.dir / name).is_file():
            _link(project.dir / name, tdir / name)
    for name in COPY:
        if (project.dir / name).is_file():
            shutil.copy2(project.dir / name, tdir / name)
    hl = read_json(tdir / "highlights.json")
    hl["project_id"] = tid
    write_json_atomic(tdir / "highlights.json", hl)
    tmeta = {k: v for k, v in meta.items() if k not in ("twin_id", "both_styles", "analysis", "full_upload")}
    tmeta.update(id=tid, title=f"{meta.get('title') or project.id} (direct)", twin_of=project.id,
                 clip_style="direct")
    twin = Project(tid, tdir)
    twin.save_meta(tmeta)
    meta["twin_id"] = tid
    project.save_meta(meta)
    from . import clipstyle

    clipstyle.set_style(twin, "direct")
    return twin


def sync(project: Project, twin: Project) -> None:
    """The newest AI captions (publish) and stories of the original go to the twin, clip by clip."""
    try:
        hl = read_json(project.dir / "highlights.json")
        th = read_json(twin.dir / "highlights.json")
    except (OSError, ValueError):
        return
    by_id = {c["id"]: c for c in hl.get("candidates") or []}
    changed = False
    for c in th.get("candidates") or []:
        o = by_id.get(c["id"])
        if o is None:
            continue
        for k in ("publish", "story", "llm"):
            if o.get(k) is not None and c.get(k) != o.get(k):
                c[k] = o[k]
                changed = True
    if changed:
        write_json_atomic(twin.dir / "highlights.json", th)
