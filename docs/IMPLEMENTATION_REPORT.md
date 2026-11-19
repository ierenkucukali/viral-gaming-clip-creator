# Implementation report — local gaming / live clipper (2026-11-18)

Upstream: `utkualsir/sharkyclips` @ `0927c49a6eaf25f5db496113abc4c88945479100` (see `UPSTREAM_BASELINE.md`).
Branch `feat/local-gaming-live-clipper`, **uncommitted** working tree (nothing pushed, no tags/releases).
`git diff 0927c49a6e` shows every change.

## Start next time

```bash
cd ~/Desktop/gaming-clip && ./scripts/start.sh        # http://127.0.0.1:8000 (127.0.0.1 only)
source scripts/env.sh && cd backend && python cli.py doctor --no-fix
```

## Files

New:
- `backend/sharkyclips/liverec.py` — live recorder: YouTube URL allowlist, yt-dlp resolve (Streamlink fallback),
  `ffmpeg -c copy` 6 s MPEG-TS segments, segment index (session time + UTC wall clock), reconnect with backoff,
  stream-end detection (`#EXT-X-ENDLIST` / not live), pause/resume, crash recovery of orphan segments,
  rolling/archive retention with pins, low/critical disk handling, CPU audio spike analysis of completed
  segments in its own thread, mark moment, cross-segment window assembly, backfill (`--live-from-start`).
- `backend/sharkyclips/web/liverec_api.py` — REST API + bridge: live window → ordinary project
  (io lane) → Whisper + highlights + manual clip + facecam (serial GPU lane) → optional render;
  `/api/gaming/settings`; `/api/gaming/export-selected` (reproducible names in `exports/<date>/`).
- `frontend/src/pages/Gaming.tsx` — Gaming page: Local file / YouTube VOD / YouTube LIVE cards, live session
  panel (state, elapsed, recorded, disk, reconnects, timeline, candidates + why, preview, clip last
  30/60/90/120, mark, retention, backfill, finalize), clip-shape tuning, export.
- `backend/tests/test_local_gaming.py` (28 tests), `scripts/make_synthetic_stream.py`, `scripts/env.sh`,
  `scripts/start.sh`, `scripts/add_i18n.py`, `scripts/smoke_live_api.py`, docs.

Changed:
- `clips.py` — `SplitSpec` + options `split_order` (top/bottom), `split_ratio` (0.25–0.40, default 0.30),
  `game_fit` (crop/blur/letterbox), `game_offset`, `hide_cam` (blurs the original overlay in the gameplay pane);
  captions/banner follow the panel seam. `edit/fx.py`, `web/studio.py` use the seam / spec.
- `frontend/src/previewMath.ts`, `components/VerticalPreview.tsx`, `pages/Project.tsx` — preview draws the same
  crops; split controls in the render panel. i18n keys in all 7 locales (en + tr translated, others English).
- `highlights/modes.py` (config `mode_tuning`, clamped), `highlights/pipeline.py` (`quality` good/weak per
  candidate, `summary` vs `daily_target`, `candidate_count`), `config.py` defaults (15/30/60 s, pre-roll 6–12 s),
  `triggers/en.json` + `tr.json` new `gta` category (cops, wasted, busted, crash, NPC… / polis, kaza, ezdim…).
- `ingest.py` — 4 bounded download attempts with backoff (login walls not retried), live link hint.
- `cli.py render` — `--cam-pos --cam-ratio --game-fit --game-offset --show-cam-twice`.
- `doctor.py` — Streamlink, free disk, "no paid APIs" rows. `web/app.py` router + startup hook. `.gitignore`
  (`tools/`, `samples/`). `frontend/dist` rebuilt.

## Tests (actual results)

