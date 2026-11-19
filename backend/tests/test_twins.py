"""Local fork: clip style "both" = one analysis, a direct-style twin with the same stories (twins.py)."""

from __future__ import annotations

import pytest

import json

from sharkyclips import clipstyle, config, twins
from sharkyclips.project import Project


@pytest.fixture(autouse=True)
def _clean_workspace():
    """These tests make projects in the throwaway workspace: remove them again (test_web expects none)."""
    root = config.workspace_dir()
    before = {p.name for p in root.iterdir()}
    yield
    import shutil

    for p in root.iterdir():
        if p.name not in before:
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def _project(pid: str = "speed-gta-1234abcd") -> Project:
    d = config.workspace_dir() / pid
    d.mkdir(parents=True)
    (d / "source.mp4").write_bytes(b"\0" * 2048)
    (d / "audio.wav").write_bytes(b"\0" * 512)
    (d / "transcript.json").write_text(json.dumps({"segments": []}))
    hl = {"project_id": pid, "mode": "gaming", "stories": {"v": 2, "count": 1},
          "candidates": [{"id": "c01", "rank": 1, "start": 10.0, "end": 60.0, "quality": "good",
                          "story": {"headline": "Speed was FURIOUS 😡", "score": 80}}]}
    (d / "highlights.json").write_text(json.dumps(hl))
    p = Project(pid, d)
    p.save_meta({"id": pid, "title": "Speed GTA", "mode": "gaming", "streamer": "IShowSpeed"})
    return p


def test_both_makes_a_direct_twin_with_the_same_stories():
    p = _project()
    clipstyle.set_style(p, "both")
    meta = p.load_meta()
    assert meta["clip_style"] == "streamer" and meta["both_styles"] is True
    twin = twins.make_twin(p)
    assert twin is not None and twin.id == "speed-gta-1234abcd-direct"
    tm = twin.load_meta()
    assert tm["clip_style"] == "direct" and tm["twin_of"] == p.id and tm["title"] == "Speed GTA (direct)"
    assert tm["streamer"] == "IShowSpeed" and "both_styles" not in tm
    assert p.load_meta()["twin_id"] == twin.id
    th = json.loads((twin.dir / "highlights.json").read_text())
    assert th["project_id"] == twin.id and th["candidates"][0]["story"]["headline"] == "Speed was FURIOUS 😡"
    assert (twin.dir / "source.mp4").stat().st_ino == (p.dir / "source.mp4").stat().st_ino  # a link, not a copy
    review = json.loads((twin.dir / "review.json").read_text())
    assert review["options"]["layout"] == "fit"
    assert twins.make_twin(twin) is None  # a twin never makes its own twin


def test_later_captions_reach_the_twin_and_it_is_made_once():
    p = _project("speed-gta-5678abcd")
    clipstyle.set_style(p, "both")
    twin = twins.make_twin(p)
    hl = json.loads((p.dir / "highlights.json").read_text())
    hl["candidates"][0]["publish"] = {"titles": ["x"], "hashtags": ["#gta6"], "description": "IShowSpeed loses it."}
    (p.dir / "highlights.json").write_text(json.dumps(hl))
    again = twins.make_twin(p)
    assert again is not None and again.id == twin.id
    th = json.loads((twin.dir / "highlights.json").read_text())
    assert th["candidates"][0]["publish"]["description"] == "IShowSpeed loses it."
    assert not (p.dir.parent / f"{p.id}-direct2").exists()


def test_projects_without_both_get_no_twin():
    p = _project("speed-gta-9999abcd")
    clipstyle.set_style(p, "streamer")
    assert twins.make_twin(p) is None
    assert "twin_id" not in p.load_meta()


def test_after_the_analysis_the_twin_is_made_and_its_clips_render(monkeypatch):
    from sharkyclips.web import app as A

    p = _project("speed-gta-4242abcd")
    clipstyle.set_style(p, "both")
    rendered = []
    monkeypatch.setattr(A, "submit_auto_render", lambda pid: rendered.append(pid) or [])
    A.submit_twin(p.id)
    A.submit_twin(p.id)  # a second analysis: captions synced, no second render batch
    assert rendered == [f"{p.id}-direct"]
    other = _project("speed-gta-4343abcd")
    clipstyle.set_style(other, "direct")
    A.submit_twin(other.id)
    assert rendered == [f"{p.id}-direct"]
