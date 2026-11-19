"""Local fork: "trim first" - pick the part of a long video to clip, then the analysis starts."""

from __future__ import annotations

import subprocess
import time

import pytest

from sharkyclips import config
from sharkyclips.errors import require_tool
from sharkyclips.project import Project, write_json_atomic


def _video(path, seconds: int) -> None:
    subprocess.run([require_tool("ffmpeg"), "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=30:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:v", "libx264", "-g", "60", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)],
                   check=True, capture_output=True, timeout=120)


def _wait(jobs, jid: str, timeout: float = 120):
    end = time.time() + timeout
    while jobs.get(jid).status in ("queued", "running"):
        assert time.time() < end, "job timed out"
        time.sleep(0.1)
    return jobs.get(jid)


@pytest.mark.requires_ffmpeg
def test_trim_keeps_the_picked_part_and_then_transcribes(monkeypatch):
    from fastapi.testclient import TestClient

    from sharkyclips.audio import probe
    from sharkyclips.web import app as A

    pid = "long-stream-trim1234"
    d = config.workspace_dir() / pid
    d.mkdir(parents=True)
    _video(d / "source.mp4", 240)
    (d / "audio.wav").write_bytes(b"RIFF")  # the old audio of the whole video
    (d / "chat.json").write_text("{}")
    write_json_atomic(d / "meta.json", {"id": pid, "title": "Long stream", "mode": "gaming", "duration": 240.0,
                                        "awaiting_trim": True, "import_language": "en", "clip_style": "streamer"})
    started = []
    monkeypatch.setattr(A, "submit_transcribe", lambda p, lang="auto", *a, **k: started.append((p, lang)))
    c = TestClient(A.app)
    waiting = c.get("/api/gaming/trim").json()["videos"]
    assert [v["id"] for v in waiting] == [pid] and waiting[0]["duration"] == 240.0
    est = c.get("/api/gaming/estimate", params={"minutes": 2, "style": "both"}).json()
    assert est["clips"] == 2 * est["stories"] and est["total_s"] > 0
    assert c.post(f"/api/gaming/trim/{pid}", json={"start": 0, "end": 30}).status_code >= 400  # under 1 minute
    j = c.post(f"/api/gaming/trim/{pid}", json={"start": 60, "end": 150}).json()
    done = _wait(A.jobs, j["id"])
    assert done.status == "done", done.error
    p = Project(pid, d)
    m = p.load_meta()
    assert "awaiting_trim" not in m and m["trimmed"] == {"start": 60.0, "end": 150.0, "original_duration": 240.0}
    assert 85 <= float(m["duration"]) <= 95  # stream copy: starts on the keyframe before 60 s
    v, a = probe(d / "source.mp4"), probe(d / "audio.wav")
    assert abs(float(v["duration"]) - float(a["duration"])) < 0.5  # the new audio matches the cut video
    assert not (d / "chat.json").exists()
    assert started == [(pid, "en")]
    assert c.get("/api/gaming/trim").json()["videos"] == []


def test_link_preview_cuts_the_right_picture_from_the_storyboard(monkeypatch):
    import io
    import time as _t

    from PIL import Image

    from sharkyclips import linkpreview as L

    # a 3x3 sprite sheet: tile i is filled with gray level 20*i; two sheets of 90 s (a picture every 10 s)
    def sheet(base):
        im = Image.new("RGB", (3 * 32, 3 * 18))
        for i in range(9):
            im.paste((20 * (i + base),) * 3, ((i % 3) * 32, (i // 3) * 18, (i % 3 + 1) * 32, (i // 3 + 1) * 18))
        b = io.BytesIO()
        im.save(b, "PNG")
        return b.getvalue()

    sheets = {0: sheet(0), 1: sheet(3)}
    monkeypatch.setattr(L, "_sheet", lambda token, n, url, headers: sheets[n])
    L._links["tok"] = {"url": "u", "at": _t.time(), "duration": 180.0, "stream": {
        "frags": [{"url": "https://x/0", "duration": 90.0}, {"url": "https://x/1", "duration": 90.0}],
        "rows": 3, "cols": 3, "w": 32, "h": 18, "headers": {}}}

    def gray(t):
        im = Image.open(io.BytesIO(L.frame("tok", t))).convert("L")
        return im.getpixel((im.width // 2, im.height // 2))

    assert abs(gray(5) - 0) < 8          # sheet 0, tile 0
    assert abs(gray(45) - 80) < 8        # sheet 0, tile 4
    assert abs(gray(95) - 60) < 8        # sheet 1, tile 0 (base 3 -> 60)
    assert abs(gray(179) - 220) < 8      # sheet 1, tile 8
    L._links.pop("tok", None)
