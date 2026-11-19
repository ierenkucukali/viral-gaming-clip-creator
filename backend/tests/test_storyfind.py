"""Local fork: story finder (storyfind.py) and the cloud AI layer (llmcloud.py)."""

from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from sharkyclips import config, llmcloud, storyfind
from sharkyclips.errors import SharkyClipsError


def _lines(n: int, step: float = 6.0) -> list[dict]:
    return [{"start": i * step, "end": i * step + step - 0.5, "text": f"line {i}."} for i in range(n)]


def _transcript(n: int, step: float = 6.0) -> dict:
    words = []
    for i in range(n):
        t = i * step
        words += [{"word": " line", "start": t, "end": t + 2.0}, {"word": f" {i}.", "start": t + 2.0, "end": t + step - 0.5}]
    return {"language": "en", "segments": [{"start": 0.0, "end": n * step, "text": "", "words": words}]}


def test_local_windows_overlap_and_cover_everything():
    lines = _lines(400)  # 40 minutes
    parts = storyfind.windows(lines, cloud=False)
    assert parts[0][0] == 0 and parts[-1][1] == len(lines)
    for (a1, b1), (a2, _b2) in zip(parts, parts[1:]):
        assert a2 < b1, "windows must overlap so no story is cut in half"
        assert a2 > a1
    assert all(lines[b - 1]["end"] - lines[a]["start"] <= storyfind.LOCAL_WINDOW_S + 6 for a, b in parts)


def test_cloud_reads_up_to_an_hour_per_request():
    lines = _lines(600)  # 1 hour of short lines: one request
    assert storyfind.windows(lines, cloud=True) == [(0, 600)]
    parts = storyfind.windows(_lines(3000), cloud=True)  # 5 hours: about one request an hour, overlapping
    assert 5 <= len(parts) <= 7 and parts[0][0] == 0 and parts[-1][1] == 3000
    assert all(b > a2 for (_a, b), (a2, _b) in zip(parts, parts[1:]))


def test_story_count_is_automatic_by_default():
    st = storyfind.settings()
    assert st["auto"] and st["count"] == storyfind.AUTO_MAX and st["per_hour"] == 30
    assert storyfind.want_for(60, True, st) == 30  # a 1-hour request may list up to 30 stories
    assert storyfind.want_for(600, True, st) == 60
    assert storyfind.want_for(10, False, st) == 5  # local 10-minute windows


def test_fit_keeps_the_peak_and_the_length_limits():
    lines = _lines(100)
    st = {"min": 45.0, "max": 120.0, "ideal": 85.0}
    # far too long (L0..L60 = 6 minutes): trimmed to <= 120 s around the peak
    s, e = storyfind._fit(lines, 0, 40, 60, st, 600.0)
    assert e - s <= 120.0 and s <= lines[40]["start"] <= e
    # far too short (one line): extended with setup first
    s, e = storyfind._fit(lines, 50, 50, 50, st, 600.0)
    assert 45.0 <= e - s <= 120.0 and s < lines[50]["start"]


class FakeModel:
    model = "fake"

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def json(self, system, prompt, schema, **kw):
        self.prompts.append(prompt)
        d = self.answers.pop(0)
        hard = kw.get("check")
        assert not hard(d), hard(d)
        return d


class FakeProject:
    def __init__(self, tmp_path, n):
        self.dir = tmp_path
        self._t = _transcript(n)

    def load_meta(self):
        return {"title": "GTA RP stream", "duration": 600.0, "source": {"uploader": "someone"}}

    def load_transcript(self):
        return self._t


def _moment(s, p, e, score, headline="Cops had zero chance 🚔"):
    return {"start_line": s, "peak_line": p, "end_line": e, "type": "funny", "score": score, "headline": headline,
            "title": "The worst getaway ever", "why": "He crashes into the only cop car in town."}


