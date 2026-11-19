"""Story clips (local fork): the AI reads the WHOLE transcript and picks complete stories.

The signal pipeline (loudness, laughter, shouting, chat, keywords) finds *moments*; a moment alone
is often not a good clip ("why is he screaming?"). Here the language model reads the transcript as
numbered lines, with the signals written next to the lines they belong to, and returns stories:

    setup (where a viewer who knows nothing has to start) -> build-up -> peak -> payoff/reaction

Each story gets a start/end line, its peak, a type, an honest 0-100 score, a one-sentence "why",
and a short Instagram-style headline (3-6 English words + one emoji) for the on-screen banner.

Long videos: with a cloud model (1M context) the whole transcript goes in one request (a 5-hour
stream is ~100k tokens); with the local model it is read in overlapping windows. Stories are then
snapped to word boundaries, kept between clip_min..clip_max seconds (story_min / story_max), and
de-duplicated. If the AI is off or fails, the signal candidates stay as they are.
"""

from __future__ import annotations

import time

from . import config
from .project import Project
from .textutil import mmss, sentences
from .ui import console

STORY_VERSION = 2  # 2: viral caption headlines (8-16 words, streamer named)
TYPES = ["funny", "exciting", "fail", "skill", "emotional", "wholesome", "rage", "other"]
LOCAL_WINDOW_S = 600.0  # local model: 10 minutes of transcript per request
LOCAL_OVERLAP_S = 120.0
CLOUD_WINDOW_CHARS = 380_000  # ~95k tokens; above that the transcript is split into parts
CLOUD_OVERLAP_S = 300.0
TAIL_S = 0.8  # after the last word of the payoff (the laugh / "NOOO" keeps going a moment)

SYSTEM = (
    "You are a senior short-form video editor. You cut long livestream VODs (mostly GTA and other "
    "games) into vertical story clips for TikTok, Instagram Reels and YouTube Shorts. You understand "
    "humor, banter, roleplay, chases, fails and reactions, and you know a clip only works when a "
    "stranger understands it without the rest of the stream. Be strict and honest: most stretches of a "
    "stream are not clip-worthy. Base everything only on the transcript and the tags; never invent "
    "events. Headlines must be advertiser-friendly (no swearing). Answer only with JSON."
)


def settings() -> dict:
    """Story length = the gaming clip shape (Gaming page: shortest / target / longest);
    how many = candidate_count (empty: story_count, default 15)."""
    from .highlights.modes import get_mode

    p = get_mode("gaming")
    lo = min(max(float(p.clip_min), 15.0), 170.0)
    hi = max(lo + 10.0, min(float(p.clip_max), 180.0))
    try:
        count = int(config.get("candidate_count") or config.get("story_count") or 15)
    except (TypeError, ValueError):
        count = 15
    return {"enabled": config.get("story_clips") is not False, "min": lo, "max": hi,
            "ideal": min(hi, max(lo, float(p.clip_ideal))), "count": min(max(count, 1), 60)}


def schema(max_items: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "moments": {
                "type": "array", "minItems": 0, "maxItems": max_items,
                "items": {
                    "type": "object",
                    "properties": {
                        "start_line": {"type": "integer", "minimum": 0},
                        "peak_line": {"type": "integer", "minimum": 0},
                        "end_line": {"type": "integer", "minimum": 0},
                        "type": {"type": "string", "enum": TYPES},
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "headline": {"type": "string", "minLength": 20, "maxLength": 110},
                        "title": {"type": "string", "minLength": 8, "maxLength": 90},
                        "why": {"type": "string", "minLength": 10, "maxLength": 300},
                    },
                    "required": ["start_line", "peak_line", "end_line", "type", "score", "headline", "title", "why"],
                },
            },
        },
        "required": ["moments"],
    }


# ------------------------------------------------------------------ the annotated transcript


