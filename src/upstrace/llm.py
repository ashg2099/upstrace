"""One small door to whichever free LLM you have, plus a cache.

Design rules, in order of importance:

1. The model never sees your data. It sees numbers we already computed - column
   names, metric deltas, lineage. That keeps this cheap, private and fast.
2. Every response is cached on disk by prompt hash, and the cache is committed.
   Anyone can clone this repo and reproduce every explanation with no API key.
3. Any provider will do. Groq and Gemini both have free tiers; Ollama runs
   locally with no key at all.
"""

import hashlib
import json
import os
from pathlib import Path

import requests

from .config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "cache" / "llm"

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "gemini": "gemini-2.0-flash",
    "ollama": "qwen2.5:3b",
    "mock": "mock",
}


def _provider() -> str:
    return os.environ.get("UPSTRACE_LLM_PROVIDER", "groq").lower()


def _model() -> str:
    provider = _provider()
    return os.environ.get("UPSTRACE_LLM_MODEL", DEFAULT_MODELS.get(provider, "unknown"))


def _cache_path(prompt: str) -> Path:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{digest}.json"


def _read_cache(prompt: str) -> dict | None:
    path = _cache_path(prompt)
    if not path.exists():
        return None
    with open(path) as fh:
        return json.load(fh)["response"]


def _write_cache(prompt: str, response: dict, provider: str, model: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_cache_path(prompt), "w") as fh:
        json.dump(
            {"provider": provider, "model": model,
             "prompt": prompt, "response": response},
            fh, indent=2, sort_keys=True,
        )


# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------

def _call_groq(prompt: str, model: str) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise SystemExit("GROQ_API_KEY is not set. export it, or use another provider.")

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout=90,
    )
    if response.status_code != 200:
        raise SystemExit(f"Groq returned {response.status_code}: {response.text[:400]}")
    return response.json()["choices"][0]["message"]["content"]


def _call_gemini(prompt: str, model: str) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise SystemExit("GEMINI_API_KEY is not set. export it, or use another provider.")

    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        },
        timeout=90,
    )
    if response.status_code != 200:
        raise SystemExit(f"Gemini returned {response.status_code}: {response.text[:400]}")
    return response.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_ollama(prompt: str, model: str) -> str:
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    response = requests.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        },
        timeout=300,
    )
    if response.status_code != 200:
        raise SystemExit(f"Ollama returned {response.status_code}: {response.text[:400]}")
    return response.json()["message"]["content"]


def _call_mock(prompt: str, model: str) -> str:
    """Deterministic stand-in so the pipeline can be tested without a network."""
    return json.dumps({
        "summary": "MOCK RESPONSE - no model was called.",
        "likely_causes": [
            {"cause": "mock cause", "confidence": "low", "reasoning": "mock"}
        ],
        "checks_to_add": ["mock check"],
        "who_is_affected": "mock",
    })


CALLERS = {
    "groq": _call_groq,
    "gemini": _call_gemini,
    "ollama": _call_ollama,
    "mock": _call_mock,
}


def ask_json(prompt: str, use_cache: bool = True) -> dict:
    """Send a prompt, get parsed JSON back. Cached by prompt hash."""
    if use_cache:
        cached = _read_cache(prompt)
        if cached is not None:
            return cached

    provider = _provider()
    model = _model()

    if provider not in CALLERS:
        raise SystemExit(
            f"Unknown provider {provider!r}. Known: {', '.join(sorted(CALLERS))}"
        )

    raw = CALLERS[provider](prompt, model)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Some models wrap JSON in prose or a code fence. Take the outermost braces.
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            raise SystemExit(f"Model did not return JSON:\n{raw[:400]}")
        parsed = json.loads(raw[start:end + 1])

    _write_cache(prompt, parsed, provider, model)
    return parsed


def cache_status() -> tuple[int, Path]:
    if not CACHE_DIR.exists():
        return 0, CACHE_DIR
    return len(list(CACHE_DIR.glob("*.json"))), CACHE_DIR