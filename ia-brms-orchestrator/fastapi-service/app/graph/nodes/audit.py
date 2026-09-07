"""
Nœud d'audit : enregistre une trace complète du traitement dans la base.
"""
import json
import time
import logging

from app.graph.state import GraphState
from app.db.postgres import get_connection

logger = logging.getLogger("audit")

# Association entre les noms des nœuds et les colonnes SQL des latences
_LATENCY_COLS = {
    "security":   "latency_security",
    "correction": "latency_correction",
    "router":     "latency_router",
    "extraction": "latency_extraction",
    "validation": "latency_validation",
    "odm":        "latency_odm",
    "generation": "latency_generation",
}


# Calcule l'écart entre les deux meilleurs scores du router
def _top2_gap(top2) -> float | None:
    if top2 and len(top2) >= 2:
        try:
            return float(top2[0]["score"]) - float(top2[1]["score"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def audit_node(state: GraphState) -> GraphState:
    start = time.time()

    # Récupère les latences des autres nœuds
    latencies = dict(state.get("latency_ms", {}))
    latency_total = sum(latencies.values())

    # Récupère la réponse ODM
    odm = state.get("odm_decision") or {}
    raw = state.get("input_raw")
    corrected = state.get("input_corrected")

    # Initialise toutes les colonnes de latence à None
    latency_values = {col: None for col in _LATENCY_COLS.values()}

    # Associe chaque latence à sa colonne SQL
    for node, ms in latencies.items():
        col = _LATENCY_COLS.get(node)
        if col:
            latency_values[col] = ms

    conn = get_connection()
    try:

        # Crée un curseur pour exécuter les requêtes SQ
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversation_sessions (session_id) "
                "VALUES (%s::uuid) ON CONFLICT DO NOTHING",
                (state["session_id"],),
            )

            # Crée la session si elle n'existe pas déjà
            cur.execute(
                f"""
                INSERT INTO audit_trail (
                    session_id, raw_input, corrected_input, correction_applied,
                    business_case, router_score, router_top2_gap, extracted_params,
                    pydantic_valid, validation_attempts, odm_payload, odm_response,
                    rules_triggered, odm_decision, llm_response, fallback_type,
                    security_blocked, security_pattern,
                    {", ".join(_LATENCY_COLS.values())}, latency_total
                ) VALUES (
                    %s::uuid, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    {", ".join(["%s"] * len(_LATENCY_COLS))}, %s
                )
                """,
                (
                    state["session_id"],
                    raw,
                    corrected,
                    bool(corrected and corrected != raw),
                    state.get("case_selected"),
                    state.get("confidence"),
                    _top2_gap(state.get("top2_scores")),
                    json.dumps(state.get("extracted_params")),
                    state.get("validation_errors") is None and state.get("fallback_type") != "C",
                    state.get("retry_count", 0),
                    json.dumps(state.get("odm_payload")),
                    json.dumps(odm or None),
                    json.dumps(state.get("rules_fired")),
                    (odm.get("decision") if isinstance(odm, dict) else None),
                    state.get("response_text"),
                    state.get("fallback_type"),
                    bool(state.get("is_blocked")),
                    state.get("security_pattern"),
                    *[latency_values[c] for c in _LATENCY_COLS.values()],
                    latency_total,
                ),
            )
        conn.commit()
        logger.info(
            "audit ok session=%s case=%s fallback=%s total=%.0fms",
            str(state["session_id"])[:8],
            state.get("case_selected"),
            state.get("fallback_type"),
            latency_total,
        )
    except Exception:

        # Annule l'insertion en cas d'erreur
        conn.rollback()

        # Enregistre l'erreur complète dans les logs
        logger.exception("audit insert FAILED")   # on ne masque plus l'erreur
    finally:
        conn.close()

    elapsed = (time.time() - start) * 1000

    # Retourne le state en ajoutant la latence de l'audit
    return {**state, "latency_ms": {**latencies, "audit": elapsed}}