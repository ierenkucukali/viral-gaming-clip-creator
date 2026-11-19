# Local setup report (2026-11-18)

Detected on this machine before any feature work (nothing was assumed).

## Hardware / OS

| Item | Found |
|---|---|
| OS | Ubuntu 22.04.5 LTS (Jammy), kernel 6.8.0-106-generic, x86_64 |
| CPU | 11th Gen Intel Core i7-11800H @ 2.30 GHz, 8 cores / 16 threads |
| RAM | 31 GB total (≈14 GB free at the time) |
| Disk | `/home` 663 GB, ≈400 GB free |
| GPU | NVIDIA RTX A3000 Laptop GPU, 6144 MiB VRAM, compute capability 8.6 |
| Driver | 580.126.09 (driver CUDA API 13.0) |
| CUDA libs | pip `nvidia-cublas-cu12` 12.9.3.13 + `nvidia-cudnn-cu12` 9.28.1.4 (no CUDA toolkit needed; the 580 driver runs cu12 runtimes) |

## Executables

| Tool | Status | How |
|---|---|---|
| Python | 3.11.15 (`/usr/bin/python3.11`; 3.12/3.13 not installed) | project `.venv` |
| ffmpeg / ffprobe | **were missing** → static build **n8.1.3** (BtbN FFmpeg-Builds `ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz`, SHA-256 verified against the release's `checksums.sha256`) in `tools/ffmpeg/bin` (gitignored). No sudo, no system change. | `scripts/env.sh` puts it first on PATH |
| Encoders | `h264_nvenc` ✔ (test encode OK), `libx264` ✔, `h264_qsv`, `h264_vaapi`, `hevc_nvenc`, `av1_nvenc` present; `h264_videotoolbox` n/a (macOS only) | SharkyClips picks NVENC |
| ffmpeg filters | `ass`, `subtitles`, `drawtext` (libass / freetype) ✔ | burned-in captions work |
| git | 2.34.1 | |
| Node / npm | v22.21.1 / 10.9.4 (nvm) | frontend build, also yt-dlp's JS runtime |
| Deno | not installed (not needed: yt-dlp uses Node; `bun` is also present) | |
| yt-dlp | 2026.10.26 + `yt-dlp-ejs` 0.8.0 (in `.venv`) | |
| Streamlink | 8.6.3 (in `.venv`, optional fallback) | |
| Ollama | 0.30.8 (already installed system-wide by the user; not modified) | models: `qwen3.5:4b` (pulled by `doctor`), existing `qwen3-vl:2b-instruct` |
| Whisper | faster-whisper 1.2.1 / ctranslate2 4.9.0, CUDA float16, model large-v3-turbo (downloaded by `doctor --download-models`, HF cache) | smoke test on GPU OK |
| Laughter model | CED-mini (sherpa-onnx, CPU) in `.sharkyclips/models` | |

## Upstream install audit

- `install.sh` targets macOS/Homebrew (curl | bash of Homebrew, `brew install ffmpeg-full`): **not run**.
- `install.bat` / `installer/` are Windows: not run.
- `cli.py doctor` was read before running: on Linux `llm.install_command()` returns `None`
  (no system installs); its fixes are `pip install` inside the venv and model downloads only.
- Upstream `CLAUDE.md` release rules (version bump, tag, push, GitHub release, `dev_in_progress.json`
  flag, restarting running servers) were **not** applied: this is a private local checkout.

## Settings location

`scripts/env.sh` sets `SHARKYCLIPS_HOME=<repo>/.sharkyclips` (gitignored) so settings, API token,
facecam profiles and small models stay inside this folder instead of `~/.sharkyclips`. Whisper models
live in the standard Hugging Face cache (`~/.cache/huggingface`).

## Baseline checks (upstream commit 0927c49, clean worktree, before any change)

- `python -m pytest -q`: **all passed, 0 failed, 10 skipped** (skips: platform-specific / optional tools).
- `python -m ruff check backend`: **All checks passed**.
- `python cli.py doctor --no-fix`: all OK; WARN only for "laughter model / LLM model not downloaded yet"
  (then downloaded with `doctor --download-models`, every row OK; qwen3.5:4b first load 103 s, JSON answer 79.5 s).
