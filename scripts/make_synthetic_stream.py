"""Synthetic GTA-style "gaming stream" fixture (no third-party content).

Everything is generated: the "game" is drawn by ffmpeg (moving gradient city, a grid, moving
"cars", a minimap and a HUD), the facecam is a bordered box in a corner with the SharkyClips logo
bobbing inside it, and the audio is a quiet ambient bed plus controlled "reaction" spikes
(a harmonic, formant-modulated, voice-like shout + an explosion thump). No speech synthesizer is
needed, so transcripts will be (nearly) empty: render tests use manual candidates, audio scoring
is tested with the spikes.

    python scripts/make_synthetic_stream.py OUT.mp4 [--duration 120] [--size 1280x720]
        [--spikes 30,75] [--corner br|bl|tr|tl] [--hls DIR --segment 4]

--hls DIR additionally writes DIR/index.m3u8 + .ts segments (a VOD-style HLS stream that a
local http server can serve to the live recorder tests).

The sidecar OUT.json records the facecam rectangle (normalized) and the spike times, so tests can
check split positions and detection against ground truth.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

SR = 48000
REPO = Path(__file__).resolve().parents[1]
LOGO = REPO / "assets" / "icon" / "sharkyclips.png"


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise SystemExit("ffmpeg not found on PATH (source scripts/env.sh)")
    return exe


def make_audio(path: Path, duration: float, spikes: list[float], seed: int = 7) -> None:
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    t = np.arange(n) / SR
    mix = np.zeros(n, dtype=np.float32)
    # ambient "city" bed: low hum + soft noise (quiet on purpose: spikes must stand out)
    mix += (0.012 * (np.sin(2 * np.pi * 60 * t) + 0.5 * np.sin(2 * np.pi * 90 * t))).astype(np.float32)
    mix += (rng.standard_normal(n) * 0.004).astype(np.float32)
    # calm "talking" every few seconds: short low-level harmonic syllables
    for s in np.arange(2.0, duration - 2, 3.3):
        i, m = int(s * SR), int(1.2 * SR)
        tt = np.arange(m) / SR
        f0 = 120 + 15 * np.sin(2 * np.pi * 3 * tt)
        syl = (0.5 + 0.5 * np.sin(2 * np.pi * 4.5 * tt)) ** 2
        v = sum(np.sin(2 * np.pi * f0 * k * tt) / k for k in range(1, 6)) * syl * 0.03
        mix[i:i + m] += v.astype(np.float32)[: n - i]
    for s in spikes:  # the reaction: explosion thump + a ~3 s excited "shout"
        i, m = int(s * SR), int(0.9 * SR)
        env = np.exp(-np.arange(m) / (0.18 * SR)).astype(np.float32)
        thump = np.sin(2 * np.pi * 48 * np.arange(m) / SR).astype(np.float32)
        mix[i:i + m] += ((0.35 * rng.standard_normal(m).astype(np.float32) + 0.5 * thump) * env)[: n - i]
        j, k = int((s + 0.3) * SR), int(3.0 * SR)
        tt = np.arange(k) / SR
        f0 = 260 + 80 * np.sin(2 * np.pi * 1.7 * tt)  # high, wobbling pitch = shouting
        syl = (0.55 + 0.45 * np.sin(2 * np.pi * 6 * tt)) ** 2
        shout = sum(np.sin(2 * np.pi * f0 * h * tt) / h for h in range(1, 9)) * syl
        shout += 0.15 * rng.standard_normal(k)
        shout *= np.minimum(1, tt / 0.05) * np.minimum(1, (3.0 - tt) / 0.3)
        mix[j:j + k] += (0.32 * shout).astype(np.float32)[: n - j]
    mix = np.clip(mix, -1, 1)
    pcm = (np.stack([mix, mix], axis=1) * 32000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


CORNERS = {"br": ("W-w-{m}", "H-h-{m}"), "bl": ("{m}", "H-h-{m}"), "tr": ("W-w-{m}", "{m}"), "tl": ("{m}", "{m}")}


def facecam_rect(W: int, H: int, corner: str) -> tuple[int, int, int, int]:
    cw, ch = int(W * 0.22) // 2 * 2, int(H * 0.25) // 2 * 2  # a "tiny" cam: 22% x 25% of the frame
    m = int(W * 0.015)
    x = W - cw - m if corner in ("br", "tr") else m
    y = H - ch - m if corner in ("br", "bl") else m
    return x, y, cw, ch


def make_video(out: Path, audio: Path, duration: float, W: int, H: int, corner: str, spikes: list[float],
               fps: int = 30, hls: Path | None = None, segment: float = 4.0) -> dict:
    x, y, cw, ch = facecam_rect(W, H, corner)
    s = W / 1280
    flash = "+".join(f"between(t\\,{b:.2f}\\,{b + 0.15:.2f})" for b in spikes) or "0"
    cars = "".join(
        f",drawbox=x='mod({x0}+{v}*t\\,{W})':y={int(y0 * s)}:w={int(70 * s)}:h={int(34 * s)}:color={c}@0.9:t=fill"
        for x0, v, y0, c in [(0, 140, 430, "0xff3d81"), (500, -90, 520, "0x2de2e6"), (900, 210, 600, "0xf9c80e")])
    game = (f"gradients=s={W}x{H}:c0=0x1d3b2a:c1=0x3a5f2b:c2=0x6b6b6b:c3=0x2a2f45:nb_colors=4:speed=0.01:r={fps}:d={duration},"
            f"drawgrid=w={int(96 * s)}:h={int(96 * s)}:t=2:c=0x222222@0.45{cars},"
            # minimap bottom-left (GTA-like HUD) and a wanted-stars bar top-right
            f"drawbox=x={int(24 * s)}:y={H - int(190 * s)}:w={int(220 * s)}:h={int(160 * s)}:color=0x0a0a0a@0.7:t=fill,"
            f"drawbox=x={int(24 * s)}:y={H - int(190 * s)}:w={int(220 * s)}:h={int(160 * s)}:color=white@0.8:t=2,"
            f"drawbox=x={int(W * 0.62)}:y={int(24 * s)}:w={int(W * 0.3)}:h={int(28 * s)}:color=0xf9c80e@0.85:t=fill,"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=white@0.6:t=fill:enable='{flash}'[game]")
    lw = min(cw, ch) * 2 // 3 // 2 * 2
    cam = (f"color=c=0x101828:s={cw}x{ch}:r={fps}:d={duration}[cbg];[1:v]scale={lw}:{lw}[logo];"
           f"[cbg][logo]overlay=x=(W-w)/2:y='(H-h)/2+{max(2, ch // 30)}*sin(t*3)'[c0];"
           f"[c0]drawbox=x=0:y=0:w={cw}:h={ch}:color=0x7f5af0:t={max(3, int(4 * s))}[cam]")
    graph = f"{cam};{game};[game][cam]overlay=x={x}:y={y}[v]"
    cmd = [_ffmpeg(), "-y", "-loglevel", "error", "-i", str(audio), "-loop", "1", "-i", str(LOGO),
           "-filter_complex", graph, "-map", "[v]", "-map", "0:a", "-t", f"{duration}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-g", str(int(fps * 2)), "-keyint_min", str(int(fps * 2)), "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    if hls is not None:
        hls.mkdir(parents=True, exist_ok=True)
        subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(out), "-c", "copy", "-f", "hls",
                        "-hls_time", f"{segment}", "-hls_list_size", "0", "-hls_playlist_type", "vod",
                        "-hls_segment_filename", str(hls / "seg_%05d.ts"), str(hls / "index.m3u8")], check=True)
    return {"width": W, "height": H, "fps": fps, "duration": duration, "corner": corner, "spikes": spikes,
            "facecam_px": [x, y, cw, ch],
            "facecam": {"x": round(x / W, 4), "y": round(y / H, 4), "w": round(cw / W, 4), "h": round(ch / H, 4)}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--spikes", default="30,75")
    ap.add_argument("--corner", default="br", choices=sorted(CORNERS))
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--hls", type=Path)
    ap.add_argument("--segment", type=float, default=4.0)
    a = ap.parse_args()
    W, H = (int(v) for v in a.size.lower().split("x"))
    spikes = [float(v) for v in a.spikes.split(",") if v.strip()]
    a.out.parent.mkdir(parents=True, exist_ok=True)
    wav = a.out.with_suffix(".wav")
    make_audio(wav, a.duration, spikes)
    info = make_video(a.out, wav, a.duration, W, H, a.corner, spikes, a.fps, a.hls, a.segment)
    wav.unlink(missing_ok=True)
    a.out.with_suffix(".json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(json.dumps(info))


if __name__ == "__main__":
    main()
