"""
Client LLM centralisé pour Ollama avec gestion des modèles,
génération JSON, retries et logs de performance.
"""
from __future__ import annotations

import json
import time
import logging
from typing import Type, Union

import httpx
from pydantic import BaseModel
from tenacity import (
    retry,                 # ajoute une logique de retry automatique
    stop_after_attempt,    # nombre maximum de tentatives
    wait_exponential,      # attente progressivement plus longue entre les essais
    retry_if_exception,    # permet de choisir quelles erreurs déclenchent un retry
    before_sleep_log,
)

from app.config import settings

logger = logging.getLogger("llm")


# Erreurs réseau temporaires qui peuvent être récupérées avec un retry
_NETWORK_ERRORS = (httpx.ConnectError, httpx.TimeoutException)


class LLMError(RuntimeError):
    """Erreur personnalisée pour les problèmes liés au LLM."""
    pass


def _is_retryable(exc: BaseException) -> bool:
    """Détermine si une erreur doit provoquer un nouvel essai.

    Retry uniquement :
    - erreurs réseau
    - timeout
    - erreurs serveur 5xx
    Pas de retry pour les erreurs client 4xx.
    """
    # Coupure réseau ou délai dépassé => on retente
    if isinstance(exc, _NETWORK_ERRORS):
        return True

    # Erreur HTTP serveur (500, 502...) => on retente
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500

    # Les autres erreurs (4xx, etc.) ne nécessitent pas de retry
    return False


def _resolve_model(task: str | None) -> str:
    """Choisit le modèle à utiliser.

    Si une tâche possède un modèle spécifique dans la config, on l'utilise.
    Sinon on utilise le modèle LLM par défaut.
    """
    if task:
        # Cherche par exemple extraction_model, classifier_model
        override = getattr(settings, f"{task}_model", None)
        if override:
            return override

    # Modèle général utilisé par défaut
    return settings.llm_model


def _with_no_think(prompt: str) -> str:
    """Ajoute /no_think pour désactiver le raisonnement détaillé des modèles."""
    # Évite d'ajouter plusieurs fois /no_think
    return prompt if prompt.lstrip().startswith("/no_think") else "/no_think\n" + prompt


def _keep_alive() -> str | float:
    """Durée pendant laquelle Ollama garde le modèle chargé en mémoire."""
    return getattr(settings, "ollama_keep_alive", "30m")


@retry(
    # Maximum 3 appels en cas d'erreur récupérable
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    # Renvoie directement la dernière erreur après les essais
    reraise=True,
)
def _post_generate(payload: dict, timeout: float) -> dict:
    """Envoie une requête POST à Ollama."""
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
    """Génère une réponse texte simple avec Ollama."""
    # Le modèle fourni prend priorité sur celui de la configuration
    chosen = model or _resolve_model(task)
    if not think:
        prompt = _with_no_think(prompt)

    # Corps envoyé à Ollama
    payload = {
        "model": chosen,
        "prompt": prompt,
        "stream": False,          # attend toute la réponse avant de retourner
        "keep_alive": _keep_alive(),
        "options": {"temperature": temperature},
    }
    start = time.time()
    try:
        # Appel réel à Ollama
        data = _post_generate(payload, timeout or settings.ollama_timeout_sec)
    except httpx.HTTPError as e:
        # Transforme l'erreur HTTP en erreur métier
        raise LLMError(f"Ollama indisponible (model={chosen}) : {e}") from e

    elapsed = (time.time() - start) * 1000
    logger.info("generate_text model=%s temp=%.2f latency_ms=%.0f", chosen, temperature, elapsed)

    # Retourne uniquement le texte généré
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
    """Génère une réponse JSON avec un format imposé.

    Utilisé lorsque le LLM doit retourner une structure précise
    (extraction de paramètres).
    """
    # Sélection du modèle
    chosen = model or _resolve_model(task)

    # Conversion d'un modèle Pydantic en JSON Schema
    fmt = schema.model_json_schema() if (isinstance(schema, type) and issubclass(schema, BaseModel)) else schema

    # Force le mode sans raisonnement
    prompt = _with_no_think(prompt)

    # Payload avec format JSON imposé
    payload = {
        "model": chosen,
        "prompt": prompt,
        "stream": False,
        "format": fmt,            # demande à Ollama de respecter ce schéma JSON
        "keep_alive": _keep_alive(),
        "options": {"temperature": temperature},
    }
    start = time.time()
    try:
        data = _post_generate(payload, timeout or settings.ollama_timeout_sec)
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama indisponible (model={chosen}) : {e}") from e

    # Récupère la réponse brute du LLM
    raw = (data.get("response") or "").strip()

    # Tente de récupérer du JSON valide (directement, puis sans les ``` fences)
    parsed = _safe_json(raw) or _safe_json(_strip_fences(raw))

    # Impossible de convertir la réponse en JSON
    if parsed is None:
        logger.error("generate_json JSON invalide model=%s raw=%r", chosen, raw[:300])
        raise LLMError("Réponse LLM non parsable en JSON")

    elapsed = (time.time() - start) * 1000
    logger.info("generate_json model=%s temp=%.2f latency_ms=%.0f", chosen, temperature, elapsed)
    return parsed

# Tente de convertir une chaîne en dictionnaire JSON.
def _safe_json(text: str) -> dict | None:
    try:

        # Vérifie que le résultat est bien un objet JSON
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except (json.JSONDecodeError, TypeError):

        # JSON invalide
        return None



# Supprime les balises Markdown ```json ... ```
def _strip_fences(text: str) -> str:
    if "```" in text:

        # Récupère uniquement le contenu entre les ```
        chunk = text.split("```")[1]

        # Supprime le mot json au début
        if chunk.startswith("json"):
            chunk = chunk[4:]
        return chunk.strip()
    return text