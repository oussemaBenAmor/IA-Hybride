# === Destination : app/graph/nodes/validation.py (remplace l'existant) ===
"""
Nœud de validation : validation STRUCTURELLE (types, présence des champs requis
pour pouvoir appeler l'ODM). La cohérence MÉTIER (seuils d'éligibilité, montants
minimaux, etc.) n'est PAS ici : elle appartient au BRMS (ODM).

CORRECTIF duree_mois : requis pour le crédit consommation (sinon mensualité
absurde côté calcul → refus injustifié).

CORRECTIF "champs manquants" (Bug 1) : on n'appelle ODM que lorsque TOUS les
champs nécessaires sont présents. Certains champs sont requis de façon
CONDITIONNELLE (ils ne comptent que selon la valeur d'un autre champ) :
  - assurance_vie : `montant_initial` requis UNIQUEMENT si operation == "souscription".
  - carte_bancaire : `plafond_souhaite` requis UNIQUEMENT si operation == "plafond".
Sans ça, un montant/plafond absent était transformé en 0 côté Java, et une règle
ODM se déclenchait à tort (ex : "versement initial < 1000" avec 0 → REFUSE).

CORRECTIF "langage humain" : les questions posées à l'utilisateur n'emploient
plus de jargon technique ("operation", "type_virement"). Pour les champs à choix
fermé, on propose explicitement les options ("souscription, versement ou rachat").
"""
import time
import logging

from app.graph.state import GraphState
from app.schemas.business import CASE_SCHEMA_MAP

logger = logging.getLogger("validation")

MAX_RETRIES = 3

# ── Champs requis INCONDITIONNELS par cas ─────────────────────────────────────
# (toujours nécessaires pour appeler l'ODM, quelle que soit la sous-opération)
REQUIRED_FIELDS: dict[str, list[str]] = {
    "credit_immobilier":   ["montant", "duree_mois", "revenu_mensuel", "age"],
    "credit_consommation": ["montant", "duree_mois", "revenu_mensuel"],
    "assurance_vie":       ["operation", "age"],
    "carte_bancaire":      ["operation"],
    "virement":            ["type_virement", "montant"],
}


# ── Requis CONDITIONNELS : (champ requis) seulement si (champ_test == valeur) ──
# Format : case -> liste de (champ_declencheur, valeur_declencheuse, champ_requis)
CONDITIONAL_REQUIRED: dict[str, list[tuple[str, str, str]]] = {
    "assurance_vie": [
        ("operation", "souscription", "montant_initial"),
    ],
    "carte_bancaire": [
        ("operation", "plafond", "plafond_souhaite"),
    ],
}


# ── Questions en LANGAGE HUMAIN ───────────────────────────────────────────────
# Pour chaque champ, une formulation naturelle. Les champs à CHOIX FERMÉ
# proposent directement les options possibles (l'utilisateur n'a pas à deviner
# le vocabulaire métier).
FIELD_QUESTIONS: dict[str, str] = {
    # Numériques / texte libre
    "montant":           "quel montant souhaitez-vous (en euros)",
    "duree_mois":        "sur quelle durée souhaitez-vous étaler le remboursement (en mois)",
    "revenu_mensuel":    "quel est votre revenu mensuel net (en euros)",
    "apport":            "de quel apport personnel disposez-vous (en euros)",
    "age":               "quel est votre âge",
    "montant_initial":   "quel montant souhaitez-vous verser au départ (en euros)",
    "versement_mensuel": "quel versement mensuel envisagez-vous (en euros)",
    "duree_ans":         "sur combien d'années souhaitez-vous souscrire",
    "plafond_souhaite":  "quel plafond souhaitez-vous pour votre carte (en euros)",
    "beneficiaire":      "à quel bénéficiaire souhaitez-vous envoyer l'argent",
    "iban":              "quel est l'IBAN du bénéficiaire",

    # Choix fermés → on propose les options en clair
    "operation": None,        # géré dynamiquement par cas (voir _human_question)
    "type_virement":         "s'agit-il d'un virement SEPA, instantané, international ou programmé",
    "type_carte":            "votre carte est-elle une Visa ou une Mastercard",
}

# Les options d'"operation" diffèrent selon le cas métier → formulation dédiée.
OPERATION_QUESTION_BY_CASE: dict[str, str] = {
    "assurance_vie":  "souhaitez-vous effectuer une souscription, un versement ou un rachat",
    "carte_bancaire": "souhaitez-vous faire opposition, modifier le plafond, "
                      "renouveler ou débloquer votre carte",
}

_CASE_LABELS = {
    "credit_immobilier":   "crédit immobilier",
    "credit_consommation": "crédit à la consommation",
    "assurance_vie":       "assurance vie",
    "carte_bancaire":      "carte bancaire",
    "virement":            "virement",
}


def _human_question(case_name: str, field: str) -> str:
    """Renvoie la formulation humaine d'UN champ, en tenant compte du cas pour
    les champs à choix fermé dépendants du cas (operation)."""
    if field == "operation":
        return OPERATION_QUESTION_BY_CASE.get(
            case_name, "quelle opération souhaitez-vous effectuer"
        )
    q = FIELD_QUESTIONS.get(field)
    if q:
        return q
    # Fallback : si un champ n'a pas de formulation dédiée, on reste neutre.
    return f"pourriez-vous préciser {field}"


def _build_question(case_name: str, missing: list) -> str:
    """Construit une question naturelle pour un ou plusieurs champs manquants."""
    label = _CASE_LABELS.get(case_name, case_name)
    human = [_human_question(case_name, f) for f in missing]

    if len(human) == 1:
        return (
            f"Pour traiter votre demande de {label}, {human[0]} ?"
        )

    # Plusieurs champs : on enchaîne proprement.
    parts = " ; ".join(human[:-1]) + f" ; et enfin {human[-1]}"
    return (
        f"Pour traiter votre demande de {label}, j'aurais besoin de quelques "
        f"précisions : {parts} ?"
    )


def _compute_missing(case_name: str, vd: dict) -> list[str]:
    """Champs manquants = requis inconditionnels absents + requis conditionnels
    déclenchés et absents. L'ordre préserve : inconditionnels d'abord."""
    missing: list[str] = []

    # 1) Requis inconditionnels
    for f in REQUIRED_FIELDS.get(case_name, []):
        if vd.get(f) is None:
            missing.append(f)

    # 2) Requis conditionnels
    for trigger_field, trigger_value, required_field in CONDITIONAL_REQUIRED.get(case_name, []):
        actual = vd.get(trigger_field)
        actual_norm = str(actual).strip().lower() if actual is not None else None
        if actual_norm == trigger_value and vd.get(required_field) is None:
            if required_field not in missing:
                missing.append(required_field)

    return missing


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

        # ── Champs requis (inconditionnels + conditionnels) ────────────────
        missing = _compute_missing(case_name, validated_dict)
        elapsed = (time.time() - start) * 1000

        if missing:
            question = _build_question(case_name, missing)
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
                "response_text":   "Les informations fournies n'ont pas pu être validées après plusieurs tentatives.",
                "latency_ms":      {**state.get("latency_ms", {}), "validation": elapsed},
            }

        return {
            **state,
            "retry_count":       retry_count,
            "validation_errors": [str(e)],
            "latency_ms":        {**state.get("latency_ms", {}), "validation": elapsed},
        }