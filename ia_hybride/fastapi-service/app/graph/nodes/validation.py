# === Destination : app/graph/nodes/validation.py (remplace l'existant) ===
"""
Nœud de validation : validation STRUCTURELLE (types, présence des champs requis
pour pouvoir appeler l'ODM). La cohérence MÉTIER (seuils d'éligibilité, montants
minimaux, etc.) n'est PAS ici : elle appartient au BRMS (ODM).

CORRECTIF : `duree_mois` est désormais REQUIS pour le crédit consommation.
Sans durée, le mock ODM calculait mensualité = montant (remboursement en 1 mois)
→ taux d'endettement absurde (283%) et refus injustifié.
"""
import time
import logging

from app.graph.state import GraphState
from app.schemas.business import CASE_SCHEMA_MAP

logger = logging.getLogger("validation")

MAX_RETRIES = 3

# Champs sans lesquels l'appel ODM n'a pas de sens → on les collecte auprès de l'user.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "credit_immobilier":   ["montant", "duree_mois", "revenu_mensuel"],
    "credit_consommation": ["montant", "duree_mois", "revenu_mensuel"],   # duree_mois AJOUTÉ
    "assurance_vie":       [],
    "carte_bancaire":      [],
    "virement":            ["montant"],
}

FIELD_LABELS: dict[str, str] = {
    "montant":           "le montant souhaité (en euros)",
    "duree_mois":        "la durée de remboursement souhaitée (en mois)",
    "revenu_mensuel":    "votre revenu mensuel net (en euros)",
    "apport":            "votre apport personnel (en euros)",
    "age":               "votre âge",
    "motif":             "le motif de votre demande",
    "beneficiaire":      "le nom du bénéficiaire",
    "iban":              "l'IBAN du bénéficiaire",
    "type_carte":        "le type de carte (Visa ou Mastercard)",
    "plafond":           "le plafond souhaité (en euros)",
    "montant_initial":   "le versement initial (en euros)",
    "versement_mensuel": "le versement mensuel souhaité (en euros)",
    "duree_ans":         "la durée du contrat (en années)",
}

_CASE_LABELS = {
    "credit_immobilier":   "crédit immobilier",
    "credit_consommation": "crédit à la consommation",
    "assurance_vie":       "assurance vie",
    "carte_bancaire":      "carte bancaire",
    "virement":            "virement",
}


def validation_node(state: GraphState) -> GraphState:
    start = time.time()
    case_name    = state["case_selected"]
    params       = state.get("extracted_params", {}) or {}
    retry_count  = state.get("retry_count", 0)
    schema_class = CASE_SCHEMA_MAP.get(case_name)

    if not schema_class:
        elapsed = (time.time() - start) * 1000
        return {
            **state,
            "fallback_type":   "C",
            "fallback_reason": "Schéma Pydantic introuvable",
            "latency_ms":      {**state.get("latency_ms", {}), "validation": elapsed},
        }

    try:
        filtered  = {k: v for k, v in params.items() if v is not None}
        validated = schema_class(**filtered)
        validated_dict = validated.model_dump()

        # ── Champs requis manquants → collecte auprès de l'utilisateur ──────
        required = REQUIRED_FIELDS.get(case_name, [])
        missing  = [f for f in required if validated_dict.get(f) is None]
        elapsed  = (time.time() - start) * 1000

        if missing:
            labels   = [FIELD_LABELS.get(f, f) for f in missing]
            question = _build_question(case_name, missing, labels)
            logger.info("params manquants pour %s : %s", case_name, missing)
            return {
                **state,
                "extracted_params":         validated_dict,
                "validation_errors":        None,
                "missing_params":           missing,
                "params_collection_needed": True,
                "params_question":          question,
                "latency_ms":               {**state.get("latency_ms", {}), "validation": elapsed},
            }

        return {
            **state,
            "extracted_params":         validated_dict,
            "validation_errors":        None,
            "missing_params":           [],
            "params_collection_needed": False,
            "params_question":          None,
            "latency_ms":               {**state.get("latency_ms", {}), "validation": elapsed},
        }

    except Exception as e:
        retry_count += 1
        elapsed = (time.time() - start) * 1000

        if retry_count >= MAX_RETRIES:
            logger.warning("validation échouée %d fois pour %s : %s", retry_count, case_name, e)
            return {
                **state,
                "retry_count":     retry_count,
                "fallback_type":   "C",
                "fallback_reason": f"Validation échouée {MAX_RETRIES} fois : {e}",
                "response_text":   "Les paramètres fournis sont invalides après plusieurs tentatives.",
                "latency_ms":      {**state.get("latency_ms", {}), "validation": elapsed},
            }

        return {
            **state,
            "retry_count":       retry_count,
            "validation_errors": [str(e)],
            "latency_ms":        {**state.get("latency_ms", {}), "validation": elapsed},
        }


def _build_question(case_name: str, missing: list, labels: list) -> str:
    label = _CASE_LABELS.get(case_name, case_name)
    if len(missing) == 1:
        return f"Pour traiter votre demande de {label}, pourriez-vous m'indiquer {labels[0]} ?"
    parts = ", ".join(labels[:-1]) + f" et {labels[-1]}"
    return (
        f"Pour traiter votre demande de {label}, j'ai besoin de quelques "
        f"informations supplémentaires : {parts}."
    )