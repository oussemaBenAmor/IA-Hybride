# === Destination : app/graph/nodes/extraction.py (remplace l'existant) ===
"""
Extraction des paramètres métier depuis le texte libre, via le client LLM
centralisé. La sortie JSON est forcée par schéma (plus de parsing à la ficelle,
température réellement appliquée).
"""
import time
import json
import logging

from app.graph.state import GraphState
from app.schemas.business import CASE_SCHEMA_MAP
from app.core.llm import generate_json, LLMError

logger = logging.getLogger("extraction")


def build_extraction_prompt(case_name: str, text: str, fields_desc: str) -> str:
    return f"""Tu es un assistant bancaire. Extrais les paramètres de la demande.

Cas métier : {case_name}
Demande : "{text}"

Champs à extraire :
{fields_desc}

Règles :
- Remplis chaque champ présent dans la demande, sinon mets null.
- Montants en euros (décimal), durées en mois (entier).
"""

def extraction_node(state: GraphState) -> GraphState:
    start = time.time()
    case_name = state["case_selected"]
    text = state.get("input_corrected") or state["input_raw"]
    schema_class = CASE_SCHEMA_MAP.get(case_name)

    if not schema_class:
        elapsed = (time.time() - start) * 1000
        return {**state, "fallback_type": "C",
                "fallback_reason": f"Schéma inconnu pour {case_name}",
                "latency_ms": {**state.get("latency_ms", {}), "extraction": elapsed}}

    props = schema_class.model_json_schema().get("properties", {})
    fields_desc = "\n".join(f'- {k} : {v.get("description", "")}' for k, v in props.items())

    try:
        freshly_extracted = generate_json(
            build_extraction_prompt(case_name, text, fields_desc),
            schema_class, task="extraction", temperature=0.1,
        )
    except LLMError as e:
        logger.warning("extraction LLM échouée : %s", e)
        freshly_extracted = {}

    inherited = {k: v for k, v in (state.get("extracted_params") or {}).items() if v is not None}
    merged = {**inherited}
    for k, v in freshly_extracted.items():
        if v is not None:
            merged[k] = v

    elapsed = (time.time() - start) * 1000
    logger.info("extraction %s → %s", case_name, merged)
    return {**state, "extracted_params": merged,
            "latency_ms": {**state.get("latency_ms", {}), "extraction": elapsed}}


def extract_params_with_llm(
        case_name: str,
        text: str,
        existing_params: dict,
        missing_fields: list,
) -> dict:
    """Extrait uniquement les `missing_fields` depuis une réponse utilisateur."""
    schema_class = CASE_SCHEMA_MAP.get(case_name)
    if not schema_class:
        return {}

    full_schema = schema_class.model_json_schema()
    partial_schema = {
        "type": "object",
        "properties": {
            k: v for k, v in full_schema.get("properties", {}).items() if k in missing_fields
        },
    }

    prompt = f"""Tu es un assistant bancaire. L'utilisateur complète sa demande.

Cas métier : {case_name}
Paramètres déjà connus : {json.dumps(existing_params, ensure_ascii=False)}
Champs à extraire : {missing_fields}
Réponse de l'utilisateur : "{text}"

Règles : montants en euros, durées en mois (entiers), null si non mentionné.
"""
    try:
        return generate_json(prompt, partial_schema, task="extraction", temperature=0.1)
    except LLMError as e:
        logger.warning("extract_params_with_llm échouée : %s", e)
        return {}