# === Destination : app/graph/nodes/extraction.py (remplace l'existant) ===
"""
Extraction des paramètres métier depuis le texte libre, via le client LLM
centralisé. La sortie JSON est forcée par schéma (plus de parsing à la ficelle,
température réellement appliquée).

CORRECTIF "null" : le LLM renvoie parfois la CHAÎNE "null" (ou "none", "nan"...)
au lieu d'un champ vide → `_clean_params` les convertit en None.

CORRECTIF nombres : le LLM tronquait parfois les montants (5000 → 500, un zéro
perdu). Deux mesures : température 0.0 (aucune créativité sur l'extraction) et
prompt qui interdit EXPLICITEMENT toute modification des nombres (pas d'arrondi,
pas de troncature, on recopie le nombre exactement comme écrit).

CORRECTIF "réponse de suivi mal captée" : lors de la collecte de paramètres
manquants, l'ancienne version n'extrayait QUE les `missing_fields`. Si
l'utilisateur répondait "j'ai 55 ans et je verse 2000 euros au départ", seul
l'âge était capté (le LLM, privé du contexte, ratait le montant). Désormais :
  - on extrait TOUS les champs du schéma (l'utilisateur peut donner spontanément
    plus que ce qu'on demandait) ;
  - on fournit au LLM la QUESTION qui a été posée, pour qu'il rattache la réponse
    aux bons champs ("2000 au départ" → montant_initial).
"""
import time
import json
import logging

from app.graph.state import GraphState
from app.schemas.business import CASE_SCHEMA_MAP
from app.core.llm import generate_json, LLMError

logger = logging.getLogger("extraction")

# Valeurs textuelles parasites que le LLM produit parfois au lieu d'une absence.
_NULLISH = {
    "null", "none", "nan", "n/a", "na", "",
    "non spécifié", "non specifie", "non renseigné", "non renseigne",
    "inconnu", "aucun", "aucune",
}

# Consigne commune sur la fidélité des nombres (réutilisée dans les deux prompts).
_NUMBER_RULES = """RÈGLES STRICTES SUR LES NOMBRES :
- Recopie chaque nombre EXACTEMENT comme il est écrit dans la demande, chiffre pour chiffre.
- N'ARRONDIS PAS, ne tronque pas, ne supprime pas de zéro. "5000" doit rester 5000 (et non 500).
- "10000 euros" → 10000 ; "200 mois" → 200 ; "5000 euros par mois" → 5000.
- Ignore les séparateurs de milliers : "200 000" ou "200.000" → 200000.
- Montants en euros (nombre décimal), durées en mois (nombre entier).
- N'invente JAMAIS une valeur absente (pas d'IBAN fictif, pas de montant deviné) : mets null."""


def _clean_params(params: dict) -> dict:
    """Convertit les valeurs parasites ('null', 'none', '', ...) en None."""
    if not params:
        return {}
    cleaned = {}
    for k, v in params.items():
        if isinstance(v, str) and v.strip().lower() in _NULLISH:
            cleaned[k] = None
        else:
            cleaned[k] = v
    return cleaned


def build_extraction_prompt(case_name: str, text: str, fields_desc: str) -> str:
    return f"""Tu es un assistant bancaire. Extrais les paramètres de la demande.

Cas métier : {case_name}
Demande : "{text}"

Champs à extraire :
{fields_desc}

{_NUMBER_RULES}
- Remplis chaque champ présent dans la demande, sinon mets la valeur JSON null (PAS la chaîne "null").
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
            schema_class, task="extraction", temperature=0.0,   # ← 0.0 : fidélité maximale
        )
    except LLMError as e:
        logger.warning("extraction LLM échouée : %s", e)
        freshly_extracted = {}

    # Nettoyage des valeurs parasites ("null", "none"...) AVANT fusion
    freshly_extracted = _clean_params(freshly_extracted)

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
        question_asked: str = "",
) -> dict:
    """Extrait les paramètres depuis une réponse de suivi de l'utilisateur.

    IMPORTANT : on extrait TOUS les champs du schéma (pas seulement
    `missing_fields`), car l'utilisateur peut spontanément fournir plus que ce
    qui était demandé. `missing_fields` et `question_asked` ne servent qu'à
    CONTEXTUALISER (aider le LLM à rattacher la réponse aux bons champs), pas à
    restreindre l'extraction.
    """
    schema_class = CASE_SCHEMA_MAP.get(case_name)
    if not schema_class:
        return {}

    # Schéma COMPLET : on autorise l'extraction de tous les champs du cas.
    full_schema = schema_class.model_json_schema()
    props = full_schema.get("properties", {})
    fields_desc = "\n".join(f'- {k} : {v.get("description", "")}' for k, v in props.items())

    # Contexte : on rappelle au LLM la question posée et les champs en attente,
    # pour qu'il rattache correctement une réponse elliptique ("2000 au départ").
    context = ""
    if question_asked:
        context += f'\nQuestion qui a été posée à l\'utilisateur : "{question_asked}"'
    if missing_fields:
        context += f"\nInformations principalement attendues : {missing_fields}"

    prompt = f"""Tu es un assistant bancaire. L'utilisateur complète sa demande en cours.

Cas métier : {case_name}
Paramètres déjà connus : {json.dumps(existing_params, ensure_ascii=False)}{context}
Réponse de l'utilisateur : "{text}"

Extrais TOUTES les valeurs présentes dans la réponse de l'utilisateur, pour
n'importe lequel des champs ci-dessous (pas seulement ceux attendus) :
{fields_desc}

{_NUMBER_RULES}
- Mets la valeur JSON null (PAS la chaîne "null") pour tout champ non mentionné.
- "au départ", "au début", "pour commencer", "initial" → renvoient au versement initial.
"""
    try:
        extracted = generate_json(prompt, full_schema, task="extraction", temperature=0.0)
    except LLMError as e:
        logger.warning("extract_params_with_llm échouée : %s", e)
        return {}

    return _clean_params(extracted)