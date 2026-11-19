# AI options and cost per 1 hour of video (2026-11-19)

> **Since 1.2.54 the per-clip package (step 2) is off for story clips** (the story finder already
> writes the headline, title and description), so a 1-hour stream costs only the story finder:
> about **$0.25 on Opus 5.5, $0.12 on Sonnet 5.5, a few cents on Haiku 5.5 / Gemini Flash**. The
> table below is the full cost with the package switched on (Gaming → Setup → Clip shape).

## What the AI does here

1. **Story finder** (`storyfind.py`): reads the whole transcript once and returns the stories + headlines.
2. **Per-clip package** (`analyze.py`): for each clip a judge call, a titles call and a description call
   (titles, hashtags, description for posting).

Video and audio never leave the PC. Facecam detection, Whisper and rendering are local and free.

## Measured input size

`iShowSpeed's First Time Playing GTA 6` (34.8 min): 2,390 spoken words, prompt with line numbers and tags
~5,100 tokens -> **~8,700 tokens per hour** for this streamer. A chatty streamer talks 2-3x as much, so the
estimate below uses **25,000 tokens per hour** (upper end).

Per hour of video (about 10 clips):

| Step | Input tokens | Output tokens |
|---|---|---|
| Story finder (1 request; cloud reads 1 h in one go) | ~25,000 | ~3,000 (+ thinking) |
| Per clip: judge + titles + description (3 requests x 10 clips) | ~70,000 | ~13,000 |
| **Total** | **~95,000** | **~16,000 + thinking** |

Claude models think before answering (billed as output). At effort `medium` count roughly +1,000 output
tokens per request, i.e. **~47,000 output tokens per hour** in total.

## Price per 1 hour of video

Prices per 1M tokens (input / output): Claude from the Claude API model table (cached 2026-11-03), Gemini
from ai.google.dev pricing (2026-11-19; Gemini 3.8 / 3.6 Flash prices double on 2027-01-01).

| Model | $ in / out per 1M | Cost per 1 h video | Notes |
|---|---|---|---|
| Claude Opus 5.5 | 4 / 20 | ~$1.30 | best judgement; default in the app |
| Claude Sonnet 5.5 | 2 / 10 | ~$0.65 | very good, half the price |
| Claude Haiku 5.5 | 0.10 / 0.50 | ~$0.03 | cheapest Claude; fine for titles |
| Gemini 3.8 Flash | 0.75 / 3.75 | ~$0.25 | free tier has a small daily request limit |
| Gemini 3.5 Flash-Lite | 0.30 / 2.50 | ~$0.14 | bigger free daily limit |
| Local qwen3.5:4b | free | $0 | works for stories; weaker for titles; uses this PC's GPU |

**With the Claude Ultra plan's $200 monthly API credits:** ~150 hours of video per month on Opus 5.5,
~300 h on Sonnet 5.5, thousands of hours on Haiku 5.5. Nothing extra to pay.

**Gemini free tier:** about 31 requests per hour of video; the free Flash daily request limit (reported
around 20/day, shown only in AI Studio) is too small, Flash-Lite (reported ~500/day) is enough. The app
falls back to the local model when the free daily limit is used up. Google may use free-tier prompts to
improve its products.

## "Video analysis" through the API (not used)

Sending the video itself is far more expensive and not needed (the transcript + local facecam detection
already cover it):

- Gemini reads video at ~258 tokens/s (+32 audio): **~1M tokens per hour -> ~$0.80 on 3.8 Flash**
  (~$0.27 at low media resolution).
- Claude has no video input; 1 frame every 2 s = 1,800 images x ~1,600 tokens = ~2.9M tokens per hour
  (~$0.30 on Haiku 5.5, ~$11.50 on Opus 5.5).

## Recommendation

- Use the **Claude API with your Ultra credits**: Sonnet 5.5 (or Opus 5.5) for the AI steps. The PC stays
  free for gaming (no local model on the GPU), the stories and titles are much better than qwen3.5:4b,
  and the cost is covered by the monthly credits.
- Keep "fall back to the local model" on, so a run never stops.
- Local qwen3.5:4b alone: works for the story finder (~1 min per 20 min of video when the GPU is free) but
  often fails the title step; qwen3.5:9b does not fit the 6 GB GPU (3 tokens/s).

How to get the keys: Claude: claude.ai > Settings > Billing > API credits > Link organization, then
console.anthropic.com > API keys > Create key. Gemini: aistudio.google.com > Get API key. Paste the key on the
Gaming page > "AI that picks the clips" > Save and use > Test.
