"""End-to-end smoke test of the LIVE path through the real web API (local fixture only).

    source scripts/env.sh
    python scripts/smoke_live_api.py [samples/synthetic_gta_120s.mp4] [--live-speed]

What it does (nothing leaves this PC):
 1. turns the synthetic fixture into HLS and serves it on 127.0.0.1 (a stand-in for YouTube;
    allowed only because this script sets SHARKYCLIPS_LIVE_TEST_HLS=1 for its own server),
    with --live-speed the playlist grows in real time like a real live stream (ffmpeg -re),
 2. starts a separate SharkyClips server on a free port with a throwaway settings folder,
 3. POST /api/liverec/sessions, waits for segments, lets the analysis find the spikes,
 4. "Clip last 30 s" + "Make clip" on a found candidate, both with render=true,
 5. waits for the job chain (assemble -> import -> Whisper -> highlights -> facecam -> render)
    and checks the rendered MP4s with ffprobe (1080x1920, H.264, AAC, duration, audio level),
 6. writes a JSON report + a contact sheet PNG next to the outputs and prints the paths.
It does NOT prove that a real YouTube live stream works (that needs an authorized stream).
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base: str, method: str, path: str, body=None, token: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **({"X-SharkyClips-Token": token} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def main() -> int:
    fixture = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else \
        REPO / "samples" / "synthetic_gta_120s.mp4"
    live_speed = "--live-speed" in sys.argv
    if not fixture.is_file():
        print(f"missing fixture {fixture}: python scripts/make_synthetic_stream.py {fixture}")
        return 2
    work = Path(tempfile.mkdtemp(prefix="sc-live-smoke-"))
    hls = work / "hls"
    hls.mkdir()
    feeder = None
    if live_speed:  # a growing EVENT playlist, like a live broadcast
        feeder = subprocess.Popen(["ffmpeg", "-v", "error", "-re", "-i", str(fixture), "-c", "copy", "-f", "hls",
                                   "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "event",
                                   "-hls_segment_filename", str(hls / "s_%04d.ts"), str(hls / "index.m3u8")])
        while not (hls / "index.m3u8").is_file():
            time.sleep(0.2)
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(fixture), "-c", "copy", "-f", "hls", "-hls_time", "2",
                        "-hls_list_size", "0", "-hls_playlist_type", "vod", "-hls_segment_filename",
                        str(hls / "s_%04d.ts"), str(hls / "index.m3u8")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(hls))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hls_url = f"http://127.0.0.1:{srv.server_address[1]}/index.m3u8"

    home = work / "home"
    (home / "workspace").mkdir(parents=True)
    real_models = REPO / ".sharkyclips" / "models"
    if real_models.is_dir():  # reuse the downloaded laughter / face models
        os.symlink(real_models, home / "models")
    (home / "config.json").write_text(json.dumps({"workspace_dir": str(home / "workspace"), "llm_enabled": False,
                                                  "auto_analyze": False, "onboarding_done": True}))
    port = free_port()
    env = {**os.environ, "SHARKYCLIPS_HOME": str(home), "SHARKYCLIPS_LIVE_TEST_HLS": "1",
           "SHARKYCLIPS_NO_MIGRATE": "1", "SHARKYCLIPS_PUBLISH_LOOP": "0", "SHARKYCLIPS_NO_GOVERNOR": "1"}
    log = open(work / "server.log", "w")  # noqa: SIM115
    server = subprocess.Popen([sys.executable, "cli.py", "serve", "--no-browser", "--port", str(port)],
                              cwd=REPO / "backend", env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    report: dict = {"fixture": str(fixture), "work": str(work), "live_speed": live_speed}
    try:
        for _ in range(120):
            try:
                call(base, "GET", "/api/meta")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        token = (home / "api_token").read_text().strip() if (home / "api_token").is_file() else None
        s = call(base, "POST", "/api/liverec/sessions", {"url": hls_url, "name": "smoke"}, token)
        sid = s["id"]
        t0 = time.time()
        while time.time() - t0 < 300:
            s = call(base, "GET", f"/api/liverec/sessions/{sid}?timeline=0")
            if s["state"] in ("ended", "failed", "stopped") or (s["recorded_s"] >= 60 and s["candidates"]):
                break
            time.sleep(1)
        report["session_while_recording"] = {k: s[k] for k in ("state", "recorded_s", "segments", "reconnects",
                                                                 "analysis", "bytes")}
        jobs = [call(base, "POST", f"/api/liverec/sessions/{sid}/clip-last", {"seconds": 30, "render": True}, token)]
        cand = next((c for c in s["candidates"] if c["kind"] == "auto"), None)
        if cand:
            jobs.append(call(base, "POST", f"/api/liverec/sessions/{sid}/candidates/{cand['id']}/clip",
                             {"render": True}, token))
        report["candidates"] = [{k: c[k] for k in ("id", "start", "end", "peak", "score", "reasons")}
                                for c in s["candidates"]]
        # wait for every job (and the render jobs they start) to finish
        t0 = time.time()
        while time.time() - t0 < 900:
            js = call(base, "GET", "/api/jobs")
            js = js.get("jobs", js) if isinstance(js, dict) else js
            active = [j for j in js if j["status"] in ("queued", "running")]
            renders = [j for j in js if j["kind"] == "render" and j["status"] == "done"]
            if not active and len(renders) >= len(jobs):
                break
            time.sleep(2)
        report["jobs"] = [{k: j.get(k) for k in ("id", "kind", "status", "error", "project_id")} for j in js]
        if feeder is None:
            call(base, "POST", f"/api/liverec/sessions/{sid}/stop", {}, token)
        outs = []
        for mp4 in sorted((home / "workspace").glob("*/clips/*.mp4")):
            info = probe(mp4)
            vol = subprocess.run(["ffmpeg", "-v", "info", "-i", str(mp4), "-af", "volumedetect", "-f", "null", "-"],
                                 capture_output=True, text=True).stderr
            mean = float(vol.split("mean_volume:")[1].split("dB")[0]) if "mean_volume:" in vol else None
            thumb = mp4.with_suffix(".png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", str(mp4), "-frames:v", "1", "-vf",
                            "scale=270:480", str(thumb)], check=True)
            outs.append({"file": str(mp4), "probe": info, "mean_volume_db": mean, "thumb": str(thumb)})
        report["outputs"] = outs
        ok = bool(outs) and all(
            any(st.get("width") == 1080 and st.get("height") == 1920 and st.get("codec_name") == "h264"
                for st in o["probe"]["streams"])
            and any(st.get("codec_name") == "aac" for st in o["probe"]["streams"])
            and (o["mean_volume_db"] or -99) > -60 for o in outs)
        report["ok"] = ok
        if outs:
            sheet = work / "contact_sheet.png"
            args = ["ffmpeg", "-v", "error", "-y"]
            for o in outs:
                args += ["-i", o["thumb"]]
            args += ["-filter_complex", f"hstack=inputs={len(outs)}" if len(outs) > 1 else "null", str(sheet)]
            subprocess.run(args, check=True)
            report["contact_sheet"] = str(sheet)
        s = call(base, "GET", f"/api/liverec/sessions/{sid}?timeline=0")
        report["session_final"] = {k: s[k] for k in ("state", "end_reason", "recorded_s", "segments", "deleted_segments",
                                                      "reconnects", "errors")}
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        if feeder:
            feeder.terminate()
        srv.shutdown()
        log.close()
    out = work / "report.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report.get(k) for k in ("ok", "contact_sheet", "work")}, indent=2))
    print("report:", out)
    keep = REPO / "docs" / "evidence"
    keep.mkdir(parents=True, exist_ok=True)
    shutil.copy(out, keep / ("live_api_smoke_report" + ("_livespeed" if live_speed else "") + ".json"))
    if report.get("contact_sheet"):
        shutil.copy(report["contact_sheet"], keep / ("live_api_smoke_sheet" + ("_livespeed" if live_speed else "") + ".png"))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