def _tags_by_line(lines: list[dict], result: dict) -> list[list[str]]:
    """Signal notes per line: [HYPE n] from the hype curve, plus the signal reasons."""
    tags: list[list[str]] = [[] for _ in lines]
    curve = ((result.get("curve") or {}).get("values")) or []
    top = max(curve) if curve else 0.0
    starts = [x["start"] for x in lines]

    def line_at(t: float) -> int | None:
        import bisect

        i = bisect.bisect_right(starts, t) - 1
        if i < 0:
            return None
        if t <= lines[i]["end"] + 2.0:
            return i
        return i if i + 1 >= len(lines) else (i if t - lines[i]["end"] < lines[i + 1]["start"] - t else i + 1)

    if top > 0:
        for i, x in enumerate(lines):
            a, b = int(x["start"]), int(x["end"]) + 1
            seg = curve[a:b] or curve[max(0, a - 1):a + 1]
            h = int(round(100 * max(seg) / top)) if seg else 0
            if h >= 55:
                tags[i].append(f"HYPE {h}")
    seen: set[tuple[int, str]] = set()
    for c in result.get("candidates") or []:
        for r in c.get("reasons") or []:
            t = r.get("t")
            if t is None:
                continue
            i = line_at(float(t))
            if i is None:
                continue
            code = str(r.get("code") or "")
            if code == "loudness_spike":
                v = r.get("value")
                tag = f"loud +{int(float(v))} dB" if isinstance(v, (int, float)) else "loud"
            elif code in ("laughter", "shouting", "cheering", "scream"):
                tag = {"laughter": "laugh", "shouting": "shout", "cheering": "cheer", "scream": "scream"}[code]
            elif code in ("chat_spike", "chat_reactions"):
                tag = "chat spike"
            elif code == "marker":
                tag = "marked by streamer"
            elif code.startswith(("df_", "lol_")):
                tag = code
            else:
                continue
            if (i, tag) not in seen:
                seen.add((i, tag))
                tags[i].append(tag)
    return tags


def annotated(lines: list[dict], tags: list[list[str]], a: int, b: int) -> str:
    out = []
    for i in range(a, b):
        x = lines[i]
        t = f" [{', '.join(tags[i])}]" if tags[i] else ""
        out.append(f"L{i} [{mmss(x['start'])}] {x['text']}{t}")
    return "\n".join(out)


def windows(lines: list[dict], cloud: bool) -> list[tuple[int, int]]:
    """[(first line, end line)] parts of the transcript, overlapping."""
    if not lines:
        return []
    if cloud:
        total = sum(len(x["text"]) + 24 for x in lines)
        if total <= CLOUD_WINDOW_CHARS:
            return [(0, len(lines))]
        span = (lines[-1]["end"] - lines[0]["start"]) * CLOUD_WINDOW_CHARS / total
        overlap = CLOUD_OVERLAP_S
    else:
        span, overlap = LOCAL_WINDOW_S, LOCAL_OVERLAP_S
    out: list[tuple[int, int]] = []
    a = 0
    while a < len(lines):
        t_end = lines[a]["start"] + span
        b = a
        while b < len(lines) and lines[b]["start"] < t_end:
            b += 1
        b = max(b, a + 1)
        out.append((a, b))
        if b >= len(lines):
            break
        t_next = lines[b - 1]["end"] - overlap
        na = a
        while na < b and lines[na]["start"] < t_next:
            na += 1
        a = max(a + 1, na)
    return out


def _prompt(meta: dict, text: str, a: int, b: int, st: dict, want: int, part: str) -> str:
    src = meta.get("source") or {}
    return f"""Video: {meta.get('title', '')}
Channel: {src.get('uploader') or 'unknown'} (use the streamer's short, well-known name in the headline)
{part}
The transcript is numbered by line (L12) with each line's start time [mm:ss or h:mm:ss]. Tags after a line:
HYPE n = how intense the audio is there (0-100, relative to this video); laugh / shout / scream / cheer /
loud +n dB = what the audio analysis heard; chat spike = the live chat exploded; marked by streamer = the
streamer pressed a clip button.

TRANSCRIPT (lines L{a}..L{b - 1}):
{text}

TASK: find the best story clips in this transcript, at most {want}. Only moments that are genuinely funny,
exciting, surprising, emotional or skillful. Return fewer (or none) rather than padding with boring talk.
Rules for every story:
- It is a complete mini story: setup -> build-up -> peak -> reaction/payoff.
- start_line: the line where a viewer who knows nothing has to start to understand the peak (what they are
  doing, who/what is involved, what goes wrong). Never start in the middle of a thought.
- peak_line: the line of the funniest / most intense moment.
- end_line: the line where the reaction or payoff is complete. End on a finished sentence, before the talk
  moves on to something else.
- Length: {st['min']:.0f} to {st['max']:.0f} seconds (aim for about {st['ideal']:.0f} s); use the line times to check.
- Stories must not overlap each other.
- headline: the viral on-screen caption shown in a white box over the whole clip, written like the big
  gaming clip pages on Instagram / TikTok: third person, it NAMES the streamer (the short name viewers know,
  e.g. "Speed" for IShowSpeed), says exactly what happens in this clip, 8 to 16 English words, ONE key word in
  CAPITALS, then 1-2 emojis at the very end. Curiosity, shock or emotion, but TRUE to the clip; no swearing,
  no hashtags, no quotes around it. Examples of the style (other streamers):
  "N3on was SHOCKED after he found a hidden island in GTA 6 😭🙏",
  "Speed found a homeless NPC and gave him money 🥺❤️‍🩹",
  "xQc couldn't believe the cops followed him into the ocean 😳😂",
  "DDG got PRESSED by a Karen and LOST IT in GTA 6 😭💔",
  "N3on tried talking to a girl and got completely REJECTED by the NPC 😳🌴".
- title: a YouTube Shorts title in English (max 80 characters, no hashtags).
- why: one English sentence: what happens and why people will watch until the end.
- score: 0-100, honest (most good clips 55-75, only rare gems 85+).
"""