| Check | Result |
|---|---|
| Baseline pytest (clean upstream worktree) | all passed, 0 failed, 10 skipped |
| Full pytest after changes | 1 failure found (`test_web::test_projects_empty`, caused by my e2e test leaving a project in the shared test workspace) → fixed; `test_local_gaming` + `test_web` + `test_preview_math` re-run: all pass. The full suite was not re-run a second time after that one-line fix. |
| ruff | All checks passed |
| Frontend `tsc -b`, `npm run build`, `check-i18n` | OK (2821 keys) |
| New tests | normalized cam coords, top/bottom pixel layout test (1080x1920), fit/offset, TS-preview == renderer (108 shapes × 3 sources), URL allowlist, wall-clock→stream time (gaps, no negatives), retention/pins/unanalyzed/newest, low/critical disk, CSV half-line, spike pre-roll/wait/dedupe, mode clamp, weak labels, GPU→CPU fallback chain, interrupted download retried / login wall not retried, cross-segment assembly with exact offset, crash recovery, duplicate recorder refused, **integration**: synthetic stream → localhost HLS → recorder → analysis (spike at 12 s found) → assemble → project → render → ffprobe 1080x1920 H.264/AAC, duration, audio > −60 dB, facecam panel on top by pixel colour |

Manual evidence (synthetic fixture `samples/synthetic_gta_120s.mp4`, spikes at 30 s / 75 s):
- CLI ingest → transcribe (GPU, 14 s) → highlights: 2 candidates 0:25–0:49 and 1:10–1:35 (`loudness_spike +23 dB`).
- Renders (NVENC, 5.9 s each): see `docs/evidence/ffprobe_split_renders.txt` (all `h264 1080x1920 aac 25.5 s`) and
  `docs/evidence/split_top_crop_vs_bottom_blur.png`.
- Export: `exports/2026-11-18/synthetic-gta-120s-75f30aa9_c01_00h01m10s-00h01m35s.mp4`.
- Live smoke (Python API, local HLS): 20 segments, 120.07 s, state `ended`, candidates 20–42 s and 65–87 s.

## UNTESTED / limitations (honest)

- **No real YouTube live stream or YouTube VOD was connected** (no authorized stream was provided). yt-dlp/
  Streamlink resolution, YouTube HLS headers/expiry, real reconnects and `--live-from-start` backfill are
  implemented but only tested with mocks / localhost HLS.
- The web API job chain for live clips (`/clip-last`, `/candidates/{id}/clip` → import → Whisper → render) and
  `/finalize` were not run end-to-end through HTTP; `scripts/smoke_live_api.py` does that but was **not executed**.
  The same functions are covered by the pytest integration test at the Python level.
- The Gaming page UI was built and type-checked, but not clicked through in a browser (no Playwright run).
- No speech in the fixture (no TTS on this PC): keyword/Whisper-based signals were not exercised with real voice.
- Optional 0.5–1 fps visual motion signal on top candidates: **not implemented**.
- Live analysis is loudness/voice-band only (cheap CPU); Whisper/LLM run per clip, not continuously.
- Stream-copy assembly uses whole 6 s segments, so live clip projects contain up to ~12 s extra around the window
  (the exact window is set as the clip; render is frame-accurate).
- Non-en/tr locales show English for the new strings.

## Update 2026-11-19 — story clips, Instagram headline, cloud AI, per-clip facecam

- **Story clips** (`storyfind.py`): the AI reads the whole transcript as numbered lines with the signals written next to
  them (HYPE n, laugh, shout, loud +n dB, chat spike) and returns complete stories (setup -> peak -> payoff) of
  45-120 s with type, score, "why" and a 3-6 word English headline + one emoji. Cloud models read a 5-hour stream in
  one request; the local model reads 10-minute windows with 2-minute overlap. Stories replace the signal candidates;
  strong signal moments the AI skipped stay as extra candidates. AI off / failing -> the signal candidates stay.
  Keywords alone no longer pick clips (the `gta` keyword category is out of the default gaming mode).
- **AI provider** (`llmcloud.py`, Gaming page card): Local (Ollama) | Claude API | Gemini API. Keys encrypted in
  `<settings>/ai_keys.sec` (secretstore), write-only in the API. Only transcript text + prompts are sent. Quota /
  auth / network errors fall back to the local model (switchable). Claude: `output_config.format` JSON schema,
  default `claude-opus-5-5` (Sonnet 5.5 / Haiku 5.5 selectable). Gemini: `responseJsonSchema`, model "auto" picks
  the best Flash the key can use, daily-quota 429 recognised.
