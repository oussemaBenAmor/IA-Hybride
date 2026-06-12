# === Destination : app/core/llm.py (remplace l'existant) ===
"""
Client LLM centralisé pour Ollama.

Apporte : température dans `options` (sinon ignorée par Ollama), JSON forcé par
schéma via `format`, retries réseau, modèle par tâche, logs avec latence.

MAJ incrément 3 : on ne retente QUE les erreurs récupérables — coupures réseau,
timeouts et erreurs serveur 5xx. Une 404 (modèle introuvable) ou autre 4xx
échoue immédiatement, sans 3 retries inutiles.
"""
from __future__ import annotations

import json
import time
import logging
from typing import Type, Union

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception,
    before_sleep_log,
)

from app.config import settings

logger = logging.getLogger("llm")

_NETWORK_ERRORS = (httpx.ConnectError, httpx.TimeoutException)


class LLMError(RuntimeError):
    """Erreur LLM non récupérable (après épuisement des retries)."""


def _is_retryable(exc: BaseException) -> bool:
    """Réseau/timeout → on retente. 5xx → on retente. 4xx (ex: 404) → non."""
    if isinstance(exc, _NETWORK_ERRORS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def _resolve_model(task: str | None) -> str:
    if task:
        override = getattr(settings, f"{task}_model", None)
        if override:
            return override
    return settings.llm_model


def _with_no_think(prompt: str) -> str:
    return prompt if prompt.lstrip().startswith("/no_think") else "/no_think\n" + prompt
def _keep_alive() -> str | float:
    return getattr(settings, "ollama_keep_alive", "30m")

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _post_generate(payload: dict, timeout: float) -> dict:
    resp = httpx.post(
        f"{settings.ollama_base_url}/api/generate",
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def generate_text(
        prompt: str,
        *,
        task: str | None = None,
        model: str | None = None,
        temperature: float = 0.3,
        timeout: float | None = None,
        think: bool = False,
) -> str:
    chosen = model or _resolve_model(task)
    if not think:
        prompt = _with_no_think(prompt)
    payload = {
        "model": chosen,
        "prompt": prompt,
        "stream": False,
        "keep_alive": _keep_alive(),
        "options": {"temperature": temperature},
    }
    start = time.time()
    try:
        data = _post_generate(payload, timeout or settings.ollama_timeout_sec)
    except (httpx.HTTPError,) as e:
        raise LLMError(f"Ollama indisponible (model={chosen}) : {e}") from e
    elapsed = (time.time() - start) * 1000
    logger.info("generate_text model=%s temp=%.2f latency_ms=%.0f", chosen, temperature, elapsed)
    return (data.get("response") or "").strip()


def generate_json(
        prompt: str,
        schema: Union[dict, Type[BaseModel]],
        *,
        task: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: float | None = None,
) -> dict:
    chosen = model or _resolve_model(task)
    fmt = schema.model_json_schema() if (isinstance(schema, type) and issubclass(schema, BaseModel)) else schema
    prompt = _with_no_think(prompt)
    payload = {
        "model": chosen,
        "prompt": prompt,
        "stream": False,
        "format": fmt,
        "keep_alive": _keep_alive(),
        "options": {"temperature": temperature},
    }
    start = time.time()
    try:
        data = _post_generate(payload, timeout or settings.ollama_timeout_sec)
    except (httpx.HTTPError,) as e:
        raise LLMError(f"Ollama indisponible (model={chosen}) : {e}") from e

    raw = (data.get("response") or "").strip()
    parsed = _safe_json(raw) or _safe_json(_strip_fences(raw))
    if parsed is None:
        logger.error("generate_json JSON invalide model=%s raw=%r", chosen, raw[:300])
        raise LLMError("Réponse LLM non parsable en JSON")

    elapsed = (time.time() - start) * 1000
    logger.info("generate_json model=%s temp=%.2f latency_ms=%.0f", chosen, temperature, elapsed)
    return parsed


def _safe_json(text: str) -> dict | None:
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _strip_fences(text: str) -> str:
    if "```" in text:
        chunk = text.split("```")[1]
        if chunk.startswith("json"):
            chunk = chunk[4:]
        return chunk.strip()
    return text