def _checker(a: int, b: int, lines: list[dict], st: dict):
    def hard(d: dict) -> list[str]:
        errs = []
        for k, m in enumerate(d.get("moments") or []):
            s, p, e = m["start_line"], m["peak_line"], m["end_line"]
            if not (a <= s <= e < b):
                errs.append(f"moments[{k}]: start_line/end_line must be lines L{a}..L{b - 1} with start <= end")
            elif not s <= p <= e:
                errs.append(f"moments[{k}]: peak_line must be between start_line and end_line")
        return errs

    def soft(d: dict) -> list[str]:
        errs = []
        for k, m in enumerate(d.get("moments") or []):
            s, e = m["start_line"], m["end_line"]
            if a <= s <= e < b:
                dur = lines[e]["end"] - lines[s]["start"]
                if dur < st["min"] * 0.75 or dur > st["max"] * 1.2:
                    errs.append(f"moments[{k}] is {dur:.0f} s long (L{s}..L{e}); it must be "
                                f"{st['min']:.0f}-{st['max']:.0f} s: move start_line / end_line")
        return errs

    return hard, soft


# ------------------------------------------------------------------ shaping the answer


def _fit(lines: list[dict], s: int, p: int, e: int, st: dict, duration: float) -> tuple[float, float]:
    """Line range -> (start, end) seconds inside story_min..story_max, keeping the peak."""
    lo, hi = st["min"], st["max"]

    def span(s_: int, e_: int) -> float:
        return lines[e_]["end"] + TAIL_S - lines[s_]["start"]

    # too long: drop lines from the start (never past 2 lines before the peak), then from the end
    while span(s, e) > hi and s < p - 2:
        s += 1
    while span(s, e) > hi and e > p + 1:
        e -= 1
    # too short: more setup first, then more payoff
    while span(s, e) < lo and (s > 0 or e < len(lines) - 1):
        if s > 0 and (lines[p]["start"] - lines[s]["start"] < 0.6 * lo or e >= len(lines) - 1):
            s -= 1
        elif e < len(lines) - 1:
            e += 1
        else:
            s -= 1
    start = max(0.0, lines[s]["start"] - 0.2)
    end = lines[e]["end"] + TAIL_S
    if e + 1 < len(lines):
        end = min(end, max(lines[e]["end"] + 0.25, lines[e + 1]["start"] - 0.05))
    end = min(end, duration)
    if end - start > hi:
        end = start + hi
    return round(start, 2), round(end, 2)


def _overlap(a: dict, b: dict) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(0.1, min(a["end"] - a["start"], b["end"] - b["start"]))