- **Local model fix**: Ollama 0.30.8 does not enforce `format` for Qwen 3.5 (it answered with other keys: the reason
  the earlier analysis failed on every clip). The schema is now also written into the prompt; lenient JSON parsing.
  Measured on the 20-min GTA 6 video: qwen3.5:4b (fits the 6 GB GPU) 45 tok/s, 7 stories in ~90 s;
  qwen3.5:9b only 53 % on the GPU, 3 tok/s -> too slow on this laptop.
- **Instagram-style headline** (`edit/headline.py`, `banner_style="ig"` default): white rounded box, black bold text,
  colour emoji (Pillow PNG overlay), whole clip, centred on the facecam/gameplay line; captions move into the
  gameplay panel. Each story clip uses its own headline when the banner box is empty (render + preview).
- **Per-clip facecam** (`facecam.for_window`, `clips.clip_facecam`): 16 frames inside the clip (padded to 60 s) are
  analysed; a confident box found there wins (moved / resized camera), else the video's box if a live face is in it,
  else no camera (gameplay only). A box drawn by hand is never changed. Config `facecam_per_clip`.
  Limitation: the live preview still shows the video-level box when a clip's box differs.
- **Layout default**: facecam panel 1/3 of the height (640 px), gameplay 2/3.
- **English**: `ui_language` and `output_language` set to `en` in this PC's settings.
- Tests: `test_storyfind.py` (11), `test_headline.py` (10, by the headline sub-task), affected upstream tests pass.

## Update 2026-11-19 (2) — two clip modes, auto-post, verified on a real stream VOD

Verified end to end on `https://www.youtube.com/watch?v=kR8bhkVAgBY` ("iShowSpeed's First Time Playing GTA 6",
34:50, 1080p60, streamer camera on the left) through the running app (HTTP API, not just Python):

- **Streamer mode**: facecam found automatically (x 0, y 0.42, w 0.20, h 0.23, confidence 0.97) although the
  game shows many faces; story finder (local qwen3.5:4b) found 9 stories; 3 rendered: 1080x1920 H.264/AAC 60 fps,
  31-49 s, audio and video both start at 0.000 s and end within 0.01 ms. Frames: `docs/evidence/ishowspeed_streamer_mode.jpg`.
- **Direct mode** (new, `clipstyle.py`): no facecam detection at all, layout "fit" on black, headline right above the
  16:9 picture, captions below; 7 stories; 3 rendered 59-89 s, A/V in sync. Frames: `docs/evidence/ishowspeed_direct_mode.jpg`.
- Exports: `exports/2026-11-19/` (6 clips).
- **Story length**: Reels data says 45-60 s works best -> target 55 s, min 30, max 120; stories under score 85
  are capped at 1.6x the target (88 s).
- **Auto-post** (`autopost.py`, Gaming page card, off by default): best clips first, N per day per platform
  (max 3, upstream cap), at your posting times. YouTube + TikTok post automatically; Instagram stays reminder-only
  (upstream switched Meta automation off after an account restriction). See `docs/PUBLISHING_SETUP.md`.
- **Bugs fixed on the way**: (1) upstream deadlock at server start when a queued job is restored (job lock ->
  governor lock vs governor lock -> job lock): `governor.tick` now asks `busy()` outside its lock; (2) the local
  LLM stayed mostly on the CPU for a whole batch when it was loaded while the PC was busy: placement re-checked
  every minute; (3) Ollama ignored the JSON schema for Qwen 3.5 (schema now also in the prompt).
- UI language: the configured language (English) always wins over an old browser choice.
- Costs per 1 h of video: `docs/AI_COSTS.md`.
- Limits (honest): the local qwen3.5:4b finds good stories but often fails the per-clip title step (titles/hashtags);
  use the Claude API for that. Per-clip camera boxes that differ from the video's box are not shown in the live preview.
  No real account was connected and nothing was posted.
- Tests: full backend suite passed (0 failed, 10 skipped) + `test_storyfind` (11), `test_headline` (11),
  `test_autopost` (11); ruff clean; frontend typecheck, i18n check and build OK; headless browser check of the
  Gaming page in English with no console errors.
