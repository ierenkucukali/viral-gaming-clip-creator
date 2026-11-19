"""Live recorder (local gaming fork): record an authorized YouTube livestream FROM NOW.

Upstream SharkyClips is VOD-centric (ingest.py refuses live streams), so live capture is a
separate module with a narrow bridge to the normal pipeline (web/liverec_api.py):

    resolve (yt-dlp, Streamlink fallback)  ->  ffmpeg -c copy  ->  segments/seg_000123.ts
          ^ re-resolved on every reconnect       (no transcode)     + segments.json index
                                                                          |
    cheap CPU audio scan of COMPLETED segments (own thread) <-------------+
          -> per-second loudness  -> spike candidates (pinned)  -> "Make clip" =
             concat whole segments (stream copy) into a small file -> ordinary SharkyClips project

Rules this module keeps:
- Recording never waits for analysis: the recorder thread only moves files and appends to the
  index; analysis runs in its own thread and spools (segments stay on disk until analyzed).
- A segment is "complete" only once ffmpeg wrote it into the segment list (CSV); the file being
  written is never analyzed. After a crash, leftover files are probed and recovered.
- Timeline: `t` = seconds of recorded material in this session (gaps from reconnects/pauses are
  collapsed and flagged with `discontinuity`), never negative. Every segment also keeps UTC
  wall-clock start/end, so a wall-clock mark maps back onto the timeline.
- Retention: "rolling" keeps the last N minutes (default 90), "archive" keeps everything. Never
  deleted: pinned ranges (pending candidates, marks, clip/render jobs), segments not analyzed yet,
  and the newest segment. Low disk is an explicit alarm; at the critical level an archive
  recording stops instead of filling the disk.
- Nothing here logs in, reads cookies or bypasses DVR / access restrictions. Backfill
  (`yt-dlp --live-from-start`) is a separate best-effort task whose failure never touches the
  recording from now.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import numpy as np

from . import config
from .errors import SharkyClipsError, require_tool
from .procs import no_window
from .project import read_json, write_json_atomic

SEGMENT_SECONDS = 6.0  # short MPEG-TS pieces: little is lost on a crash, cheap to analyze
ANALYSIS_SR = 16000
ACTIVE_STATES = ("starting", "connecting", "recording", "reconnecting", "pausing", "stopping")
DEFAULTS = {
    "quality": 720,  # 720p low-bandwidth default; 1080 opt-in
    "retention": "rolling",  # rolling | archive
    "keep_minutes": 90,  # rolling window, 30..120
    "low_disk_gb": 10.0,  # alarm
    "critical_disk_gb": 3.0,  # rolling: emergency cleanup of unpinned; archive: stop recording
    "max_reconnects": 40,  # consecutive failed attempts before giving up
    "spike_db": 8.0,  # loudness rise over the rolling 60 s baseline that counts as a reaction
    "pre_roll": 10.0,
    "post_roll": 8.0,
    "clip_min": 20.0,
    "clip_max": 45.0,
}
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


class PermanentLiveError(SharkyClipsError):
    """A problem that a reconnect can't fix (sign-in needed, not live, bad link)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def settings() -> dict:
    cfg = config.get("liverec") or {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS and v is not None})
    out["keep_minutes"] = int(min(max(int(out["keep_minutes"]), 30), 120))
    out["quality"] = 1080 if int(out["quality"]) >= 1080 else 720
    out["retention"] = "archive" if out["retention"] == "archive" else "rolling"
    return out


def live_root() -> Path:
    path = config.workspace_dir() / "_live"
    path.mkdir(parents=True, exist_ok=True)
    return path


# =========================================================================== URL validation


def normalize_youtube_url(url: str) -> dict:
    """Allowlist: a single YouTube watch / live / youtu.be / channel-live link (no playlists,
    no credentials, no other hosts). Returns {"url": canonical, "video_id": id | None,
    "kind": "video" | "channel_live"}. Raises SharkyClipsError with a clear message."""
    text = (url or "").strip()
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SharkyClipsError("Paste a full YouTube link (https://www.youtube.com/watch?v=...).")
    if parts.username or parts.password:
        raise SharkyClipsError("Links with a user name / password are not accepted.")
    host = (parts.hostname or "").lower()
    if host not in _YT_HOSTS:
        raise SharkyClipsError("Live recording supports YouTube links only.",
                               "Use a youtube.com/watch?v=..., youtube.com/live/... or youtu.be/... link.")
    path = parts.path.rstrip("/")
    vid = None
    if host == "youtu.be":
        vid = path.lstrip("/").split("/")[0]
    elif path == "/watch":
        vid = (parse_qs(parts.query).get("v") or [""])[0]
    elif path.startswith("/live/") or path.startswith("/shorts/"):
        vid = path.split("/")[2] if len(path.split("/")) > 2 else ""
    elif re.fullmatch(r"/(@[\w.\-]+|channel/UC[\w-]{22}|c/[\w.\-]+|user/[\w.\-]+)/live", path):
        return {"url": f"https://www.youtube.com{path}", "video_id": None, "kind": "channel_live"}
    else:
        raise SharkyClipsError("That YouTube link is not a single video or live stream.",
                               "Open the stream and copy its address (watch?v=... or /live/...).")
    if not vid or not YT_ID.match(vid):
        raise SharkyClipsError("That YouTube link has no valid video id.")
    return {"url": f"https://www.youtube.com/watch?v={vid}", "video_id": vid, "kind": "video"}


def _test_hls_allowed(url: str) -> bool:
    """Tests / local smoke runs only: SHARKYCLIPS_LIVE_TEST_HLS=1 lets a loopback HLS playlist
    (http://127.0.0.1:<port>/....m3u8) stand in for YouTube. Never on by default."""
    if os.environ.get("SHARKYCLIPS_LIVE_TEST_HLS") != "1":
        return False
    parts = urlsplit(url)
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and parts.path.endswith(".m3u8")


# ============================================================================ resolving


@dataclass
class Resolved:
    stream_url: str
    title: str
    channel: str
    video_id: str
    is_live: bool
    live_status: str
    headers: dict = field(default_factory=dict)
    via: str = "yt-dlp"
    height: int | None = None


def _ydl_format(max_h: int) -> str:
    # Muxed HLS (YouTube live serves a+v in one HLS variant): stream copy needs no merging.
    return (f"best[protocol^=m3u8][height<={max_h}]/best[protocol^=m3u8]/"
            f"best[height<={max_h}]/best")