def test_find_returns_ranked_non_overlapping_stories(tmp_path, monkeypatch):
    monkeypatch.setattr(storyfind, "settings", lambda: {"enabled": True, "min": 45.0, "max": 120.0,
                                                        "ideal": 85.0, "count": 15, "per_hour": 30,
                                                        "min_score": 50})
    proj = FakeProject(tmp_path, 100)
    result = {"duration": 600.0, "curve": {"values": [0.1] * 600}, "candidates": [
        {"reasons": [{"code": "laughter", "t": 130.0, "strength": 0.9}]}]}
    model = FakeModel([{"game": "GTA 6", "moments": [_moment(10, 20, 30, 70), _moment(12, 22, 31, 60),  # overlaps the first
                                    _moment(60, 70, 80, 85, "He said WHAT 😭"),
                                    _moment(40, 41, 42, 55, "Damn this is sh*t 💩")]}])
    out = storyfind.find(proj, result, model)
    assert [c["story"]["score"] for c in out] == [85, 70, 55]
    assert all(45.0 <= c["duration"] <= 120.0 for c in out)
    assert out[0]["story"]["headline"] == "He said WHAT 😭"
    assert out[0]["reasons"][0]["code"] == "llm_story"
    assert {c["story"]["game"] for c in out} == {"GTA 6"}  # the game the AI named, on every story
    assert "FIRST: name the game being played" in model.prompts[0] and "names the game when it is known" in model.prompts[0]
    assert "[laugh]" in model.prompts[0] or "laugh" in model.prompts[0]  # signals are written next to the lines
    assert "Whole video" not in model.prompts[0] or "L0" in model.prompts[0]


def test_bad_line_numbers_are_rejected_for_a_retry():
    hard, soft = storyfind._checker(0, 50, _lines(50), {"min": 45.0, "max": 120.0, "ideal": 85.0})
    assert hard({"moments": [_moment(10, 5, 20, 50)]})  # peak before start
    assert hard({"moments": [_moment(10, 20, 80, 50)]})  # outside the window
    assert not hard({"moments": [_moment(10, 15, 20, 50)]})
    assert soft({"moments": [_moment(10, 10, 11, 50)]})  # 12 s: too short


def _ai_on(monkeypatch):
    orig = config.get
    monkeypatch.setattr(config, "get", lambda k: True if k == "llm_enabled" else orig(k))


def test_apply_replaces_signal_candidates_and_keeps_strong_extras(tmp_path, monkeypatch):
    from sharkyclips import llm

    _ai_on(monkeypatch)

    proj = FakeProject(tmp_path, 100)
    result = {"duration": 600.0, "curve": {"values": [0.1] * 600}, "candidates": [
        {"id": "c01", "rank": 1, "start": 100.0, "end": 130.0, "hype_score": 90, "quality": "good", "reasons": []},
        {"id": "c02", "rank": 2, "start": 400.0, "end": 430.0, "hype_score": 80, "quality": "good", "reasons": []}]}

    @contextmanager
    def fake_session(*a, **kw):
        yield FakeModel([{"moments": [_moment(15, 20, 25, 80)]}])

    monkeypatch.setattr(llm, "session", fake_session)
    monkeypatch.setattr(llmcloud, "active", lambda: llmcloud.Cloud("anthropic", "claude-opus-5-5", "k" * 30))
    monkeypatch.setattr(storyfind, "settings", lambda: {"enabled": True, "min": 45.0, "max": 120.0,
                                                        "ideal": 85.0, "count": 15, "per_hour": 30,
                                                        "min_score": 50})
    assert storyfind.apply(proj, result)
    cands = result["candidates"]
    assert cands[0]["story"]["score"] == 80 and cands[0]["id"] == "c01"
    starts = [c["start"] for c in cands]
    assert 400.0 in starts  # strong signal moment the AI didn't cover stays (lower)
    assert 100.0 not in starts  # covered by the story


def test_apply_keeps_signal_candidates_when_the_ai_fails(tmp_path, monkeypatch):
    from sharkyclips import llm

    @contextmanager
    def broken(*a, **kw):
        raise SharkyClipsError("no model")
        yield

    monkeypatch.setattr(llm, "session", broken)
    monkeypatch.setattr(llmcloud, "active", lambda: llmcloud.Cloud("gemini", "auto", "k" * 30))
    result = {"duration": 600.0, "candidates": [{"id": "c01", "start": 1.0, "end": 20.0}]}
    assert not storyfind.apply(FakeProject(tmp_path, 10), result)
    assert result["candidates"] == [{"id": "c01", "start": 1.0, "end": 20.0}]


