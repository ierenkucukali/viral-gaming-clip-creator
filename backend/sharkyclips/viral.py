"""Viral score of a clip, 0-99 (local fork, like OpusClip's: Hook / Flow / Value / Trend parts).

No extra AI call: it combines what the analysis already measured.

  story   40 %  the AI's story score (storyfind): setup -> peak -> payoff, how much it makes viewers
                want to see the end, whether a stranger gets it on its own (OpusClip: Flow + Value)
  energy  25 %  the sound: how hard the peak hits (hype curve), how long it stays loud (long shouting
                / panic), laughs / shouts / screams heard in the clip
  hook    15 %  how fast it gets going: the peak early in the clip, action in the first 5 seconds
  words   12 %  hype words ("no way", "what the", "bro"...), repeats ("NO NO NO"), fast speech,
                a burst after silence, exclamations
  length   8 %  30-90 s is the sweet spot for Reels / TikTok / Shorts; 20 s or 3 min score a bit lower

Labels: 80+ very high, 65+ high, 50+ medium, below 50 low.
"""

from __future__ import annotations

import re

WEIGHTS = {"story": 0.40, "energy": 0.25, "hook": 0.15, "words": 0.12, "length": 0.08}
HYPE = ("no way", "what the", "oh my god", "oh my gosh", "let's go", "lets go", "bro", "dude", "what",
        "crazy", "insane", "wtf", "yo", "help", "run", "stop", "no no", "yes yes", "holy", "oh no")
_WORD = re.compile(r"[A-Za-z']+")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def label(score: int) -> str:
    return "very_high" if score >= 80 else "high" if score >= 65 else "medium" if score >= 50 else "low"


def _length(d: float) -> float:
    if d < 30:
        return _clamp(0.7 + (d - 20) * 0.03, 0.4, 1.0)
    if d <= 90:
        return 1.0
    return _clamp(1.0 - (d - 90) * 0.0033, 0.6, 1.0)


def compute(cand: dict, curve: list[float] | None, text: str = "", res: float = 1.0) -> dict:
    """{"score": 0-99, "label", "parts": {story, energy, hook, words, length} 0-100}."""
    s, e = float(cand.get("start") or 0), float(cand.get("end") or 0)
    d = max(1.0, e - s)
    story = float((cand.get("story") or {}).get("score") or cand.get("hype_score") or 50) / 100

    # --- energy: the hype curve inside the clip (0..1 a second), and what the sound analysis heard
    vals = []
    if curve:
        a, b = int(s / res), max(int(s / res) + 1, int(e / res))
        vals = [float(v) for v in curve[a:b]]
    srt = sorted(curve or [0.0])
    p90 = srt[int(0.9 * (len(srt) - 1))] if srt else 0.0
    peak = max(vals) if vals else 0.0
    mean = sum(vals) / len(vals) if vals else 0.0
    sustain = sum(1 for v in vals if v >= max(0.2, p90 * 0.8)) / len(vals) if vals else 0.0
    heard = sum(1 for r in cand.get("reasons") or [] if r.get("signal") in ("loudness", "laughter")
                or r.get("code") in ("laugh", "shout", "scream", "cheer", "loud"))
    audio_hype = float((cand.get("story") or {}).get("audio_hype") or 0) / 100
    energy = _clamp(0.35 * _clamp(peak / 0.4) + 0.25 * _clamp(sustain / 0.35) + 0.15 * _clamp(mean / max(p90, 0.05))
                    + 0.15 * audio_hype + 0.10 * _clamp(heard / 3))

    # --- hook: the peak early, action in the first 5 seconds
    pk = float(cand.get("peak") or (s + d / 2))
    pos = _clamp((pk - s) / d)
    early = vals[: max(1, int(5 / res))] if vals else []
    early_mean = sum(early) / len(early) if early else 0.0
    hook = _clamp(0.55 * (1.0 if pos <= 0.35 else 1.0 - (pos - 0.35) / 0.65 * 0.7)
                  + 0.45 * _clamp(early_mean / max(p90 * 0.8, 0.05)))

    # --- words: hype words, repeats, exclamations, the speech signals
    low = text.lower()
    words = _WORD.findall(low)
    per_min = 60.0 / d
    hits = sum(low.count(h) for h in HYPE) * per_min
    repeats = sum(1 for x, y in zip(words, words[1:]) if x == y and len(x) > 1) * per_min
    excl = text.count("!") * per_min
    speech = sum(float(r.get("strength") or 0) for r in cand.get("reasons") or []
                 if r.get("signal") in ("keywords", "speech"))
    wscore = _clamp(0.35 * _clamp(hits / 6) + 0.2 * _clamp(repeats / 3) + 0.15 * _clamp(excl / 4)
                    + 0.3 * _clamp(speech / 2.5))

    parts = {"story": story, "energy": energy, "hook": hook, "words": wscore, "length": _length(d)}
    score = sum(WEIGHTS[k] * v for k, v in parts.items())
    if (cand.get("llm") or {}).get("self_contained") is False:
        score *= 0.9
    score = int(round(_clamp(score) * 99))
    return {"score": score, "label": label(score), "parts": {k: int(round(v * 100)) for k, v in parts.items()}}


def clip_text(transcript: dict | None, start: float, end: float) -> str:
    out = []
    for seg in (transcript or {}).get("segments") or []:
        if seg.get("end", 0) < start or seg.get("start", 0) > end:
            continue
        out.append(str(seg.get("text") or ""))
    return " ".join(out)