def resolve_ytdlp(url: str, max_h: int) -> Resolved:
    import yt_dlp

    from .ingest import _YdlLogger, js_runtimes

    opts: dict[str, Any] = {"quiet": True, "noplaylist": True, "skip_download": True, "socket_timeout": 30,
                            "retries": 3, "logger": _YdlLogger(), "format": _ydl_format(max_h)}
    if js_runtimes():
        opts["js_runtimes"] = js_runtimes()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        raise SharkyClipsError("That link is a playlist/channel page, not a live stream.")
    status = str(info.get("live_status") or ("is_live" if info.get("is_live") else "not_live"))
    sel = info.get("requested_formats") or [info]
    stream = sel[0].get("url") or info.get("url") or info.get("manifest_url") or ""
    return Resolved(stream_url=stream, title=info.get("title") or info.get("id") or "live",
                    channel=info.get("channel") or info.get("uploader") or "unknown",
                    video_id=info.get("id") or "", is_live=bool(info.get("is_live")), live_status=status,
                    headers=dict(sel[0].get("http_headers") or info.get("http_headers") or {}),
                    via="yt-dlp", height=sel[0].get("height") or info.get("height"))


def resolve_streamlink(url: str, max_h: int) -> Resolved:
    exe = shutil.which("streamlink")
    if not exe:
        raise SharkyClipsError("Streamlink is not installed (optional fallback).")
    quality = f"{max_h}p,{max_h}p60,best" if max_h else "best"
    out = subprocess.run([exe, "--stream-url", url, quality], capture_output=True, text=True, timeout=60,
                         **no_window())
    line = (out.stdout or "").strip().splitlines()[-1:] or [""]
    if out.returncode != 0 or not line[0].startswith("http"):
        raise SharkyClipsError(f"Streamlink could not open the stream: {(out.stderr or out.stdout)[-300:]}")
    return Resolved(stream_url=line[0], title="live", channel="unknown", video_id="", is_live=True,
                    live_status="is_live", via="streamlink", height=max_h)


def resolve(url: str, max_h: int = 720) -> Resolved:
    """The current HLS address of an authorized live stream (re-run on every reconnect: the
    signed addresses expire). yt-dlp first, Streamlink as fallback for the address only."""
    if _test_hls_allowed(url):
        return Resolved(stream_url=url, title="Local test stream", channel="local test", video_id="test",
                        is_live=True, live_status="is_live", via="test-hls", height=None)
    try:
        norm = normalize_youtube_url(url)
    except SharkyClipsError as exc:
        raise PermanentLiveError(str(exc), exc.hint) from exc
    try:
        r = resolve_ytdlp(norm["url"], max_h)
    except SharkyClipsError:
        raise
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        low = text.lower()
        if any(k in low for k in ("sign in", "members-only", "private video", "age", "login", "cookies")):
            raise PermanentLiveError(
                "This stream needs a signed-in account (members-only, private or age-restricted).",
                "SharkyClips never logs in or uses cookies. Only public streams can be recorded.") from exc
        try:
            r = resolve_streamlink(norm["url"], max_h)
        except Exception:  # noqa: BLE001
            raise SharkyClipsError(f"Could not open the live stream: {text[:300]}",
                                   "Update yt-dlp (pip install -U 'yt-dlp[default]') and check the link.") from exc
    if not r.is_live and r.live_status != "is_live":
        if r.live_status in ("was_live", "post_live", "not_live"):
            raise PermanentLiveError("This stream is not live right now.",
                                   "Use the YouTube VOD import for a finished stream (when the VOD is available).")
        if r.live_status == "is_upcoming":
            raise SharkyClipsError("This stream hasn't started yet.", "Start recording once it is live.")
    if not r.stream_url:
        raise SharkyClipsError("The stream has no playable HLS address.")
    return r


# ============================================================================ index / pins


@dataclass
class Segment:
    n: int
    file: str
    t0: float  # session seconds where this segment starts
    dur: float
    wall_start: str
    wall_end: str
    run: int
    bytes: int
    discontinuity: bool = False
    recovered: bool = False

    @property
    def t1(self) -> float:
        return self.t0 + self.dur


def overlapping(segments: list[Segment], t0: float, t1: float) -> list[Segment]:
    return [s for s in segments if s.t1 > t0 and s.t0 < t1]


def wall_to_t(segments: list[Segment], wall: float) -> float:
    """UTC timestamp -> session seconds (approximate inside a segment; past the last complete
    segment it extrapolates; inside a gap it snaps to the next segment's start). Never negative."""
    if not segments:
        return 0.0
    for s in segments:
        ws, we = _ts(s.wall_start), _ts(s.wall_end)
        if wall < ws:
            return max(0.0, s.t0)
        if ws <= wall <= we:
            return max(0.0, s.t0 + min(s.dur, wall - ws))
    last = segments[-1]
    return max(0.0, last.t1 + (wall - _ts(last.wall_end)))


def deletable(segments: list[Segment], pins: list[dict], mode: str, keep_s: float, analyzed_until: float,
              emergency: bool = False) -> list[Segment]:
    """Segments the retention policy may remove now (oldest first). Never: the newest segment, a
    pinned range, anything not analyzed yet (unless `emergency`), anything in archive mode
    (unless `emergency` - and even then pins and the newest segment stay)."""
    if len(segments) <= 1:
        return []
    newest = segments[-1]
    horizon = newest.t1 - keep_s
    out = []
    for s in segments[:-1]:
        if any(s.t1 > float(p["t0"]) and s.t0 < float(p["t1"]) for p in pins):
            continue
        if emergency:
            out.append(s)
            continue
        if mode != "rolling":
            continue
        if s.t1 <= horizon and s.t1 <= analyzed_until:
            out.append(s)
    return out