# ------------------------------------------------------------------ llmcloud


def test_provider_schema_drops_unsupported_constraints():
    s = llmcloud.provider_schema(storyfind.schema(10))
    txt = json.dumps(s)
    for k in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        assert f'"{k}"' not in txt
    assert s["additionalProperties"] is False
    assert s["properties"]["moments"]["items"]["additionalProperties"] is False


def test_keys_are_stored_encrypted_and_never_shown(tmp_path, monkeypatch):
    _ai_on(monkeypatch)
    key = "sk-ant-api03-" + "x" * 40
    llmcloud.save_key("anthropic", key)
    assert llmcloud.has_key("anthropic")
    raw = (config.config_dir() / llmcloud.KEY_FILE).read_bytes()
    assert key.encode() not in raw
    view = json.dumps(llmcloud.settings_view())
    assert key not in view and "xxxx" in view  # masked hint only
    assert llmcloud.active() is None  # provider still "ollama" until the user picks it
    config.update(llm_provider="anthropic")
    try:
        c = llmcloud.active()
        assert c is not None and c.provider == "anthropic" and c.model == "claude-opus-5-5"
    finally:
        config.update(llm_provider="ollama")
        llmcloud.save_key("anthropic", "")
    assert not llmcloud.has_key("anthropic")
    with pytest.raises(SharkyClipsError):
        llmcloud.save_key("anthropic", "short")


def test_cloud_quota_falls_back_to_the_local_model(monkeypatch):
    from contextlib import ExitStack

    from sharkyclips import llm

    class Local:
        model = "qwen3.5:9b"

        def _send(self, messages, schema, temperature, num_predict):
            return json.dumps({"headline": "Local model answered 🙂"})

    @contextmanager
    def local_session(*a, **kw):
        assert kw.get("local") is True
        yield Local()

    monkeypatch.setattr(llm, "session", local_session)
    with ExitStack() as stack:
        c = llmcloud.CloudLLM(llmcloud.Cloud("gemini", "gemini-3.6-flash", "k" * 30), stack)

        def quota(*a, **kw):
            raise llmcloud.CloudQuotaError("The Gemini free daily limit is used up.")

        monkeypatch.setattr(c, "_gemini", quota)
        schema = {"type": "object", "properties": {"headline": {"type": "string"}}, "required": ["headline"]}
        assert c.json("sys", "prompt", schema)["headline"] == "Local model answered 🙂"
        assert "qwen3.5:9b" in c.model
        # the provider test never falls back: it must reach the provider itself
        t = llmcloud.CloudLLM(llmcloud.Cloud("gemini", "x", "k" * 30), stack, allow_fallback=False)
        monkeypatch.setattr(t, "_gemini", quota)
        with pytest.raises(llmcloud.CloudQuotaError):
            t.json("sys", "prompt", schema)


def test_gemini_daily_quota_is_recognised():
    class R:
        def json(self):
            return {"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}

    wait, daily = llmcloud._gemini_retry(R())
    assert daily and 17 <= wait <= 19


def test_auto_length_lets_the_ai_decide_within_20_s_to_3_min(monkeypatch):
    from sharkyclips import config as C

    real = C.get
    monkeypatch.setattr(C, "get", lambda k: {"mode_tuning": {"gaming": {"clip_min": 20, "clip_ideal": 55, "clip_max": 180,
                                                                         "pre_roll": [8, 20], "min_gap": 60}}}.get(k, real(k)))
    st = storyfind.settings()
    assert st["min"] == 20 and st["max"] == 180 and st["auto_length"] is True
    rule = storyfind._length_rule(st)
    assert "as long as THIS story needs" in rule and "20 to 180 seconds" in rule
    lines = _lines(100)  # a 3-minute story (L0..L30, 6 s a line) stays whole: no 1.6x-target cap
    s, e = storyfind._fit(lines, 0, 20, 29, {**st, "max": st["max"]}, 600.0)
    assert 170 <= e - s <= 180
