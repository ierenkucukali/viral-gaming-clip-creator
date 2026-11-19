"""Local fork: Buffer publishing (bufferpost.py) against a fake Buffer GraphQL API + fake Cloudinary."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sharkyclips import bufferpost as B
from sharkyclips import config
from sharkyclips.errors import SharkyClipsError


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
        self.edited: list[dict] = []
        self.room_asks = 0
        self.plan_limit = 10
        self.own: dict[str, int] = {}  # posts the user scheduled in Buffer itself, per channel
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
                {"id": "org1", "name": "My org", "channelCount": 3, "limits": {"scheduledPosts": self.plan_limit}}]}}}
        if "channels(" in q:
            assert v["org"] == "org1"
            return {"data": {"channels": [
                {"id": "chIG", "name": "ig", "service": "instagram", "isDisconnected": False, "isLocked": False},
                {"id": "chTT", "name": "tt", "service": "tiktok", "isDisconnected": False, "isLocked": False},
                {"id": "chYT", "name": "yt", "service": "youtube", "isDisconnected": False, "isLocked": False}]}}
        if "posts(first" in q:  # what is scheduled in Buffer now (also posts made in Buffer itself)
            self.room_asks += 1
            nodes = [{"id": k, "channelId": p["channelId"], "dueAt": p["dueAt"]} for k, p in self.posts.items()
                     if p["status"] == "scheduled" and p["channelId"] in v["ch"]]
            nodes += [{"id": f"own{k}", "channelId": c, "dueAt": "2026-12-01T10:00:00Z"} for c, n in self.own.items() for k in range(n)]
            return {"data": {"posts": {"edges": [{"node": n} for n in nodes]}}}
        if "createPost" in q:
            inp = v["input"]
            n = sum(1 for p in self.posts.values() if p["channelId"] == inp["channelId"] and p["status"] == "scheduled")
            if n >= self.limit:
                return {"data": {"createPost": {"message": "You have reached the scheduled posts limit"}}}
            pid = f"post{len(self.created) + 1}"
            self.created.append(inp)
            self.posts[pid] = {"channelId": inp["channelId"], "status": "scheduled", "dueAt": inp["dueAt"]}
            return {"data": {"createPost": {"post": {"id": pid, "dueAt": inp["dueAt"], "status": "scheduled"}}}}
        if "editPost" in q:
            inp = v["input"]
            self.edited.append(inp)
            self.posts[inp["id"]]["dueAt"] = inp["dueAt"]
            return {"data": {"editPost": {"post": {"id": inp["id"], "dueAt": inp["dueAt"], "status": "scheduled"}}}}
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
        for k in range(1, 26):
            cid = f"c{k:02d}"
            (d / "clips" / f"{k:02d}_{cid}.mp4").write_bytes(b"\0" * 1000)
            cl[cid] = {"file": f"{k:02d}_{cid}.mp4", "rank": k, "start": 100.0 * k, "end": 100.0 * k + 50, "duration": 50.0}
            cands.append({"id": cid, "start": 100.0 * k, "end": 100.0 * k + 50, "peak": 100.0 * k + 30,
                          "story": {"headline": f"Speed found <the> SECRET island number {k} 😳🌴", "title": "t", "score": 90 - k,
                                    "game": "GTA 6", "why": "Speed finds an island."}})
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
    B.save_settings({"mode": "times", "per_day": {"youtube": 3}, "times": ["21:00", "12:00", "18:30", "bad"]})
    s = B.settings()
    assert s["times"] == ["12:00", "18:30", "21:00"] and s["per_day"]["youtube"] == 3
    now = datetime(2026, 11, 20, 0, 0).astimezone().astimezone(timezone.utc)
    got = [x.astimezone().strftime("%H:%M") for x in B.next_slots({"items": []}, "youtube", 4, now=now, s=s)]
    assert got == ["12:00", "18:30", "21:00", "12:00"]
    config.update(buffer={})


def test_send_goes_straight_to_buffer_up_to_the_room(env):
    B.test()
    r = B.send(_picks(14), wait=True)
    assert r["count"] == 30 and r["skipped"] == 12  # 10 per channel (free plan), 4 clips x 3 for next time
    sd = B.sending()
    assert sd["sent"] == 30 and sd["failed"] == 0 and not sd["running"]
    v = B.view()
    for p in B.PLATFORMS:
        assert v["platforms"][p]["scheduled"] == 10
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
    with pytest.raises(SharkyClipsError, match="full"):  # no room left: a clear message, nothing sent
        B.send([{"project_id": "p1", "cid": "c12"}], wait=True)


def test_room_counts_posts_made_in_buffer_itself(env):
    B.test()
    env.net.own = {"chTT": 7}
    r = B.room(fresh=True)
    assert r["room"] == {"instagram": 10, "tiktok": 3, "youtube": 10}
    B.send(_picks(5), wait=True)
    assert B.view()["platforms"]["tiktok"]["scheduled"] == 3


def test_nothing_happens_while_the_app_is_closed_or_idle(env):
    B.test()
    B.send(_picks(3), wait=True)
    created = len(env.net.created)
    for _ in range(5):  # the publish loop while the app is open: never sends anything by itself
        B.tick()
    assert len(env.net.created) == created


def test_never_twice_on_one_platform(env):
    B.test()
    B.send(_picks(2), wait=True)
    with pytest.raises(SharkyClipsError, match="already"):
        B.send(_picks(2), wait=True)


def test_posted_ones_free_room_and_their_hosted_copy_is_deleted(env):
    B.test()
    B.send(_picks(10), wait=True)
    assert B.room(fresh=True)["room"]["tiktok"] == 0
    for p in env.net.posts.values():
        p.update(status="sent", sentAt="2026-11-19T10:00:00Z")
    later = datetime.now(timezone.utc) + timedelta(days=3)  # the next time the app is opened
    B.tick(now=later)
    B.tick(now=later + timedelta(hours=1))
    v = B.view()
    assert all(v["platforms"][p]["sent"] == 10 and v["platforms"][p]["scheduled"] == 0 for p in B.PLATFORMS)
    assert len(env.net.destroyed) == 10
    assert B.room(fresh=True)["room"]["tiktok"] == 10
    B.send(_picks(3, "p2"), wait=True)  # the next 10 can go now
    assert B.view()["platforms"]["tiktok"]["scheduled"] == 3


def test_next_pick_continues_after_the_posts_in_buffer_and_a_start_time_is_kept(env):
    B.test()
    B.send(_picks(4), wait=True)
    last = max(i["due_at"] for i in _items("instagram"))
    B.send(_picks(3, "p2"), wait=True)
    new = [i for i in _items("instagram") if i["project_id"] == "p2"]
    assert len(new) == 3 and all(i["due_at"] > last for i in new)
    start = (datetime.now() + timedelta(days=20)).replace(hour=12, minute=0, second=0, microsecond=0)
    B.send([{"project_id": "p2", "cid": "c10"}], start=start.isoformat(), wait=True)
    it = next(i for i in _items("instagram") if i["cid"] == "c10" and i["project_id"] == "p2")
    assert B._parse(it["due_at"]) >= start.astimezone()


def test_cancel_deletes_the_buffer_post_and_send_again_works(env):
    B.test()
    B.send(_picks(1), wait=True)
    it = next(i for i in _items("tiktok") if i["state"] == "scheduled")
    B.cancel(it["id"])
    assert env.net.deleted == [it["post_id"]]
    assert next(i for i in _items("tiktok") if i["id"] == it["id"])["state"] == "cancelled"
    B.retry(it["id"])
    assert [i["state"] for i in _items("tiktok")] == ["scheduled"]


def test_a_refused_key_stops_the_send_with_a_clear_reason(env):
    B.test()
    env.net.auth_ok = False
    r = B.send(_picks(3), wait=True)
    sd = B.sending()
    assert sd["sent"] == 0 and sd["failed"] == r["count"] and "refused" in sd["message"]
    assert env.net.created == []  # stopped at the first refusal, no hammering


def test_a_failed_send_is_shown_not_retried_in_the_background(env):
    B.test()
    env.net.limit = 2  # Buffer refuses more than 2 (e.g. posts made elsewhere at the same time)
    B.send(_picks(4), wait=True)
    tt = _items("tiktok")
    assert [i["state"] for i in tt].count("scheduled") == 2 and [i["state"] for i in tt].count("error") == 2
    assert "full" in next(i for i in tt if i["state"] == "error")["error"]
    created = len(env.net.created)
    for _ in range(3):
        B.tick()
    assert len(env.net.created) == created



# ===================================================================== the posting plan


def _sent(net, ids):
    for pid in ids:
        net.posts[pid].update(status="sent", sentAt="2026-11-19T10:00:00Z")


def _items(platform):
    return sorted((i for i in B.load_state()["items"] if i["platform"] == platform), key=lambda i: i["due_at"])


def test_buffer_calls_match_buffers_real_schema():
    graphql = pytest.importorskip("graphql")
    from pathlib import Path

    schema = graphql.build_schema((Path(__file__).parent / "data" / "buffer_schema.graphql").read_text(encoding="utf-8"))
    for q in (B.Q_ACCOUNT, B.Q_CHANNELS, B.M_CREATE, B.M_DELETE, B.M_EDIT, B.Q_POST, B.Q_SCHEDULED):
        assert graphql.validate(schema, graphql.parse(q)) == [], q
    s = B.settings()
    s["instagram"]["first_comment"] = "#more"
    for p in B.PLATFORMS:
        inp = B._input({"platform": p, "caption": "Speed went ALL IN 🔥 #gta6", "title": "Speed <went> ALL IN",
                        "peak_ms": 30000, "due_at": "2026-11-20T10:00:00.000Z"}, "https://x.test/v.mp4", s, "ch1")
        errs = []
        graphql.coerce_input_value(inp, schema.get_type("CreatePostInput"), lambda path, v, e: errs.append(e.message))
        assert errs == [], (p, errs)
    errs = []
    graphql.coerce_input_value({"id": "p1", "dueAt": "2026-11-20T10:00:00.000Z", "mode": "customScheduled"},
                               schema.get_type("EditPostInput"), lambda path, v, e: errs.append(e.message))
    assert errs == []


def test_interval_mode_every_hour_then_the_next_day():
    B.save_settings({"mode": "interval", "start_time": "10:00", "gap_min": 60, "per_day": {"tiktok": 10}})
    s = B.settings()
    assert B.example_day(s, "tiktok") == [f"{h}:00" for h in range(10, 20)]
    now = datetime(2026, 11, 20, 0, 0).astimezone().astimezone(timezone.utc)
    got = B.next_slots({"items": []}, "tiktok", 12, now=now, s=s)
    assert [x.astimezone().strftime("%d %H:%M") for x in got[9:]] == ["20 19:00", "21 10:00", "21 11:00"]
    B.save_settings({"gap_min": 120, "per_day": {"tiktok": 10}})  # 10 x 2 h from 10:00: 7 fit before midnight
    assert B.example_day(None, "tiktok") == ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]
    config.update(buffer={})


def test_moving_a_post_in_buffer_edits_it_there(env):
    B.test()
    B.send(_picks(1), wait=True)
    it = next(i for i in _items("youtube") if i["state"] == "scheduled")
    when = (datetime.now() + timedelta(days=2)).replace(hour=20, minute=30, second=0, microsecond=0)
    B.set_time(it["id"], when.isoformat())
    assert env.net.edited == [{"id": it["post_id"], "dueAt": B._iso(when.astimezone()), "mode": "customScheduled"}]
    with pytest.raises(SharkyClipsError):
        B.set_time(it["id"], datetime.now().isoformat())  # not in the past / too soon


def test_preview_tells_how_long_a_pick_lasts():
    B.save_settings({"per_day": {"instagram": 10, "tiktok": 5, "youtube": 10}})
    pv = B.preview(30, ["instagram", "tiktok"])
    assert pv["instagram"]["days"] in (3, 4) and pv["tiktok"]["days"] in (6, 7)
    config.update(buffer={})


def test_sent_clips_are_marked_in_the_picker_and_failed_ones_stay_pickable(env):
    for proj in B._app().list_projects():
        (proj.dir / "clips.json").write_text("{}")  # the picker only looks at projects with rendered clips
    B.test()
    env.net.limit = 1  # Buffer takes one per channel, the second clip fails
    B.send(_picks(2), wait=True)
    marks = {(c["project_id"], c["cid"]): c["queued"] for c in B.picker()}
    assert marks[("p1", "c01")] == {"instagram": "scheduled", "tiktok": "scheduled", "youtube": "scheduled"}
    assert set(marks[("p1", "c02")].values()) == {"error"}  # shown, and it can be sent again
    assert marks[("p2", "c01")] == {}
    env.net.limit = 10
    B.send([{"project_id": "p1", "cid": "c02"}], wait=True)
    marks = {(c["project_id"], c["cid"]): c["queued"] for c in B.picker()}
    assert set(marks[("p1", "c02")].values()) == {"scheduled"}
    with pytest.raises(SharkyClipsError, match="already"):
        B.send([{"project_id": "p1", "cid": "c01"}], wait=True)  # never twice


def test_the_ai_package_description_and_hashtags_join_the_caption(env):
    proj = B._app().get_project("p1")
    cand = {"id": "c01", "story": {"headline": "Speed was FURIOUS in GTA 6 😡"},
            "publish": {"titles": ["x"], "hashtags": ["#gta6", "#ishowspeedclips", "#funny"],
                        "description": "Speed finds tigers in cages and loses it.\n\n#gta6 #rage"}}
    t = B.default_texts(proj, cand)
    assert t["title"] == "Speed was FURIOUS in GTA 6 😡"
    head, body, tags = t["caption"].split("\n\n")
    assert body == "Speed finds tigers in cages and loses it."
    assert tags.split()[:4] == ["#ishowspeed", "#gta6", "#ishowspeedclips", "#funny"] and "#rage" in tags
    assert len(tags.split()) == len({x.lower() for x in tags.split()})  # no duplicates


def test_test_reads_buffers_queue_limit_and_daily_limits(env):
    B.test()
    assert B.settings()["queue_cap"] == 10  # free plan
    env.net.plan_limit = 5000  # after upgrading to a paid plan
    B.test()
    assert B.settings()["queue_cap"] == B.SEND_MAX


# ===================================================================== Cloudflare R2 hosting (1.2.57)


class FakeR2:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.public_ok = True

    def request(self, method, url, data=None, headers=None, timeout=None):
        assert timeout and url.startswith("https://" + "0" * 31 + "a.r2.cloudflarestorage.com/sharkyclips/")
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=r2key/")
        assert "/auto/s3/aws4_request" in headers["Authorization"]
        key = url.split("/sharkyclips/", 1)[1]
        self.calls.append((method, key))
        if method == "PUT":
            body = data if isinstance(data, bytes) else data.read()
            assert headers["x-amz-content-sha256"] == __import__("hashlib").sha256(body).hexdigest()
            self.objects[key] = body
            return Resp(200, {})
        if method == "DELETE":
            self.objects.pop(key, None)
            return Resp(204, {})
        raise AssertionError(method)

    def get(self, url, timeout=None):
        key = url.split("r2.dev/", 1)[1]
        r = Resp(200 if self.public_ok and key in self.objects else 404, {})
        r.content = self.objects.get(key, b"")
        return r


R2_KEYS = {"account_id": "0" * 31 + "a", "access_key_id": "r2key", "secret_access_key": "r2secret",
           "bucket": "sharkyclips", "public_url": "https://pub-abc.r2.dev/"}


@pytest.fixture
def r2(env, monkeypatch):
    from sharkyclips import r2store

    fake = FakeR2()
    monkeypatch.setattr(r2store, "_http", lambda: fake)
    B.save_keys({"r2": R2_KEYS})
    return fake


def test_r2_signature_matches_the_aws_example():
    from sharkyclips import r2store as R

    h = R.sign("GET", "examplebucket.s3.amazonaws.com", "/test.txt", {}, {"Range": "bytes=0-9"}, R.EMPTY_SHA,
               "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", region="us-east-1",
               now=datetime(2013, 5, 24, tzinfo=timezone.utc))
    assert h["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_r2_is_preferred_and_buffer_gets_its_public_link(env, r2):
    v = B.keys_view()
    assert v["host"] == "r2" and v["has_r2"] and "r2secret" not in json.dumps(v)
    B.test()
    B.send(_picks(2), wait=True)
    assert env.net.uploads == []  # nothing to Cloudinary
    puts = [k for m, k in r2.calls if m == "PUT" and k.endswith(".mp4")]
    assert len(puts) == 2 and all(re.fullmatch(r"sc/[0-9a-f]{32}\.mp4", k) for k in puts)  # unguessable names
    urls = {c["assets"][0]["video"]["url"] for c in env.net.created}
    assert urls == {f"https://pub-abc.r2.dev/{k}" for k in puts}
    for p in env.net.posts.values():
        p.update(status="sent", sentAt="2026-11-19T10:00:00Z")
    later = datetime.now(timezone.utc) + timedelta(days=2)
    B.tick(now=later)
    B.tick(now=later + timedelta(hours=1))
    assert sorted(k for m, k in r2.calls if m == "DELETE" and k.endswith(".mp4")) == sorted(puts) and r2.objects == {}


def test_r2_test_checks_upload_public_link_and_cleans_up(env, r2):
    r = B.test()
    assert r["hosting"]["ok"] and r["hosting"]["host"] == "r2" and r["ok"]
    assert r2.objects == {}  # the test file is deleted again
    r2.public_ok = False
    r = B.test()
    assert not r["hosting"]["ok"] and "Public Development URL" in r["hosting"]["hint"]


def test_r2_keys_are_checked():
    with pytest.raises(SharkyClipsError, match="Account ID"):
        B.save_keys({"r2": {**R2_KEYS, "account_id": "nope"}})
    with pytest.raises(SharkyClipsError, match="public address"):
        B.save_keys({"r2": {**R2_KEYS, "public_url": "pub-abc.r2.dev"}})


def test_the_games_own_hashtags_and_no_fixed_gta_tags(env):
    proj = B._app().get_project("p1")
    mc = B.default_texts(proj, {"story": {"headline": "Speed got TERRIFIED by the Warden in Minecraft 😭", "game": "Minecraft"}})
    tags = mc["caption"].split("\n\n")[-1].split()
    assert tags[:2] == ["#ishowspeed", "#minecraft"] and "#gta6" not in tags and "#gaming" in tags
    gta = B.default_texts(proj, {"story": {"headline": "Speed went ALL IN in GTA 6 🔥", "game": "GTA 6"}})
    assert "#gta6" in gta["caption"] and "#gta" in gta["caption"].split()
    none = B.default_texts(proj, {"story": {"headline": "Speed laughs at chat 😂"}})
    assert none["caption"].split("\n\n")[-1].split() == ["#ishowspeed", "#gaming", "#streamer", "#fyp"]
