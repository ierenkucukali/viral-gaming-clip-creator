"""Local fork: auto-post (autopost.py) - N a day per platform, best first, never twice."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sharkyclips import autopost, config
from sharkyclips.errors import SharkyClipsError


class FakeMP:
    """Stands in for web.multipost.start: records every plan, can fail."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, str]] = []
        self.fail: dict[tuple[str, str], str] = {}
        self.problems: set[str] = set()

    def start(self, project, cid, body):
        p = body["platforms"][0]
        if p in self.problems:
            e = SharkyClipsError("Fix the marked problems first.")
            e.problems = [{"code": "not_connected"}]
            raise e
        self.calls.append((p, project.id, cid, body["when"]))
        if (p, cid) in self.fail:
            return {"results": {p: {"status": "failed", "error": {"message": self.fail[(p, cid)]}}}}
        return {"results": {p: {"status": "scheduled", "publish_at": body["when"]}}}


@pytest.fixture
def ap(monkeypatch):
    mp = FakeMP()
    projects = {"p1": SimpleNamespace(id="p1"), "p2": SimpleNamespace(id="p2")}
    cands = [{"project_id": "p1", "cid": "c01", "score": 90, "title": "A", "rendered_at": "1"},
             {"project_id": "p2", "cid": "c03", "score": 80, "title": "B", "rendered_at": "2"},
             {"project_id": "p1", "cid": "c02", "score": 70, "title": "C", "rendered_at": "3"},
             {"project_id": "p2", "cid": "c04", "score": 60, "title": "D", "rendered_at": "4"},
             {"project_id": "p2", "cid": "c05", "score": 58, "title": "E", "rendered_at": "5"}]
    app = SimpleNamespace(get_project=lambda pid: projects[pid], list_projects=lambda: list(projects.values()))
    monkeypatch.setattr(autopost, "_app", lambda: app)
    monkeypatch.setattr(autopost, "candidates", lambda min_score: [c for c in cands if c["score"] >= min_score])
    monkeypatch.setattr(autopost, "_on_platform", lambda project, cid, platform: False)
    monkeypatch.setattr(autopost, "_external_count", lambda platform, day: 0)
    conn = {p: {"connected": True, "mode": "auto", "problem": None} for p in autopost.PLATFORMS}
    monkeypatch.setattr(autopost, "connection", lambda p: conn[p])
    import sharkyclips.web.multipost as real_mp

    monkeypatch.setattr(real_mp, "start", mp.start)
    import sharkyclips.platforms as PF

    monkeypatch.setattr(PF, "record", lambda *a, **k: {})
    autopost._state_path().unlink(missing_ok=True)
    yield SimpleNamespace(mp=mp, conn=conn, cands=cands)
    config.update(autopost={})
    autopost._state_path().unlink(missing_ok=True)


NOON = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)  # early morning: every slot of the day is ahead


def _on(per_day: dict, **extra):
    autopost.save_settings({"enabled": True, "platforms": {p: {"enabled": True, "per_day": n} for p, n in per_day.items()},
                            "days_ahead": 1, "min_score": 55, **extra})


def test_off_by_default_does_nothing(ap):
    assert autopost.settings()["enabled"] is False
    assert autopost.tick(NOON) == []
    assert ap.mp.calls == []


def test_per_day_cap_per_platform_and_best_first(ap):
    _on({"youtube": 2, "tiktok": 1, "reels": 3})
    autopost.tick(NOON)
    by = {}
    for p, _pid, cid, _w in ap.mp.calls:
        by.setdefault(p, []).append(cid)
    assert by["youtube"] == ["c01", "c03"]  # best score first
    assert by["tiktok"] == ["c01"]
    assert by["reels"] == ["c01", "c03", "c02"]
    times = sorted(w for p, _pid, _cid, w in ap.mp.calls if p == "reels")
    gaps = [(datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() for a, b in zip(times, times[1:])]
    assert all(g >= autopost.GAP_MIN * 60 for g in gaps)


def test_per_day_never_above_the_hard_cap(ap):
    _on({"youtube": 50})
    assert autopost.settings()["platforms"]["youtube"]["per_day"] == autopost._cap()


def test_never_twice_across_ticks_and_days(ap):
    _on({"youtube": 1})
    autopost.tick(NOON)
    autopost.tick(NOON + timedelta(minutes=1))  # same day: full
    assert [c[2] for c in ap.mp.calls] == ["c01"]
    autopost.tick(NOON + timedelta(days=1))  # next day: the next best clip, never c01 again
    assert [c[2] for c in ap.mp.calls] == ["c01", "c03"]


def test_weak_or_low_score_clips_are_never_used(ap):
    _on({"youtube": 3}, min_score=85)
    autopost.tick(NOON)
    assert [c[2] for c in ap.mp.calls] == ["c01"]  # only one clip scores >= 85; no filler


def test_a_failed_plan_is_skipped_then_retried_later(ap, monkeypatch):
    _on({"tiktok": 1})
    ap.mp.fail[("tiktok", "c01")] = "upload failed"
    autopost.tick(NOON)
    calls = [c[2] for c in ap.mp.calls]
    assert calls == ["c01", "c03"]  # c01 failed -> the next clip takes the slot
    ap.mp.fail.clear()
    st = autopost.load_state()
    assert st["failed"]
    later = time_shift(monkeypatch, autopost.FAIL_SKIP_S + 10)
    autopost.tick(NOON + timedelta(days=1))
    assert ap.mp.calls[-1][2] == "c01"  # retried after the skip window
    assert later


def time_shift(monkeypatch, seconds: float) -> bool:
    import time as _t

    real = _t.time
    monkeypatch.setattr(autopost.time, "time", lambda: real() + seconds)
    return True


def test_login_wall_pauses_the_platform_with_a_notice(ap):
    _on({"tiktok": 2, "youtube": 1})
    ap.conn["tiktok"] = {"connected": True, "mode": "auto", "problem": "login"}
    autopost.tick(NOON)
    v = autopost.load_state()
    assert "tiktok" in v["paused"] and "login" in v["paused"]["tiktok"]["reason"]
    assert all(c[0] != "tiktok" for c in ap.mp.calls)
    assert any(c[0] == "youtube" for c in ap.mp.calls)  # the others carry on
    ap.conn["tiktok"] = {"connected": True, "mode": "auto", "problem": None}
    autopost.resume("tiktok")
    autopost.tick(NOON)
    assert any(c[0] == "tiktok" for c in ap.mp.calls)


def test_plan_problems_pause_the_platform(ap):
    _on({"reels": 1})
    ap.mp.problems.add("reels")
    autopost.tick(NOON)
    assert "reels" in autopost.load_state()["paused"]


def test_not_connected_pauses(ap):
    _on({"youtube": 1})
    ap.conn["youtube"] = {"connected": False, "mode": "auto", "problem": "not_connected"}
    autopost.tick(NOON)
    assert autopost.load_state()["paused"]["youtube"]["reason"] == "Not connected."
    assert ap.mp.calls == []


def test_view_lists_next_posts(ap):
    _on({"youtube": 2})
    autopost.tick(NOON)
    v = autopost.view()
    assert v["platforms"]["youtube"]["enabled"] and v["settings"]["enabled"]
    assert v["log"] and v["log"][0]["kind"] == "planned"


def test_old_render_of_another_cut_is_not_posted():
    assert autopost._same_cut({"start": 10, "end": 70}, {"start": 12, "end": 75})
    assert not autopost._same_cut({"start": 94.7, "end": 111.8}, {"start": 966.2, "end": 1042.2})
    assert autopost._same_cut({}, {"start": 1, "end": 2})