def find(project: Project, result: dict, model, *, on_progress=None) -> list[dict]:
    """The stories (best first), already as highlight candidates."""
    st = settings()
    meta = project.load_meta()
    transcript = project.load_transcript() if hasattr(project, "load_transcript") else None
    if transcript is None:
        import json

        tp = project.dir / "transcript.json"
        transcript = json.loads(tp.read_text(encoding="utf-8")) if tp.is_file() else None
    if not transcript:
        return []
    duration = float(result.get("duration") or meta.get("duration") or 0)
    lines = sentences(transcript, 0.0, duration + 1)
    if len(lines) < 3:
        return []
    tags = _tags_by_line(lines, result)
    from .llmcloud import CloudLLM

    cloud = isinstance(model, CloudLLM) and model._local is None
    parts = windows(lines, cloud)
    found: list[dict] = []
    t0 = time.perf_counter()
    for k, (a, b) in enumerate(parts):
        span_min = (lines[b - 1]["end"] - lines[a]["start"]) / 60
        want = max(3, min(40, int(round(span_min / 60 * 6 + 2)))) if cloud else max(2, min(6, int(span_min / 3)))
        part = (f"This is part {k + 1} of {len(parts)} of the video ({mmss(lines[a]['start'])}-{mmss(lines[b - 1]['end'])})."
                if len(parts) > 1 else f"Whole video ({mmss(duration)}).")
        hard, soft = _checker(a, b, lines, st)
        try:
            d = model.json(SYSTEM, _prompt(meta, annotated(lines, tags, a, b), a, b, st, want, part),
                           schema(want), temperature=0.4, check=hard, soft=soft, retries=2,
                           num_predict=min(4000, 300 * want + 400))
        except Exception as exc:  # noqa: BLE001 - one bad part must not lose the others
            console.print(f"[yellow]Story finder: part {k + 1}/{len(parts)} failed: {exc}[/]")
            continue
        for m in d.get("moments") or []:
            s, p, e = int(m["start_line"]), int(m["peak_line"]), int(m["end_line"])
            if not (a <= s <= p <= e < b):
                continue
            # Reels / Shorts data: 45-60 s works best. Only the strongest stories may run to story_max;
            # the others are kept within 1.6x the target (setup kept, cut from the far ends first).
            cap = st["max"] if int(m["score"]) >= 85 else min(st["max"], max(st["ideal"] * 1.6, st["min"] + 10))
            start, end = _fit(lines, s, p, e, {**st, "max": cap}, duration)
            found.append({"start": start, "end": end, "peak": round(lines[p]["start"], 2), "lines": [s, p, e],
                          "type": m["type"], "score": int(m["score"]), "headline": " ".join(m["headline"].split()),
                          "title": " ".join(m["title"].split()), "why": " ".join(m["why"].split())})
        if on_progress:
            on_progress(k + 1, len(parts))
    found.sort(key=lambda x: -x["score"])
    kept: list[dict] = []
    for f in found:
        if all(_overlap(f, g) < 0.3 for g in kept):
            kept.append(f)
    kept = kept[: st["count"]]
    console.print(f"Story finder: {len(kept)} stories from {len(parts)} part(s) in {time.perf_counter() - t0:.0f} s "
                  f"({getattr(model, 'model', '?')}).")
    return [_as_candidate(f, lines, result, transcript) for f in kept]


def _as_candidate(f: dict, lines: list[dict], result: dict, transcript: dict) -> dict:
    from .analyze import has_profanity
    from .highlights.pipeline import candidate_quality

    curve = ((result.get("curve") or {}).get("values")) or []
    top = max(curve) if curve else 0.0
    seg = curve[int(f["start"]):int(f["end"]) + 1]
    hype = int(round(100 * max(seg) / top)) if seg and top > 0 else 0
    reasons = [{"code": "llm_story", "value": f["why"], "t": f["peak"], "strength": round(f["score"] / 100, 2),
                "signal": "llm"}]
    for c in result.get("candidates") or []:  # the signals that fired inside the story
        for r in c.get("reasons") or []:
            t = r.get("t")
            if t is not None and f["start"] <= float(t) <= f["end"] and len(reasons) < 6:
                reasons.append(r)
    headline = f["headline"] if not has_profanity(f["headline"]) else ""
    text = " ".join(x["text"] for x in sentences(transcript, f["start"], f["end"]))
    score = int(round(0.75 * f["score"] + 0.25 * hype))
    return {"start": f["start"], "end": f["end"], "duration": round(f["end"] - f["start"], 2), "peak": f["peak"],
            "hype_score": score, "hype_raw": 0.0, "hype_raw_unsmoothed": 0.0, "reasons": reasons,
            "signal_scores": {}, "text": text,
            "quality": candidate_quality(score, reasons) if f["score"] >= 50 else "weak",
            "story": {"v": STORY_VERSION, "type": f["type"], "score": f["score"], "headline": headline,
                      "title": f["title"], "why": f["why"], "lines": f["lines"], "audio_hype": hype}}


def apply(project: Project, result: dict, *, on_progress=None) -> bool:
    """Replace the signal candidates with AI stories (if the AI can run). True when it did."""
    st = settings()
    if not st["enabled"] or config.get("llm_enabled") is False:
        return False
    from . import llm, llmcloud

    if llmcloud.active() is None and llm.ollama_exe() is None and llm.server_version(0.5) is None:
        return False
    try:
        with llm.session() as model:
            stories = find(project, result, model, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 - the signal candidates stay
        console.print(f"[yellow]Story finder failed, keeping the signal candidates: {exc}[/]")
        return False
    if not stories:
        return False
    keep_signal = config.get("story_keep_signal") is not False
    extra = []
    if keep_signal:  # strong signal moments the AI didn't cover stay as extra (lower) candidates
        for c in result.get("candidates") or []:
            if all(_overlap(c, s) < 0.3 for s in stories) and c.get("quality") == "good":
                extra.append({**c, "quality": "weak" if c.get("hype_score", 0) < 60 else c["quality"]})
    cands = stories + extra[: max(0, settings()["count"] - len(stories))]
    for i, c in enumerate(cands):
        c["rank"], c["id"] = i + 1, f"c{i + 1:02d}"
    result["candidates"] = cands
    result["stories"] = {"v": STORY_VERSION, "count": len(stories), "settings": st}
    return True