def disk_free(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


# ============================================================================ audio analysis


def segment_levels(path: Path, ffmpeg: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Per-second RMS level (dBFS) and voice-band share (300-3400 Hz energy / total) of one
    segment's audio. Cheap CPU work (decodes only audio, 16 kHz mono)."""
    exe = ffmpeg or require_tool("ffmpeg")
    out = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path), "-vn",
                          "-ac", "1", "-ar", str(ANALYSIS_SR), "-f", "s16le", "pipe:1"],
                         capture_output=True, timeout=120, **no_window())
    pcm = np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(np.ceil(len(pcm) / ANALYSIS_SR))
    if n == 0:
        return np.zeros(0, np.float32), np.zeros(0, np.float32)
    pad = np.zeros(n * ANALYSIS_SR, np.float32)
    pad[: len(pcm)] = pcm
    frames = pad.reshape(n, ANALYSIS_SR)
    rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(ANALYSIS_SR, 1 / ANALYSIS_SR)
    band = (freqs >= 300) & (freqs <= 3400)
    voice = spec[:, band].sum(axis=1) / (spec.sum(axis=1) + 1e-9)
    return db.astype(np.float32), voice.astype(np.float32)


def detect_spikes(levels: np.ndarray, voice: np.ndarray, until: float, cfg: dict | None = None,
                  start_at: float = 0.0) -> list[dict]:
    """Reaction candidates from the per-second level curve (pure function, unit tested).

    A spike = level rises >= spike_db over the rolling 60 s median before it (works for loud and
    quiet streamers alike), above a silence floor. Seconds of a spike within 3 s are one event.
    Window = peak - pre_roll .. event end + post_roll, clamped to clip_min..clip_max and to the
    analyzed material; only windows that END before `until` are returned (the reaction is fully
    recorded). Overlapping events are merged when the merge stays under clip_max."""
    c = {**DEFAULTS, **(cfg or {})}
    n = min(len(levels), int(until))
    if n < 10:
        return []
    L = levels[:n].astype(np.float64)
    base = np.empty(n)
    for i in range(n):
        a = max(0, i - 60)
        ref = L[a:max(a + 1, i - 2)]
        base[i] = np.median(ref) if len(ref) else L[i]
    rise = L - base
    hot = (rise >= c["spike_db"]) & (L > -45.0)
    hot[: int(max(0, start_at))] = False
    events: list[list[int]] = []
    for i in np.flatnonzero(hot):
        if events and i - events[-1][-1] <= 3:
            events[-1].append(int(i))
        else:
            events.append([int(i)])
    out: list[dict] = []
    for ev in events:
        peak = max(ev, key=lambda i: rise[i])
        strength = float(min(1.0, 0.4 + (rise[peak] - c["spike_db"]) / 12.0))
        v = float(np.mean(voice[ev[0]:ev[-1] + 1])) if len(voice) > ev[-1] else 0.0
        s = max(0.0, peak - c["pre_roll"])
        e = ev[-1] + 1 + c["post_roll"]
        e = min(max(e, s + c["clip_min"]), s + c["clip_max"])
        if e > until:
            continue  # the reaction isn't fully recorded yet: wait for more segments
        reasons = [{"code": "loudness_spike", "value": f"+{rise[peak]:.0f} dB", "t": float(peak),
                    "strength": round(strength, 2), "signal": "loudness"}]
        if len(ev) >= 3:
            reasons.append({"code": "sustained_loud", "value": f"{len(ev)} s", "t": float(ev[0]),
                            "strength": round(min(1.0, len(ev) / 8), 2), "signal": "loudness"})
        if v >= 0.45:
            reasons.append({"code": "voice", "value": f"{v:.0%}", "t": float(peak), "strength": round(v, 2),
                            "signal": "voice"})
        score = int(round(100 * min(1.0, strength * (0.85 + 0.15 * min(1.0, len(ev) / 5)) + 0.1 * (v >= 0.45))))
        cand = {"start": round(s, 2), "end": round(e, 2), "peak": float(peak), "score": score, "reasons": reasons}
        prev = out[-1] if out else None
        if prev and cand["start"] < prev["end"]:
            if max(prev["end"], cand["end"]) - prev["start"] <= c["clip_max"]:
                prev["end"] = max(prev["end"], cand["end"])
                prev["reasons"] = (prev["reasons"] + reasons)[:6]
                if cand["score"] > prev["score"]:
                    prev["score"], prev["peak"] = cand["score"], cand["peak"]
                continue
            if cand["score"] <= prev["score"]:
                continue
            out.pop()
        out.append(cand)
    return out


