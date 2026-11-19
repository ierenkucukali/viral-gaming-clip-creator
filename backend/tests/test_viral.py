"""Local fork: the viral score (viral.py) - story, sound energy, hook, words, length."""

from __future__ import annotations

from sharkyclips import viral


def _cand(start, end, peak, score, reasons=()):
    return {"start": start, "end": end, "peak": peak, "story": {"score": score}, "reasons": list(reasons)}


def test_a_loud_early_peak_beats_a_calm_talk():
    curve = [0.05] * 600
    for t in range(102, 160):  # 1:42 - 2:40: long shouting
        curve[t] = 0.55
    loud = viral.compute(_cand(100, 160, 104, 78, [{"signal": "loudness"}, {"code": "shout"}]), curve,
                         "NO NO NO what the hell bro no way! let's go!", 1.0)
    calm = viral.compute(_cand(300, 360, 350, 78), curve, "so I think the yacht is for celebrities", 1.0)
    assert loud["score"] > calm["score"] + 15
    assert loud["parts"]["energy"] > 70 and calm["parts"]["energy"] < 30
    assert loud["parts"]["hook"] > calm["parts"]["hook"] and loud["parts"]["words"] > calm["parts"]["words"]
    assert set(loud["parts"]) == {"story", "energy", "hook", "words", "length"} and 0 <= loud["score"] <= 99


def test_labels_and_length_rule():
    assert [viral.label(x) for x in (85, 70, 55, 30)] == ["very_high", "high", "medium", "low"]
    assert viral._length(60) == 1.0 and viral._length(20) < 1.0 and viral._length(180) < 1.0
    assert viral._length(180) >= 0.6


def test_clip_text_takes_the_clip_window():
    tr = {"segments": [{"start": 0, "end": 5, "text": "before"}, {"start": 10, "end": 15, "text": "inside"},
                       {"start": 30, "end": 35, "text": "after"}]}
    assert viral.clip_text(tr, 8, 20) == "inside"
