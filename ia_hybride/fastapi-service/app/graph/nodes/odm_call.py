"""
Appelle le service ODM via Spring Boot.

- Envoie les paramètres métier.
- Réessaie automatiquement en cas d'erreur réseau.
- Retourne la décision d'ODM.
"""

import time
import logging

import httpx

# bibliothèque qui ajoute automatiquement un mécanisme de retry.
from tenacity import (
    retry,
    stop_after_delay,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
    RetryError,
)

from app.graph.state import GraphState
from app.config import settings

logger = logging.getLogger("odm")


# Réessaie automatiquement l'appel ODM en cas d'erreur réseau
@retry(

    # Arrête les tentatives après la durée maximale configurée
    stop=stop_after_delay(settings.odm_retry_window_sec),

    # Augmente progressivement le temps d'attente entre deux essais
    wait=wait_exponential(multiplier=1, min=settings.odm_wait_min_sec, max=settings.odm_wait_max_sec),

    # Réessaie uniquement pour les erreurs réseau ou HTTP
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError)),

    # Journalise chaque nouvelle tentative
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=False,
)
def _call_odm(payload: dict) -> dict:

    # Envoie la requête au service Spring Boot
    resp = httpx.post(
        f"{settings.spring_boot_url}/api/odm/execute",
        json=payload,
        timeout=settings.odm_request_timeout_sec,
    )

    # Déclenche une exception si le code HTTP n'est pas 2xx
    resp.raise_for_status()

    # Retourne la réponse JSON d'ODM
    return resp.json()


def _extract_rules(result: dict) -> list:
    # Java sérialise `rulesTriggered` (camelCase). On tolère les deux par sécurité.
    return result.get("rulesTriggered") or result.get("rules_triggered") or []


def odm_call_node(state: GraphState) -> GraphState:
    start = time.time()

    # Prépare les données à envoyer à ODM
    payload = {
        "caseName": state["case_selected"],
        "params":   state.get("extracted_params", {}),
    }
    logger.info("appel ODM pour %s (retry jusqu'à %.0fs)", state["case_selected"], settings.odm_retry_window_sec)

    try:

        # Appelle le service ODM
        result  = _call_odm(payload)
        elapsed = (time.time() - start) * 1000
        logger.info("ODM ok en %.0f ms (decision=%s)", elapsed, result.get("decision"))

        # Sauvegarde la décision et les règles déclenchées
        return {
            **state,
            "odm_payload":  payload,
            "odm_decision": result,
            "rules_fired":  _extract_rules(result),
            "latency_ms":   {**state.get("latency_ms", {}), "odm": elapsed},
        }

    # Toutes les tentatives automatiques ont échoué
    except RetryError as e:
        elapsed = (time.time() - start) * 1000

        # Récupère la dernière erreur rencontrée
        cause = str(e.last_attempt.exception()) if e.last_attempt else str(e)
        logger.error("ODM indisponible après %.0f ms : %s", elapsed, cause)

        # Retourne un fallback si ODM reste indisponible
        return {
            **state,
            "odm_payload":     payload,
            "fallback_type":   "E",
            "fallback_reason": f"ODM indisponible après {settings.odm_retry_window_sec:.0f}s : {cause}",
            "response_text": (
                "Le service de décision est temporairement indisponible. "
                "Votre demande a bien été reçue — veuillez réessayer dans quelques instants."
            ),
            "latency_ms": {**state.get("latency_ms", {}), "odm": elapsed},
        }


    # Capture toute autre erreur inattendue
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        logger.exception("erreur inattendue ODM")
        return {
            **state,
            "odm_payload":     payload,
            "fallback_type":   "E",
            "fallback_reason": f"Erreur ODM : {e}",
            "response_text": (
                "Une erreur inattendue s'est produite lors de la prise de décision. "
                "Veuillez réessayer."
            ),
            "latency_ms": {**state.get("latency_ms", {}), "odm": elapsed},
        }