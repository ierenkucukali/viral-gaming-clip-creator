"""Cloud AI for the clip analysis (local fork, opt-in): Claude API or Gemini API.

Off by default: the local Ollama model answers until the user picks a provider on the Gaming page
and saves its API key. Then every `llm.session()` (story finder, judge, titles, descriptions) goes
to that provider instead. What is sent: the prompts, i.e. transcript TEXT with timings and signal
notes. Never the video, the audio or account data.

  anthropic  Claude API with the user's own key (Claude Max / Ultra plans include monthly API
             credits on a linked Console organization). Structured output: output_config.format.
  gemini     Google AI Studio key (free tier; Google may use free-tier prompts to improve its
             products). Structured output: responseJsonSchema.

Keys are stored encrypted (secretstore.py) in <settings>/ai_keys.sec, never in config.json and
never logged. A daily-quota / auth / network failure falls back to the local model when
`llm_cloud_fallback` is on (default), so a run never dies because the free quota ran out.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import config, secretstore
from .errors import SharkyClipsError
from .llm import LLM
from .ui import console

PROVIDERS = ("ollama", "anthropic", "gemini")
LABELS = {"ollama": "Local (Ollama)", "anthropic": "Claude API", "gemini": "Gemini API"}
# Claude: the current Opus by default (the user's monthly credits cover it); Haiku/Sonnet are cheaper.
ANTHROPIC_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
# Gemini: first one the key can use wins ("auto"). Flash-Lite has the bigger free daily quota.
GEMINI_PREFERRED = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite",
                    "gemini-3.1-flash-lite", "gemini-2.5-flash")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
KEY_FILE = "ai_keys.sec"
MAX_OUT = 16000


class CloudQuotaError(SharkyClipsError):
    """Daily quota used up / key rejected / no network: the local model may take over."""


@dataclass
class Cloud:
    provider: str
    model: str
    key: str


# ------------------------------------------------------------------ keys + settings


def _key_path() -> Path:
    return config.config_dir() / KEY_FILE


def _keys() -> dict:
    try:
        return secretstore.load(_key_path()) or {}
    except Exception:  # noqa: BLE001 - a broken / foreign key file counts as "no keys"
        return {}


def save_key(provider: str, key: str | None) -> None:
    if provider not in ("anthropic", "gemini"):
        raise SharkyClipsError(f"Unknown AI provider '{provider}'.")
    keys = _keys()
    key = (key or "").strip()
    if key:
        if len(key) < 20 or any(c.isspace() for c in key):
            raise SharkyClipsError("That doesn't look like an API key.", "Paste the whole key, without spaces.")
        keys[provider] = key
    else:
        keys.pop(provider, None)
    secretstore.save(_key_path(), keys)


def has_key(provider: str) -> bool:
    return bool(_keys().get(provider))


def masked(provider: str) -> str | None:
    k = _keys().get(provider)
    return f"{k[:6]}…{k[-4:]}" if k else None


def provider() -> str:
    p = str(config.get("llm_provider") or "ollama")
    return p if p in PROVIDERS else "ollama"


def model_for(p: str) -> str:
    m = str((config.get("llm_cloud_models") or {}).get(p) or "auto")
    if p == "anthropic":
        return m if m != "auto" else ANTHROPIC_MODELS[0]
    return m


def active() -> Cloud | None:
    """The cloud provider to use now, or None (= local Ollama)."""
    if config.get("llm_enabled") is False:
        return None
    p = provider()
    if p == "ollama":
        return None
    key = _keys().get(p)
    if not key:
        return None
    return Cloud(p, model_for(p), key)


def settings_view() -> dict:
    return {"provider": provider(), "providers": [{"id": p, "label": LABELS[p], "has_key": p == "ollama" or has_key(p),
                                                    "key": masked(p) if p != "ollama" else None,
                                                    "model": model_for(p) if p != "ollama" else None}
                                                   for p in PROVIDERS],
            "anthropic_models": list(ANTHROPIC_MODELS), "gemini_models": list(GEMINI_PREFERRED),
            "fallback": config.get("llm_cloud_fallback") is not False,
            "effort": str(config.get("llm_cloud_effort") or "medium")}


# ------------------------------------------------------------------ schema for the providers


def provider_schema(schema: dict) -> dict:
    """The JSON schema without the constraints the providers reject (min/max, lengths, item
    counts) and with additionalProperties: false on every object. The full schema is still
    checked here (llm.validate), and a bad answer is asked again, like with Ollama."""
    drop = {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "exclusiveMinimum",
            "exclusiveMaximum", "multipleOf", "pattern"}
    out: dict = {}
    for k, v in schema.items():
        if k in drop:
            continue
        if k == "properties":
            out[k] = {name: provider_schema(sub) for name, sub in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = provider_schema(v)
        else:
            out[k] = v
    if out.get("type") == "object":
        out["additionalProperties"] = False
        out.setdefault("required", list((out.get("properties") or {}).keys()))
    return out


# ------------------------------------------------------------------ the session object


class CloudLLM(LLM):
    """Same interface as llm.LLM (json(), calls, seconds, tokens_per_second, ...)."""

    def __init__(self, cloud: Cloud, stack: ExitStack, allow_fallback: bool = True):
        super().__init__(f"{cloud.provider}/{cloud.model}", num_ctx=1_000_000)
        self.allow_fallback = allow_fallback
        self.cloud = cloud
        self.load_seconds = 0.0
        self.tokens_in = 0
        self._stack = stack
        self._local: LLM | None = None
        self._client = None

    # no GPU, no governor pause, no model switching
    def warm_up(self) -> None:
        return None

    def _safe_point(self) -> None:
        return None

    def _send(self, messages: list[dict], schema: dict, temperature: float, num_predict: int) -> str:
        if self._local is not None:
            return self._local._send(messages, schema, temperature, num_predict)
        try:
            if self.cloud.provider == "anthropic":
                return self._anthropic(messages, schema, num_predict)
            return self._gemini(messages, schema, temperature, num_predict)
        except CloudQuotaError as exc:
            if not self.allow_fallback or config.get("llm_cloud_fallback") is False:
                raise
            from . import llm as llm_mod

            console.print(f"[yellow]{exc.message} Switching to the local model for the rest of this run.[/]")
            self._local = self._stack.enter_context(llm_mod.session(local=True))
            self.model = f"{self.model}+{self._local.model}"
            return self._local._send(messages, schema, temperature, num_predict)

    # ---------------------------------------------------------------- Claude API

    def _anthropic(self, messages: list[dict], schema: dict, num_predict: int) -> str:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.cloud.key, timeout=900.0, max_retries=4)
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
        effort = str(config.get("llm_cloud_effort") or "medium")
        kw: dict = {"model": self.cloud.model, "max_tokens": MAX_OUT, "messages": msgs,
                    "output_config": {"format": {"type": "json_schema", "schema": provider_schema(schema)},
                                      **({"effort": effort} if effort in ("low", "medium", "high") else {})}}
        if system:
            kw["system"] = system
        t0 = time.perf_counter()
        try:
            with self._client.messages.stream(**kw) as stream:
                msg = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise CloudQuotaError("The Claude API key was rejected.",
                                  "Check the key on the Gaming page (Console > API keys).") from exc
        except anthropic.PermissionDeniedError as exc:
            raise CloudQuotaError(f"Claude API: no access ({_short(exc)}).",
                                  "Check that the Console organization has credits.") from exc
        except anthropic.NotFoundError as exc:
            raise SharkyClipsError(f"Claude API: model '{self.cloud.model}' not found.",
                                   "Pick another Claude model on the Gaming page.") from exc
        except anthropic.RateLimitError as exc:
            raise CloudQuotaError(f"Claude API rate limit / credits used up ({_short(exc)}).") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code == 400 and "credit" in str(exc).lower():
                raise CloudQuotaError("Claude API: no credits left on the Console organization.") from exc
            raise SharkyClipsError(f"Claude API error {exc.status_code}: {_short(exc)}") from exc
        except anthropic.APIConnectionError as exc:
            raise CloudQuotaError("Claude API: no connection.") from exc
        self.gen_seconds += time.perf_counter() - t0
        self.tokens_out += int(getattr(msg.usage, "output_tokens", 0) or 0)
        self.tokens_in += int(getattr(msg.usage, "input_tokens", 0) or 0)
        if msg.stop_reason == "refusal":
            raise SharkyClipsError("Claude declined this request.", "Try again or use another provider.")
        return next((b.text for b in msg.content if b.type == "text"), "")

    # ---------------------------------------------------------------- Gemini API

    def _gemini(self, messages: list[dict], schema: dict, temperature: float, num_predict: int) -> str:
        from .llm import _http

        model = self.cloud.model
        if model == "auto":
            model = resolve_gemini_model(self.cloud.key)
            self.cloud.model = model
            self.model = f"gemini/{model}"
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages if m["role"] != "system"]
        body: dict = {"contents": contents,
                      "generationConfig": {"responseMimeType": "application/json",
                                           "responseJsonSchema": provider_schema(schema),
                                           "temperature": temperature, "maxOutputTokens": MAX_OUT}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{GEMINI_URL}/models/{model}:generateContent"
        for attempt in range(4):
            t0 = time.perf_counter()
            try:
                r = _http().post(url, json=body, headers={"x-goog-api-key": self.cloud.key}, timeout=600)
            except Exception as exc:  # noqa: BLE001 - requests' connection errors
                raise CloudQuotaError("Gemini API: no connection.") from exc
            self.gen_seconds += time.perf_counter() - t0
            if r.status_code == 429:
                wait, daily = _gemini_retry(r)
                if daily or attempt == 3:
                    raise CloudQuotaError("The Gemini free daily limit is used up." if daily
                                          else "Gemini API rate limit.")
                console.print(f"[dim]Gemini rate limit: waiting {wait:.0f} s ...[/]")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise CloudQuotaError("The Gemini API key was rejected.",
                                      "Make a new key in Google AI Studio and save it on the Gaming page.")
            if r.status_code >= 500 and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            if not r.ok:
                raise SharkyClipsError(f"Gemini API error {r.status_code}: {r.text[:300]}")
            data = r.json()
            usage = data.get("usageMetadata") or {}
            self.tokens_out += int(usage.get("candidatesTokenCount") or 0)
            self.tokens_in += int(usage.get("promptTokenCount") or 0)
            cands = data.get("candidates") or []
            if not cands:
                raise SharkyClipsError(f"Gemini returned no answer ({(data.get('promptFeedback') or {})}).")
            parts = (cands[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))
        raise SharkyClipsError("Gemini API: no answer after retries.")


def _short(exc: Exception) -> str:
    return re.sub(r"\s+", " ", str(exc))[:200]


def _gemini_retry(r) -> tuple[float, bool]:
    """(seconds to wait, is it the DAILY quota)."""
    wait, daily = 30.0, False
    try:
        err = r.json().get("error") or {}
        for d in err.get("details") or []:
            if str(d.get("@type", "")).endswith("RetryInfo"):
                m = re.match(r"([\d.]+)s", str(d.get("retryDelay") or ""))
                if m:
                    wait = float(m.group(1)) + 1.0
            for v in d.get("violations") or []:
                if "PerDay" in str(v.get("quotaId") or "") or "per_day" in str(v.get("quotaMetric") or "").lower():
                    daily = True
    except ValueError:
        pass
    return min(wait, 120.0), daily


def resolve_gemini_model(key: str) -> str:
    """The best Flash model this key can use (cached in the config)."""
    from .llm import _http

    try:
        r = _http().get(f"{GEMINI_URL}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200},
                        timeout=20)
    except Exception as exc:  # noqa: BLE001
        raise CloudQuotaError("Gemini API: no connection.") from exc
    if r.status_code in (400, 401, 403):
        raise CloudQuotaError("The Gemini API key was rejected.")
    names = {str(m.get("name", "")).removeprefix("models/") for m in (r.json().get("models") or [])} if r.ok else set()
    for m in GEMINI_PREFERRED:
        if m in names:
            models = dict(config.get("llm_cloud_models") or {})
            models["gemini"] = m
            config.update(llm_cloud_models=models)
            return m
    flash = sorted(n for n in names if "flash" in n and "image" not in n and "tts" not in n and "live" not in n)
    if flash:
        return flash[-1]
    raise SharkyClipsError("This Gemini key can't use any Flash model.")


@contextmanager
def cloud_session(cloud: Cloud) -> Iterator[CloudLLM]:
    with ExitStack() as stack:
        console.print(f"Using [bold]{LABELS[cloud.provider]}[/] ({cloud.model}) for the AI steps.")
        yield CloudLLM(cloud, stack)


def test(provider_id: str) -> dict:
    """A tiny structured request: is the key + model OK? -> {"ok", "model", "seconds", "answer"}."""
    key = _keys().get(provider_id)
    if not key:
        raise SharkyClipsError("No API key saved for this provider.")
    schema = {"type": "object", "properties": {"headline": {"type": "string", "minLength": 3, "maxLength": 60}},
              "required": ["headline"]}
    t0 = time.perf_counter()
    with ExitStack() as stack:
        # the test must reach the provider itself, never the local fallback
        c = CloudLLM(Cloud(provider_id, model_for(provider_id), key), stack, allow_fallback=False)
        d = c.json("Answer only with JSON.", "Write a 4-word English headline about a GTA police chase, "
                   "with one emoji at the end.", schema, retries=1)
    return {"ok": True, "model": c.model, "seconds": round(time.perf_counter() - t0, 1),
            "answer": d.get("headline"), "tokens_in": c.tokens_in, "tokens_out": c.tokens_out}


def describe_for_prompt() -> str:
    """For logs: which AI answers (no key)."""
    c = active()
    return json.dumps({"provider": c.provider, "model": c.model} if c else {"provider": "ollama"})
