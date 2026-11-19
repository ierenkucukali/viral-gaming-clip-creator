"""Local fork: Buffer publishing (bufferpost.py) against a fake Buffer GraphQL API + fake Cloudinary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sharkyclips import bufferpost as B
from sharkyclips import config


class Resp:
    def __init__(self, code: int, body: dict, headers: dict | None = None):
        self.status_code, self._body, self.headers = code, body, headers or {}

    def json(self):
        return self._body


class FakeNet:
    """Buffer (api.buffer.com GraphQL) + Cloudinary upload / destroy / usage."""

    def __init__(self):
        self.posts: dict[str, dict] = {}
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.uploads: list[str] = []
        self.destroyed: list[str] = []
        self.limit = 10
        self.auth_ok = True
        self.calls = 0

    def post(self, url, json=None, data=None, files=None, headers=None, timeout=None, auth=None):
        assert timeout, "every request has a timeout"
        if url == B.API:
            self.calls += 1
            assert headers["Authorization"] == "Bearer buf_test_key_123456"
            if not self.auth_ok:
                return Resp(401, {"errors": [{"message": "Not authorized", "extensions": {"code": "UNAUTHORIZED"}}]})
            return Resp(200, self._gql(json["query"], json.get("variables") or {}))
        if url.endswith("/video/upload"):
            assert data["signature"] == B._sign({k: data[k] for k in ("folder", "public_id", "timestamp")}, "sekret")
            self.uploads.append(data["public_id"])
            return Resp(200, {"secure_url": f"https://res.cloudinary.com/demo/video/upload/{data['public_id']}.mp4",
                              "public_id": f"sharkyclips/{data['public_id']}", "bytes": 1234})
        if url.endswith("/video/destroy"):
            self.destroyed.append(data["public_id"])
            return Resp(200, {"result": "ok"})
        raise AssertionError(url)

    def get(self, url, auth=None, timeout=None):
        assert url.endswith("/usage") and auth == ("ck_123", "sekret")
        return Resp(200, {"plan": "Free", "credits": {"usage": 0.4, "limit": 25}})

    def _gql(self, q: str, v: dict) -> dict:
        if "account" in q:
            return {"data": {"account": {"id": "a1", "name": "Me", "organizations": [
                {"id": "org1", "name": "My org", "channelCount": 3, "limits": {"scheduledPosts": 10}}]}}}
        if "channels(" in q:
            assert v["org"] == "org1"
            return {"data": {"channels": [
                {"id": "chIG", "name": "ig", "service": "instagram", "isDisconnected": False, "isLocked": False},
                {"id": "chTT", "name": "tt", "service": "tiktok", "isDisconnected": False, "isLocked": False},
                {"id": "chYT", "name": "yt", "service": "youtube", "isDisconnected": False, "isLocked": False}]}}
        if "createPost" in q:
            inp = v["input"]
            n = sum(1 for p in self.posts.values() if p["channelId"] == inp["channelId"] and p["status"] == "scheduled")
            if n >= self.limit:
                return {"data": {"createPost": {"message": "You have reached the scheduled posts limit"}}}
            pid = f"post{len(self.created) + 1}"
            self.created.append(inp)
            self.posts[pid] = {"channelId": inp["channelId"], "status": "scheduled", "dueAt": inp["dueAt"]}
            return {"data": {"createPost": {"post": {"id": pid, "dueAt": inp["dueAt"], "status": "scheduled"}}}}
        if "deletePost" in q:
            self.deleted.append(v["id"])
            self.posts.pop(v["id"], None)
            return {"data": {"deletePost": {"id": v["id"]}}}
        if "post(input" in q:
            p = self.posts[v["id"]]
            return {"data": {"post": {"id": v["id"], "status": p["status"], "dueAt": p["dueAt"],
                                      "sentAt": p.get("sentAt"), "externalLink": None, "error": p.get("error")}}}
        raise AssertionError(q)


@pytest.fixture
def env(monkeypatch, tmp_path):
    net = FakeNet()
    monkeypatch.setattr(B, "_http", lambda: net)
    projects = {}
    clips, hls = {}, {}
    for pid in ("p1", "p2"):
        d = tmp_path / pid
        (d / "clips").mkdir(parents=True)
        cl, cands = {}, []
        for k in range(1, 8):
            cid = f"c{k:02d}"
            (d / "clips" / f"{k:02d}_{cid}.mp4").write_bytes(b"\0" * 1000)
            cl[cid] = {"file": f"{k:02d}_{cid}.mp4", "rank": k, "start": 100.0 * k, "end": 100.0 * k + 50, "duration": 50.0}
            cands.append({"id": cid, "start": 100.0 * k, "end": 100.0 * k + 50, "peak": 100.0 * k + 30,
                          "story": {"headline": f"Speed found <the> SECRET island number {k} 😳🌴", "title": "t", "score": 90 - k}})
        clips[pid], hls[pid] = cl, {"candidates": cands}
        projects[pid] = SimpleNamespace(id=pid, dir=d, load_meta=lambda: {"title": "Speed GTA", "source": {"uploader": "IShowSpeed"},
                                                                           "created_at": "2026-11-19"})
    app = SimpleNamespace(get_project=lambda pid: projects[pid], list_projects=lambda: list(projects.values()))
    monkeypatch.setattr(B, "_app", lambda: app)
    from sharkyclips.web import state

    monkeypatch.setattr(state, "load_clips", lambda project: clips[project.id])
    monkeypatch.setattr(state, "load_highlights", lambda project, *a, **k: hls[project.id])
    B._state_path().unlink(missing_ok=True)
    B.save_keys({"buffer": "buf_test_key_123456", "cloudinary": {"cloud_name": "demo", "api_key": "ck_123", "api_secret": "sekret"}})
    yield SimpleNamespace(net=net)
    config.update(buffer={})
    B._state_path().unlink(missing_ok=True)
    B._key_path().unlink(missing_ok=True)


def _picks(n: int, pid: str = "p1") -> list[dict]:
    return [{"project_id": pid, "cid": f"c{k:02d}"} for k in range(1, n + 1)]


def test_keys_are_write_only_and_masked(env):
    v = B.keys_view()
    assert v["has_buffer"] and v["has_cloudinary"]
    assert "buf_test_key_123456" not in json.dumps(v) and "sekret" not in json.dumps(v)
    assert "buf_test_key_123456" not in B._key_path().read_bytes().decode("latin-1")


def test_test_links_one_channel_per_platform(env):
    r = B.test()
    assert r["ok"] and r["buffer"]["organization"] == "My org"
    assert B.settings()["channels"] == {"instagram": "chIG", "tiktok": "chTT", "youtube": "chYT"}
    assert r["cloudinary"]["plan"] == "Free"


def test_ten_a_day_spread_over_the_window():
    s = B.settings()
    assert s["per_day"] == {"instagram": 10, "tiktok": 10, "youtube": 10}
    st = {"items": []}
    day = datetime(2026, 11, 20, 0, 1).astimezone()
    slots = B.next_slots(st, "tiktok", 12, now=day.astimezone(timezone.utc) - timedelta(hours=1), s=s)
    first_day = [x for x in slots if x.astimezone().date() == day.date()]
    assert len(first_day) == 10
    assert first_day[0].astimezone().strftime("%H:%M") == "10:00" and first_day[-1].astimezone().strftime("%H:%M") == "23:00"
    assert slots[10].astimezone().date() == day.date() + timedelta(days=1)


def test_fixed_times_and_per_day_are_flexible():
    B.save_settings({"per_day": {"youtube": 3}, "times": ["21:00", "12:00", "18:30", "bad"]})
    s = B.settings()
    assert s["times"] == ["12:00", "18:30", "21:00"] and s["per_day"]["youtube"] == 3
    now = datetime(2026, 11, 20, 0, 0).astimezone().astimezone(timezone.utc)
    got = [x.astimezone().strftime("%H:%M") for x in B.next_slots({"items": []}, "youtube", 4, now=now, s=s)]
    assert got == ["12:00", "18:30", "21:00", "12:00"]
    config.update(buffer={})


def test_queue_pushes_only_up_to_the_free_plan_limit_and_the_rest_waits(env):
    B.test()
    res = B.enqueue(_picks(7) + _picks(7, "p2"))
    assert res["added"] == 14 * 3  # one post per platform
    for _ in range(20):
        B.tick()
    v = B.view()
    for p in B.PLATFORMS:
        assert v["platforms"][p]["scheduled"] == 10 and v["platforms"][p]["waiting"] == 4
    assert len(env.net.uploads) == 10  # one upload per clip, shared by the three platforms
    yt = next(c for c in env.net.created if c["channelId"] == "chYT")
    assert yt["mode"] == "customScheduled" and yt["schedulingType"] == "automatic"
    assert yt["metadata"]["youtube"]["title"] == "Speed found the SECRET island number 1 😳🌴"  # no < >
    assert yt["metadata"]["youtube"]["categoryId"] == "20" and yt["metadata"]["youtube"]["privacy"] == "public"
    assert "#Shorts" in yt["text"] and "<" not in yt["text"]
    ig = next(c for c in env.net.created if c["channelId"] == "chIG")
    assert ig["metadata"]["instagram"] == {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": False}
    assert ig["assets"][0]["video"]["url"].startswith("https://res.cloudinary.com/")
    assert ig["assets"][0]["video"]["metadata"]["thumbnailOffset"] == 30000  # the peak
    assert "#ishowspeed" in ig["text"] and "#gta6" in ig["text"]


def test_never_twice_on_one_platform(env):
    B.test()
    B.enqueue(_picks(2))
    again = B.enqueue(_picks(2))
    assert again["added"] == 0


def test_sent_posts_free_slots_and_media_is_deleted(env, monkeypatch):
    B.test()
    B.enqueue(_picks(1))
    B.tick()
    for p in env.net.posts.values():
        p.update(status="sent", sentAt="2026-11-19T10:00:00Z")
    later = datetime.now(timezone.utc) + timedelta(days=3)
    B.tick(now=later, force_sync=True)
    v = B.view()
    assert all(v["platforms"][p]["sent"] == 1 for p in B.PLATFORMS)
    assert env.net.destroyed == [f"sharkyclips/{env.net.uploads[0]}"]


def test_cancel_deletes_the_buffer_post(env):
    B.test()
    B.enqueue(_picks(1))
    B.tick()
    it = next(i for i in B.load_state()["items"] if i["platform"] == "tiktok")
    B.cancel(it["id"])
    assert env.net.deleted == [it["post_id"]]
    assert next(i for i in B.load_state()["items"] if i["id"] == it["id"])["state"] == "cancelled"


def test_a_refused_key_pauses_instead_of_hammering(env):
    B.test()
    B.enqueue(_picks(2))
    env.net.auth_ok = False
    B.tick()
    n = env.net.calls
    B.tick()
    B.tick()
    assert env.net.calls == n  # paused
    assert "refused" in B.view()["paused"]["reason"]
    env.net.auth_ok = True
    B.resume()
    v = B.view()
    assert v["paused"] is None
    assert sum(v["platforms"][p]["scheduled"] for p in B.PLATFORMS) == B.MAX_PUSH_PER_TICK  # a few per tick


def test_missed_slot_moves_to_the_next_free_one(env):
    B.test()
    B.enqueue(_picks(1))
    st = B.load_state()
    for i in st["items"]:
        i["due_at"] = "2026-01-01T10:00:00.000Z"  # the PC was off
    B._save_state(st)
    B.tick()
    for i in B.load_state()["items"]:
        assert i["state"] == "scheduled" and B._parse(i["due_at"]) > datetime.now(timezone.utc)
