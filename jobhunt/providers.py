"""Provider-agnostic LLM clients.

One tiny interface, five backends. Screening and drafting each pick their own
provider from env vars, so you can point the cheap pass at Groq or Gemini and
keep Claude for the expensive drafting pass - or run the whole thing on a free
tier with no card on file.

    complete(system, user)            -> str   (every provider)
    complete_document(prompt, pdf)    -> str   (Anthropic + Gemini only)

Nothing here parses JSON or knows what a Job is. That lives in llm.py.
"""
from __future__ import annotations

import base64
import os
from typing import Any

import requests

TIMEOUT = 120


class LLMError(RuntimeError):
    """Anything that came back wrong from a provider."""


class UnsupportedDocument(LLMError):
    """Provider cannot read a PDF; caller should fall back to plain text."""


# ---------------------------------------------------------------------------


class Provider:
    name = "base"
    required_env: str | None = None

    def preflight(self) -> None:
        """Fail before the first call, not on batch 1 of 40.

        Without this a missing key surfaces as a per-batch warning, every batch
        fails the same way, and the run ends with an empty digest that reads as
        "no good jobs today".
        """
        if self.required_env:
            self._env(self.required_env)

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        """`json_mode` asks the provider to guarantee valid JSON where it can.

        Most providers have a native switch for this. It is a request, not a
        promise — llm.parse_json still has to cope with whatever comes back.
        """
        raise NotImplementedError

    def complete_document(self, model: str, prompt: str, pdf: bytes,
                          max_tokens: int) -> str:
        raise UnsupportedDocument(
            f"{self.name} cannot read PDFs here - pass a .txt/.md resume instead"
        )

    @staticmethod
    def _env(key: str) -> str:
        # An empty value is a missing value. A blank line in .env otherwise
        # sails past the check and fails much later as an opaque 401.
        value = (os.environ.get(key) or "").strip()
        if not value:
            raise LLMError(f"{key} is not set (see .env.example)")
        return value


class AnthropicProvider(Provider):
    """Claude via the official SDK."""

    name = "anthropic"
    required_env = "ANTHROPIC_API_KEY"

    def _client(self):
        try:
            from anthropic import Anthropic
        except ImportError:
            raise LLMError("pip install anthropic") from None
        return Anthropic(api_key=self._env("ANTHROPIC_API_KEY"))

    @staticmethod
    def _text(msg) -> str:
        return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        # No native JSON switch needed here — the prompts already specify the
        # shape and Claude honours it. json_mode is accepted and ignored.
        msg = self._client().messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return self._text(msg)

    def complete_document(self, model: str, prompt: str, pdf: bytes,
                          max_tokens: int) -> str:
        msg = self._client().messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(pdf).decode(),
                }},
                {"type": "text", "text": prompt},
            ]}],
        )
        return self._text(msg)


class GeminiProvider(Provider):
    """Google AI Studio REST API. Generous free tier, no card needed."""

    name = "gemini"
    required_env = "GEMINI_API_KEY"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def _post(self, model: str, body: dict) -> str:
        r = requests.post(
            f"{self.BASE}/{model}:generateContent",
            params={"key": self._env("GEMINI_API_KEY")},
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise LLMError(f"gemini HTTP {r.status_code}: {r.text[:300]}")
        try:
            candidate = r.json()["candidates"][0]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"gemini returned no candidates: {r.text[:300]}") from e

        # Gemini 2.5+ spends output tokens on internal thinking, so a tight
        # maxOutputTokens gets consumed before the answer starts. That arrives
        # as truncated JSON or no parts at all — say so plainly instead of
        # letting it surface three layers up as a parse error.
        reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        if reason == "MAX_TOKENS" or (not text and reason not in (None, "STOP")):
            raise LLMError(
                f"gemini stopped early (finishReason={reason}) with "
                f"{len(text)} chars of output — raise max_tokens for this stage"
            )
        if not text:
            raise LLMError(f"gemini returned no text: {r.text[:300]}")
        return text

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        gen: dict[str, Any] = {"maxOutputTokens": max_tokens, "temperature": 0.2}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        if system:
            body["system_instruction"] = {"parts": [{"text": system}]}
        return self._post(model, body)

    def complete_document(self, model: str, prompt: str, pdf: bytes,
                          max_tokens: int) -> str:
        return self._post(model, {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": "application/pdf",
                                 "data": base64.b64encode(pdf).decode()}},
                {"text": prompt},
            ]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.2},
        })


class OpenAICompatProvider(Provider):
    """Anything speaking /chat/completions - Groq, Together, OpenRouter, vLLM."""

    name = "openai-compatible"
    required_env = "GROQ_API_KEY"
    default_base = "https://api.groq.com/openai/v1"
    key_env = "GROQ_API_KEY"

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        base = os.getenv("LLM_BASE_URL", self.default_base).rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": user}]
        payload: dict[str, Any] = {"model": model, "messages": messages,
                                   "max_tokens": max_tokens, "temperature": 0.2}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        r = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {self._env(self.key_env)}"},
            json=payload,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            raise LLMError(f"{self.name} HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.name} malformed reply: {r.text[:300]}") from e


class GroqProvider(OpenAICompatProvider):
    name = "groq"


class OllamaProvider(Provider):
    """Fully local. No key, no cost, no rate limit - just a slower model."""

    name = "ollama"

    def complete(self, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool = False) -> str:
        base = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": user}]
        payload: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = requests.post(
                f"{base}/api/chat", json=payload, timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise LLMError(
                f"ollama unreachable at {base} - is `ollama serve` running?") from e
        if r.status_code != 200:
            raise LLMError(f"ollama HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["message"]["content"]
        except (KeyError, ValueError) as e:
            raise LLMError(f"ollama malformed reply: {r.text[:300]}") from e


PROVIDERS = {
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
    "groq": GroqProvider,
    "openai-compatible": OpenAICompatProvider,
    "ollama": OllamaProvider,
}

# What each provider gets if you do not name a model, so `--scorer llm` works
# after setting nothing but a key.
DEFAULT_MODELS = {
    "anthropic": {"screen": "claude-haiku-4-5-20251001", "draft": "claude-sonnet-5"},
    "gemini": {"screen": "gemini-2.0-flash", "draft": "gemini-2.0-flash"},
    "groq": {"screen": "llama-3.3-70b-versatile", "draft": "llama-3.3-70b-versatile"},
    "openai-compatible": {"screen": "gpt-4o-mini", "draft": "gpt-4o"},
    "ollama": {"screen": "llama3.1", "draft": "llama3.1"},
}


def get_provider(name: str) -> Provider:
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise LLMError(
            f"unknown provider {name!r}; pick one of {', '.join(PROVIDERS)}"
        ) from None


def resolve(stage: str, check: bool = True) -> tuple[Provider, str]:
    """Which provider + model handles this stage ("screen" or "draft")?

    Precedence: stage-specific env var -> global env var -> built-in default.
    Raises LLMError if the provider's credentials are missing, so the caller
    can bail before spending anything.
    """
    name = (os.getenv(f"{stage.upper()}_PROVIDER")
            or os.getenv("LLM_PROVIDER")
            or "anthropic").strip().lower()
    # Resolve the provider first: a typo'd name should say "unknown provider",
    # not send you hunting for a model id it was never going to use.
    provider = get_provider(name)
    model = (os.getenv(f"{stage.upper()}_MODEL") or "").strip() \
        or DEFAULT_MODELS.get(name, {}).get(stage)
    if not model:
        raise LLMError(f"set {stage.upper()}_MODEL for provider {name!r}")
    if check:
        provider.preflight()
    return provider, model