# ============================================================================ the session


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run([require_tool("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=30,
                             **no_window())
        return float(out.stdout.strip() or 0)
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0.0


class Session:
    """One recording (one stream URL). Files: <workspace>/_live/<id>/{session.json, segments.json,
    pins.json, candidates.json, levels.json, segments/, backfill/, clips_src/}."""

    def __init__(self, sid: str, root: Path | None = None):
        self.id = sid
        self.dir = (root or live_root()) / sid
        self.seg_dir = self.dir / "segments"
        self.lock = threading.RLock()
        self.info: dict = {}
        self.segments: list[Segment] = []
        self.pins: list[dict] = []
        self.candidates: list[dict] = []
        self.levels = np.zeros(0, np.float32)
        self.voice = np.zeros(0, np.float32)
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.pause_flag = threading.Event()
        self.backfill_proc: subprocess.Popen | None = None
        self.resolver: Callable[[str, int], Resolved] = resolve
        self._seen_csv: dict[str, int] = {}

    # ------------------------------------------------------------------ persistence

    def load(self) -> "Session":
        self.info = read_json(self.dir / "session.json") if (self.dir / "session.json").is_file() else {}
        segs = read_json(self.dir / "segments.json").get("segments", []) if (self.dir / "segments.json").is_file() else []
        self.segments = [Segment(**s) for s in segs]
        self.pins = read_json(self.dir / "pins.json").get("pins", []) if (self.dir / "pins.json").is_file() else []
        self.candidates = (read_json(self.dir / "candidates.json").get("candidates", [])
                           if (self.dir / "candidates.json").is_file() else [])
        if (self.dir / "levels.json").is_file():
            lv = read_json(self.dir / "levels.json")
            self.levels = np.asarray(lv.get("db", []), np.float32)
            self.voice = np.asarray(lv.get("voice", []), np.float32)
        return self

    def save_info(self) -> None:
        with self.lock:
            self.info["updated_at"] = now_iso()
            write_json_atomic(self.dir / "session.json", self.info)

    def _save_index(self) -> None:
        write_json_atomic(self.dir / "segments.json", {"version": 1, "segments": [asdict(s) for s in self.segments]})

    def _save_pins(self) -> None:
        write_json_atomic(self.dir / "pins.json", {"pins": self.pins})

    def _save_candidates(self) -> None:
        write_json_atomic(self.dir / "candidates.json", {"candidates": self.candidates})

    def _save_levels(self) -> None:
        write_json_atomic(self.dir / "levels.json", {"db": [round(float(v), 1) for v in self.levels],
                                                      "voice": [round(float(v), 2) for v in self.voice]},
                          compact=True)

    # ------------------------------------------------------------------ timeline

    @property
    def recorded(self) -> float:
        return self.segments[-1].t1 if self.segments else 0.0

    @property
    def analyzed_until(self) -> float:
        return float(self.info.get("analyzed_until") or 0.0)

    def add_segment(self, path: Path, dur: float, run: int, discontinuity: bool, recovered: bool = False,
                    wall_end: float | None = None) -> Segment | None:
        if dur <= 0.05 or not path.is_file():
            return None
        with self.lock:
            if any(s.file == path.name for s in self.segments):
                return None
            we = wall_end or time.time()
            n = int(re.findall(r"\d+", path.stem)[-1]) if re.findall(r"\d+", path.stem) else len(self.segments)
            seg = Segment(n=n, file=path.name, t0=round(self.recorded, 3), dur=round(float(dur), 3),
                          wall_start=datetime.fromtimestamp(we - dur, timezone.utc).isoformat(timespec="milliseconds"),
                          wall_end=datetime.fromtimestamp(we, timezone.utc).isoformat(timespec="milliseconds"),
                          run=run, bytes=path.stat().st_size, discontinuity=discontinuity, recovered=recovered)
            self.segments.append(seg)
            self._save_index()
            self.info["last_segment"] = {"n": seg.n, "t0": seg.t0, "dur": seg.dur, "wall_end": seg.wall_end}
            self.save_info()
            return seg

    def next_number(self) -> int:
        nums = [s.n for s in self.segments]
        for f in self.seg_dir.glob("seg_*.ts"):
            m = re.findall(r"\d+", f.stem)
            if m:
                nums.append(int(m[-1]))
        return max(nums) + 1 if nums else 0

    def recover_orphans(self) -> int:
        """Segment files on disk that are not in the index (the server or ffmpeg died mid-segment):
        probe them and append the playable ones, in file order. Returns how many."""
        known = {s.file for s in self.segments}
        got = 0
        for f in sorted(self.seg_dir.glob("seg_*.ts")):
            if f.name in known:
                continue
            dur = _probe_duration(f)
            if dur > 0.5 and self.add_segment(f, dur, int(self.info.get("run") or 0), discontinuity=True,
                                              recovered=True, wall_end=f.stat().st_mtime):
                got += 1
        return got

    # ------------------------------------------------------------------ pins / retention

    def pin(self, t0: float, t1: float, reason: str, ref: str | None = None) -> dict:
        p = {"id": uuid.uuid4().hex[:10], "t0": round(max(0.0, t0), 2), "t1": round(max(t0, t1), 2),
             "reason": reason, "ref": ref, "created_at": now_iso()}
        with self.lock:
            self.pins.append(p)
            self._save_pins()
        return p

    def unpin(self, pid: str | None = None, ref: str | None = None) -> int:
        with self.lock:
            before = len(self.pins)
            self.pins = [p for p in self.pins if not ((pid and p["id"] == pid) or (ref and p.get("ref") == ref))]
            self._save_pins()
            return before - len(self.pins)

    def apply_retention(self) -> dict:
        """Delete what the policy allows; returns the disk state (alarm levels included)."""
        st = self.info.get("settings") or settings()
        free = disk_free(self.dir)
        low = free < float(st["low_disk_gb"]) * 1e9
        critical = free < float(st["critical_disk_gb"]) * 1e9
        removed = 0
        with self.lock:
            for s in deletable(self.segments, self.pins, st["retention"], float(st["keep_minutes"]) * 60,
                               self.analyzed_until, emergency=critical and st["retention"] == "rolling"):
                (self.seg_dir / s.file).unlink(missing_ok=True)
                self.segments.remove(s)
                removed += 1
                if critical and disk_free(self.dir) > float(st["critical_disk_gb"]) * 1e9 * 1.5:
                    break
            if removed:
                self._save_index()
                self.info["deleted_segments"] = int(self.info.get("deleted_segments") or 0) + removed
                self.info["first_t"] = self.segments[0].t0 if self.segments else self.recorded
        disk = {"free_bytes": disk_free(self.dir), "low": low, "critical": critical, "removed": removed}
        self.info["disk"] = disk
        return disk

    def bytes_on_disk(self) -> int:
        return sum(s.bytes for s in self.segments)

    def bitrate_bps(self) -> float:
        recent = self.segments[-20:]
        dur = sum(s.dur for s in recent)
        return (sum(s.bytes for s in recent) * 8 / dur) if dur > 0 else 0.0

    # ------------------------------------------------------------------ analysis

    def analyze_pending(self, ffmpeg: str | None = None, max_segments: int = 50) -> int:
        """Scan completed, not yet analyzed segments (oldest first); update candidates."""
        done = 0
        cfg = self.info.get("settings") or settings()
        for s in list(self.segments):
            if done >= max_segments or self.stop_analysis():
                break
            if s.t1 <= self.analyzed_until + 1e-6:
                continue
            path = self.seg_dir / s.file
            if not path.is_file():
                continue
            try:
                db, voice = segment_levels(path, ffmpeg)
            except Exception as exc:  # noqa: BLE001 - a broken segment never stops the rest
                self.error(f"analysis of {s.file} failed: {exc}")
                db, voice = np.full(int(np.ceil(s.dur)), -90.0, np.float32), np.zeros(int(np.ceil(s.dur)), np.float32)
            a = int(round(s.t0))
            need = a + len(db)
            with self.lock:
                if len(self.levels) < need:
                    self.levels = np.concatenate([self.levels, np.full(need - len(self.levels), -90.0, np.float32)])
                    self.voice = np.concatenate([self.voice, np.zeros(need - len(self.voice), np.float32)])
                self.levels[a:need] = db
                self.voice[a:need] = voice
                self.info["analyzed_until"] = round(s.t1, 3)
            done += 1
        if done:
            self._update_candidates(cfg)
            self._save_levels()
            self.save_info()
        return done

    def stop_analysis(self) -> bool:
        return False

    def _update_candidates(self, cfg: dict) -> None:
        found = detect_spikes(self.levels, self.voice, self.analyzed_until, cfg,
                              start_at=float(self.info.get("first_t") or 0))
        with self.lock:
            changed = False
            for f in found:
                dup = next((c for c in self.candidates if c["start"] < f["end"] and f["start"] < c["end"]), None)
                if dup is not None:
                    continue  # already offered (de-duplicated by overlap)
                cid = f"L{len(self.candidates) + 1:03d}"
                pin = self.pin(f["start"], f["end"], "candidate", ref=cid)
                self.candidates.append({**f, "id": cid, "status": "new", "pin": pin["id"], "kind": "auto",
                                        "wall": self._wall_at(f["peak"]), "created_at": now_iso()})
                changed = True
            if changed:
                self._save_candidates()

    def _wall_at(self, t: float) -> str | None:
        for s in self.segments:
            if s.t0 <= t <= s.t1:
                return datetime.fromtimestamp(_ts(s.wall_start) + (t - s.t0), timezone.utc).isoformat(timespec="seconds")
        return None

    def add_mark(self, wall: float | None = None, note: str = "", before: float = 25.0, after: float = 8.0) -> dict:
        """Mark moment now: maps the wall clock onto the timeline (the live edge when the moment
        isn't recorded yet) and pins the window; it becomes clip-able once recorded."""
        with self.lock:
            t = wall_to_t(self.segments, wall or time.time())
            cid = f"M{sum(1 for c in self.candidates if c.get('kind') == 'mark') + 1:03d}"
            s, e = max(float(self.info.get("first_t") or 0), t - before), t + after
            pin = self.pin(s, e, "mark", ref=cid)
            c = {"id": cid, "kind": "mark", "start": round(s, 2), "end": round(e, 2), "peak": round(t, 2),
                 "score": 100, "reasons": [{"code": "marker", "value": note or "manual", "t": round(t, 2),
                                             "strength": 1.0, "signal": "marker"}],
                 "status": "new", "pin": pin["id"], "wall": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "created_at": now_iso()}
            self.candidates.append(c)
            self._save_candidates()
            return c

    def candidate(self, cid: str) -> dict:
        c = next((c for c in self.candidates if c["id"] == cid), None)
        if c is None:
            raise SharkyClipsError(f"Live candidate {cid} doesn't exist.")
        return c

    def set_candidate(self, cid: str, **changes: Any) -> dict:
        with self.lock:
            c = self.candidate(cid)
            c.update(changes)
            self._save_candidates()
            return c

    # ------------------------------------------------------------------ assembling windows

    def assemble(self, t0: float, t1: float, dest: Path, ffmpeg: str | None = None) -> dict:
        """Concat the WHOLE segments that cover [t0, t1] into one MP4 (stream copy, no
        re-encode). Returns {"path", "offset": session t of the file's 0, "t0", "t1"} so the
        moment's exact position in the file is t - offset (no keyframe guessing)."""
        with self.lock:
            segs = overlapping(self.segments, t0, t1)
            if not segs:
                raise SharkyClipsError("That part of the stream isn't on disk (not recorded yet, or "
                                       "already removed by the rolling buffer).")
            missing = [s for s in segs if not (self.seg_dir / s.file).is_file()]
            if missing:
                raise SharkyClipsError("Some segments of that window were removed.")
            if segs[0].t0 > t0 + 0.5 and segs[0] is not self.segments[0]:
                raise SharkyClipsError("The start of that window is no longer on disk.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        lst = dest.with_suffix(".txt")
        lst.write_text("".join(f"file '{(self.seg_dir / s.file).as_posix()}'\n" for s in segs), encoding="utf-8")
        tmp = dest.with_name(dest.stem + ".partial.mp4")
        cmd = [ffmpeg or require_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-f", "concat", "-safe", "0", "-i", str(lst), "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", "-avoid_negative_ts", "make_zero", str(tmp)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600, **no_window())
        lst.unlink(missing_ok=True)
        if res.returncode != 0 or not tmp.is_file():
            tmp.unlink(missing_ok=True)
            raise SharkyClipsError(f"Could not assemble the clip window: {res.stderr[-400:]}")
        os.replace(tmp, dest)
        return {"path": str(dest), "offset": segs[0].t0, "t0": segs[0].t0, "t1": segs[-1].t1,
                "segments": [s.n for s in segs], "discontinuity": any(s.discontinuity for s in segs[1:])}

    # ------------------------------------------------------------------ status

    def error(self, text: str) -> None:
        with self.lock:
            errs = list(self.info.get("errors") or [])
            errs.append({"at": now_iso(), "text": str(text)[:500]})
            self.info["errors"] = errs[-20:]
        self.save_info()

    def set_state(self, state: str, **extra: Any) -> None:
        with self.lock:
            self.info["state"] = state
            self.info.update(extra)
        self.save_info()

    def view(self, timeline: bool = False) -> dict:
        st = self.info.get("settings") or settings()
        free = disk_free(self.dir)
        bps = self.bitrate_bps()
        started = self.info.get("started_at")
        out = {
            "id": self.id, "state": self.info.get("state"), "url": self.info.get("url"),
            "name": self.info.get("name"), "title": self.info.get("title"), "channel": self.info.get("channel"),
            "via": self.info.get("via"), "started_at": started,
            "elapsed_s": round(time.time() - _ts(started), 1) if started and self.info.get("state") in ACTIVE_STATES
            else self.info.get("elapsed_s"),
            "recorded_s": round(self.recorded, 2),
            "buffered_s": round(sum(s.dur for s in self.segments), 2),
            "first_t": self.segments[0].t0 if self.segments else 0.0,
            "segments": len(self.segments), "deleted_segments": int(self.info.get("deleted_segments") or 0),
            "last_segment": self.info.get("last_segment"),
            "bytes": self.bytes_on_disk(), "bitrate_bps": round(bps),
            "disk": {"free_bytes": free, "low": free < float(st["low_disk_gb"]) * 1e9,
                     "critical": free < float(st["critical_disk_gb"]) * 1e9,
                     "hours_left": round(free / (bps / 8) / 3600, 1) if bps > 0 else None},
            "analysis": {"analyzed_until": round(self.analyzed_until, 2),
                         "pending_s": round(max(0.0, self.recorded - self.analyzed_until), 2),
                         "candidates": len(self.candidates)},
            "reconnects": int(self.info.get("reconnects") or 0), "errors": (self.info.get("errors") or [])[-5:],
            "settings": st, "pins": len(self.pins), "backfill": self.info.get("backfill"),
            "end_reason": self.info.get("end_reason"), "project_id": self.info.get("project_id"),
            "candidates": sorted(self.candidates, key=lambda c: (c.get("status") == "dismissed", -c.get("score", 0),
                                                                 c.get("start", 0))),
        }
        if timeline:
            lv = self.levels
            step = max(1, int(np.ceil(len(lv) / 1500))) if len(lv) else 1
            pts = [round(float(lv[i:i + step].max()), 1) for i in range(0, len(lv), step)]
            out["timeline"] = {"step_s": step, "db": pts}
        return out


# ============================================================================ recorder


def ffmpeg_record_cmd(ffmpeg: str, r: Resolved, seg_dir: Path, csv: Path, start_number: int,
                      seg_s: float = SEGMENT_SECONDS) -> list[str]:
    """argv for one recording run (stream copy into MPEG-TS segments). No shell."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-rw_timeout", "20000000"]
    if r.stream_url.startswith("http"):
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                "-reconnect_delay_max", "10"]
    ua = r.headers.get("User-Agent")
    if ua:
        cmd += ["-user_agent", ua]
    extra = "".join(f"{k}: {v}\r\n" for k, v in r.headers.items() if k.lower() not in ("user-agent",)
                    and re.fullmatch(r"[A-Za-z0-9-]+", k) and "\n" not in str(v) and "\r" not in str(v))
    if extra:
        cmd += ["-headers", extra]
    cmd += ["-i", r.stream_url, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
            "-segment_time", f"{seg_s:g}", "-segment_format", "mpegts", "-segment_list", str(csv),
            "-segment_list_type", "csv", "-segment_start_number", str(start_number),
            "-reset_timestamps", "0", str(seg_dir / "seg_%06d.ts")]
    return cmd


def read_segment_csv(csv: Path, seen: int) -> tuple[list[tuple[str, float]], int]:
    """New complete segments in ffmpeg's CSV list since line `seen`: ([(file, duration)], new seen).
    A last line without its newline is still being written and is read next time."""
    if not csv.is_file():
        return [], seen
    try:
        text = csv.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], seen
    lines = text.split("\n")[:-1]  # only lines that end with a newline are complete
    out = []
    for line in lines[seen:]:
        parts = line.strip().split(",")
        try:
            out.append((parts[0], max(0.0, float(parts[2]) - float(parts[1]))))
        except (IndexError, ValueError):
            pass
    return out, max(seen, len(lines))


class Recorder:
    """The recording loop of one Session (own thread). Analysis runs in a separate thread."""

    def __init__(self, session: Session, on_segment: Callable[[Session, Segment], None] | None = None):
        self.s = session
        self.on_segment = on_segment
        self.ffmpeg = require_tool("ffmpeg")
        self.last_rc: int | None = None

    def run(self) -> None:
        s = self.s
        st = s.info.get("settings") or settings()
        failures = 0
        s.set_state("connecting")
        while not s.stop_flag.is_set():
            if s.pause_flag.is_set():
                s.set_state("paused")
                while s.pause_flag.is_set() and not s.stop_flag.is_set():
                    time.sleep(0.3)
                if s.stop_flag.is_set():
                    break
                s.set_state("connecting")
            try:
                r = s.resolver(s.info["url"], int(st["quality"]))
            except PermanentLiveError as exc:
                msg = str(exc)
                if "not live" in msg.lower() and s.segments:
                    s.set_state("ended", end_reason="The stream ended (no longer live).")
                else:
                    s.error(f"resolve: {msg}")
                    s.set_state("failed", end_reason=msg)
                break
            except Exception as exc:  # noqa: BLE001 - network etc.: retry with backoff
                msg = str(exc)
                failures += 1
                s.error(f"resolve: {msg}")
                if failures > int(st["max_reconnects"]):
                    s.set_state("failed", end_reason=msg)
                    break
                s.set_state("reconnecting", reconnects=int(s.info.get("reconnects") or 0) + 1)
                self._sleep(min(60.0, 2.0 ** min(failures, 6)))
                continue
            with s.lock:
                s.info.update(title=s.info.get("title") or r.title, channel=s.info.get("channel") or r.channel,
                              via=r.via, video_id=s.info.get("video_id") or r.video_id)
                run = int(s.info.get("run") or 0) + 1
                s.info["run"] = run
            got = self._record_run(r, run)
            if s.stop_flag.is_set() or s.pause_flag.is_set():
                continue
            if self.last_rc == 0 and got and playlist_ended(r):
                s.set_state("ended", end_reason="The stream ended (the broadcast playlist is closed).")
                break
            if got:
                failures = 0
            else:
                failures += 1
            if failures > int(st["max_reconnects"]):
                s.set_state("failed", end_reason="Too many failed reconnects.")
                break
            if s.info.get("state") == "stopped_disk":
                break
            s.set_state("reconnecting", reconnects=int(s.info.get("reconnects") or 0) + 1)
            self._sleep(min(30.0, 1.0 + 2.0 ** min(failures, 5)))
        if s.info.get("state") in ACTIVE_STATES or s.info.get("state") == "paused":
            s.set_state("stopped", end_reason=s.info.get("end_reason") or "Stopped by you.")
        with s.lock:
            started = s.info.get("started_at")
            if started:
                s.info["elapsed_s"] = round(time.time() - _ts(started), 1)
        s.save_info()

    def _sleep(self, secs: float) -> None:
        end = time.time() + secs
        while time.time() < end and not s_flag(self.s):
            time.sleep(0.25)

    def _record_run(self, r: Resolved, run: int) -> int:
        """One ffmpeg process. Returns how many complete segments it produced."""
        s = self.s
        s.seg_dir.mkdir(parents=True, exist_ok=True)
        csv = s.dir / f"list_run{run:03d}.csv"
        cmd = ffmpeg_record_cmd(self.ffmpeg, r, s.seg_dir, csv, s.next_number())
        log = open(s.dir / f"ffmpeg_run{run:03d}.log", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                                    **no_window())
        except OSError as exc:
            log.close()
            s.error(f"ffmpeg could not start: {exc}")
            return 0
        s.proc = proc
        s.set_state("recording")
        seen, got, first = 0, 0, True
        try:
            while True:
                rc = proc.poll()
                new, seen = read_segment_csv(csv, seen)
                for name, dur in new:
                    seg = s.add_segment(s.seg_dir / Path(name).name, dur, run, discontinuity=first and bool(s.segments))
                    first = False
                    if seg is not None:
                        got += 1
                        disk = s.apply_retention()
                        if disk["critical"] and (s.info.get("settings") or {}).get("retention") == "archive":
                            s.error("Disk almost full: recording stopped to protect your disk "
                                    "(archive mode never deletes). Free space, then resume.")
                            s.set_state("stopped_disk", end_reason="Disk almost full.")
                            self._stop_proc(proc)
                        if self.on_segment:
                            self.on_segment(s, seg)
                if rc is not None:
                    break
                if s.stop_flag.is_set() or s.pause_flag.is_set():
                    self._stop_proc(proc)
                time.sleep(0.5)
            new, seen = read_segment_csv(csv, seen)
            for name, dur in new:
                if s.add_segment(s.seg_dir / Path(name).name, dur, run, discontinuity=first and bool(s.segments)):
                    got += 1
                    first = False
            got += s.recover_orphans()  # a last segment that ffmpeg didn't list (killed)
            self.last_rc = proc.returncode
            if proc.returncode not in (0, 255) and not s.stop_flag.is_set():
                tail = _tail(s.dir / f"ffmpeg_run{run:03d}.log")
                s.error(f"ffmpeg exited with {proc.returncode}: {tail}")
        finally:
            log.close()
            s.proc = None
        return got

    def _stop_proc(self, proc: subprocess.Popen) -> None:
        """Ask ffmpeg to finish (q on stdin closes the current segment cleanly), then kill."""
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def playlist_ended(r: Resolved, timeout: float = 15.0) -> bool:
    """True when the HLS playlist says the broadcast is over (#EXT-X-ENDLIST). A master
    playlist is followed to its first variant. Any network problem -> False (re-resolve)."""
    import urllib.request
    from urllib.parse import urljoin

    url = r.stream_url
    if not url.startswith("http"):
        return False
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={k: v for k, v in r.headers.items() if "\n" not in str(v)})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - http(s) only, checked above
                text = resp.read(2_000_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
        if "#EXT-X-ENDLIST" in text:
            return True
        if "#EXT-X-STREAM-INF" in text:
            nxt = next((ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")), "")
            if not nxt:
                return False
            url = urljoin(url, nxt)
            continue
        return False
    return False


def s_flag(s: Session) -> bool:
    return s.stop_flag.is_set() or s.pause_flag.is_set()


def _tail(path: Path, n: int = 300) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:].strip()
    except OSError:
        return ""


# ============================================================================ manager


class Manager:
    """All sessions of this server: start / stop / pause / resume, the analysis thread and
    restart recovery. One active recorder per stream (video id / URL)."""

    def __init__(self, root: Path | None = None, resolver: Callable[[str, int], Resolved] | None = None):
        self.root = root
        self.resolver = resolver
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self._analysis: threading.Thread | None = None
        self._closing = False
        self.on_candidates: list[Callable[[Session], None]] = []
        self._load_existing()

    def _root(self) -> Path:
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            return self.root
        return live_root()

    def _load_existing(self) -> None:
        for d in sorted(self._root().glob("*/session.json")):
            s = Session(d.parent.name, self._root()).load()
            if self.resolver:
                s.resolver = self.resolver
            if s.info.get("state") in ACTIVE_STATES:
                # the server stopped while recording: keep everything, recover the last segment
                n = s.recover_orphans()
                s.info["state"] = "interrupted"
                s.info["end_reason"] = (f"SharkyClips stopped while recording ({n} segment(s) recovered). "
                                        "Press Resume to continue from now.")
                s.save_info()
            self.sessions[s.id] = s

    def list(self) -> list[dict]:
        return [s.view() for s in sorted(self.sessions.values(), key=lambda s: s.info.get("started_at") or "",
                                         reverse=True)]

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise SharkyClipsError(f"Live session {sid} not found.")
        return s

    def _key(self, url: str) -> str:
        if _test_hls_allowed(url):
            return url
        n = normalize_youtube_url(url)
        return n["video_id"] or n["url"]

    def start(self, url: str, name: str = "", quality: int | None = None, retention: str | None = None,
              keep_minutes: int | None = None) -> Session:
        key = self._key(url)
        with self.lock:
            for s in self.sessions.values():
                if s.info.get("key") == key and s.info.get("state") in ACTIVE_STATES + ("paused",):
                    raise SharkyClipsError("This stream is already being recorded.",
                                           f"Open the running session ({s.id}).")
            st = settings()
            if quality:
                st["quality"] = 1080 if int(quality) >= 1080 else 720
            if retention in ("rolling", "archive"):
                st["retention"] = retention
            if keep_minutes:
                st["keep_minutes"] = int(min(max(int(keep_minutes), 30), 120))
            free = disk_free(self._root())
            if free < float(st["critical_disk_gb"]) * 1e9:
                raise SharkyClipsError(f"Not enough free disk space ({free / 1e9:.1f} GB).",
                                       "Free some space first; recording needs a few GB per hour.")
            sid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            s = Session(sid, self._root())
            s.dir.mkdir(parents=True, exist_ok=True)
            s.seg_dir.mkdir(exist_ok=True)
            if self.resolver:
                s.resolver = self.resolver
            canonical = url if _test_hls_allowed(url) else normalize_youtube_url(url)["url"]
            s.info = {"id": sid, "url": canonical, "key": key, "name": (name or "").strip()[:80], "state": "starting",
                      "settings": st, "started_at": now_iso(), "run": 0, "reconnects": 0, "errors": [],
                      "analyzed_until": 0.0, "first_t": 0.0}
            s.save_info()
            self.sessions[sid] = s
        self._launch(s)
        return s

    def _launch(self, s: Session) -> None:
        s.stop_flag.clear()
        s.pause_flag.clear()
        rec = Recorder(s, on_segment=lambda *_: self.wake.set())
        s.thread = threading.Thread(target=rec.run, name=f"liverec-{s.id}", daemon=True)
        s.thread.start()
        self.ensure_analysis()

    def stop(self, sid: str, wait: float = 15.0) -> Session:
        s = self.get(sid)
        s.stop_flag.set()
        s.pause_flag.clear()
        if s.info.get("state") in ("interrupted", "paused") and not (s.thread and s.thread.is_alive()):
            s.set_state("stopped", end_reason="Stopped by you.")
        if s.thread and wait:
            s.thread.join(timeout=wait)
        self.wake.set()
        return s

    def pause(self, sid: str) -> Session:
        s = self.get(sid)
        if s.info.get("state") not in ACTIVE_STATES:
            raise SharkyClipsError("This session isn't recording.")
        s.pause_flag.set()
        return s

    def resume(self, sid: str) -> Session:
        s = self.get(sid)
        with self.lock:
            for o in self.sessions.values():
                if o is not s and o.info.get("key") == s.info.get("key") and o.info.get("state") in ACTIVE_STATES:
                    raise SharkyClipsError("Another session is already recording this stream.")
        if s.thread and s.thread.is_alive():
            s.pause_flag.clear()
            return s
        s.info["end_reason"] = None
        self._launch(s)
        return s

    # ------------------------------------------------------------------ analysis thread

    def ensure_analysis(self) -> None:
        with self.lock:
            if self._analysis and self._analysis.is_alive():
                return
            self._analysis = threading.Thread(target=self._analysis_loop, name="liverec-analysis", daemon=True)
            self._analysis.start()

    def _analysis_loop(self) -> None:
        ffmpeg = require_tool("ffmpeg")
        while not self._closing:
            self.wake.wait(timeout=5.0)
            self.wake.clear()
            for s in list(self.sessions.values()):
                try:
                    if s.analyzed_until < s.recorded - 1e-6:
                        before = len(s.candidates)
                        s.analyze_pending(ffmpeg)
                        s.apply_retention()  # analyzed segments may now leave the rolling buffer
                        if len(s.candidates) != before:
                            for cb in self.on_candidates:
                                cb(s)
                except Exception as exc:  # noqa: BLE001 - analysis problems never stop recording
                    s.error(f"analysis: {exc}")

    def analyze_now(self, sid: str) -> int:
        """Synchronous analysis (tests / CLI)."""
        return self.get(sid).analyze_pending()

    def close(self) -> None:
        self._closing = True
        for s in self.sessions.values():
            if s.thread and s.thread.is_alive():
                s.stop_flag.set()
        for s in self.sessions.values():
            if s.thread:
                s.thread.join(timeout=15)
        self.wake.set()

    # ------------------------------------------------------------------ backfill (best effort)

    def start_backfill(self, sid: str) -> dict:
        """EXPERIMENTAL: yt-dlp --live-from-start in its own process, into <session>/backfill/.
        Only works when the stream has DVR enabled and YouTube still serves the early part.
        Its failure never touches the recording from now."""
        s = self.get(sid)
        if _test_hls_allowed(s.info.get("url", "")):
            raise SharkyClipsError("Backfill needs a YouTube stream.")
        if s.backfill_proc and s.backfill_proc.poll() is None:
            raise SharkyClipsError("Backfill is already running.")
        out_dir = s.dir / "backfill"
        out_dir.mkdir(exist_ok=True)
        h = int((s.info.get("settings") or settings())["quality"])
        cmd = [sys.executable, "-m", "yt_dlp", "--live-from-start", "--no-playlist", "--newline",
               "--no-cookies-from-browser", "-f", f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b",
               "--merge-output-format", "mp4", "--retries", "10", "--fragment-retries", "10",
               "-o", str(out_dir / "backfill.%(ext)s"), s.info["url"]]
        from .ingest import js_runtimes

        for name, rt in js_runtimes().items():
            cmd[3:3] = ["--js-runtimes", f"{name}:{rt['path']}"]
            break
        log = open(out_dir / "yt-dlp.log", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        s.backfill_proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                           **no_window())
        s.info["backfill"] = {"state": "running", "started_at": now_iso(), "experimental": True,
                              "note": "Best effort: works only if the stream has DVR and YouTube still "
                                      "serves its start. It may fail or overlap the recording from now."}
        s.save_info()
        threading.Thread(target=self._watch_backfill, args=(s, log), daemon=True).start()
        return s.info["backfill"]

    def _watch_backfill(self, s: Session, log) -> None:
        proc = s.backfill_proc
        assert proc is not None
        rc = proc.wait()
        log.close()
        text = _tail(s.dir / "backfill" / "yt-dlp.log", 2000)
        files = [f for f in (s.dir / "backfill").glob("backfill.*") if f.suffix not in (".part", ".ytdl", ".log")
                 and not f.name.endswith(".part")]
        bf = dict(s.info.get("backfill") or {})
        if rc == 0 and files:
            bf.update(state="finished", file=files[0].name, size=files[0].stat().st_size)
        elif bf.get("state") == "cancelled":
            pass
        else:
            low = text.lower()
            reason = ("This stream has no DVR / live-from-start is not available." if
                      ("live from start" in low or "dvr" in low or "not supported" in low) else
                      "yt-dlp could not download the earlier part.")
            bf.update(state="failed", reason=reason, log_tail=text[-600:])
        bf["ended_at"] = now_iso()
        s.info["backfill"] = bf
        s.save_info()

    def cancel_backfill(self, sid: str) -> dict:
        s = self.get(sid)
        if s.backfill_proc and s.backfill_proc.poll() is None:
            s.info["backfill"] = {**(s.info.get("backfill") or {}), "state": "cancelled"}
            s.backfill_proc.terminate()
        return s.info.get("backfill") or {}


_manager: Manager | None = None
_mlock = threading.Lock()


def manager() -> Manager:
    global _manager
    with _mlock:
        if _manager is None:
            _manager = Manager()
        return _manager


def reset_manager_for_tests() -> None:
    global _manager
    with _mlock:
        if _manager is not None:
            _manager.close()
        _manager = None


def tool_status() -> dict:
    """What the live recorder can use on this PC (doctor / UI)."""
    out: dict[str, Any] = {"ffmpeg": shutil.which("ffmpeg"), "streamlink": shutil.which("streamlink")}
    try:
        import yt_dlp

        out["yt_dlp"] = yt_dlp.version.__version__
    except Exception:  # noqa: BLE001
        out["yt_dlp"] = None
    from .ingest import js_runtimes

    out["js_runtimes"] = sorted(js_runtimes())
    return out


def estimate_bytes(hours: float, mbps: float) -> float:
    """Disk use of a recording: hours at an average bitrate (decimal GB = bytes / 1e9)."""
    return hours * 3600 * mbps * 1e6 / 8


if __name__ == "__main__":  # pragma: no cover
    print(json.dumps(tool_status(), indent=2))
