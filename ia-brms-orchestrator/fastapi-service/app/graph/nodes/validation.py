"""
Nœud de validation.

- Vérifie la structure des données (types, champs obligatoires).
- Vérifie les champs requis avant l'appel à ODM.
- La logique métier reste gérée par le BRMS (ODM).
"""
import time
import logging

from app.graph.state import GraphState
from app.schemas.business import CASE_SCHEMA_MAP

logger = logging.getLogger("validation")

# Nombre maximal d'échecs de validation avant fallback
MAX_RETRIES = 3


# Champs toujours obligatoires pour chaque cas métier
REQUIRED_FIELDS: dict[str, list[str]] = {
    "credit_immobilier":   ["montant", "duree_mois", "revenu_mensuel", "age"],
    "credit_consommation": ["montant", "duree_mois", "revenu_mensuel"],
    "assurance_vie":       ["operation", "age"],
    "carte_bancaire":      ["operation"],
    "virement":            ["type_virement", "montant"],
}


# Champs obligatoires uniquement sous certaines conditions
# Format : case -> liste de (champ_declencheur, valeur_declencheuse, champ_requis)
CONDITIONAL_REQUIRED: dict[str, list[tuple[str, str, str]]] = {
    "assurance_vie": [
        ("operation", "souscription", "montant_initial"),
    ],
    "carte_bancaire": [
        ("operation", "plafond", "plafond_souhaite"),
    ],
}


# Question naturelle associée à chaque champ
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

    # Géré dynamiquement selon le cas métier
    "operation": None,
    "type_virement":         "s'agit-il d'un virement SEPA, instantané, international ou programmé",
    "type_carte":            "votre carte est-elle une Visa ou une Mastercard",
}

# Formulation de "operation" selon le cas métier
OPERATION_QUESTION_BY_CASE: dict[str, str] = {
    "assurance_vie":  "souhaitez-vous effectuer une souscription, un versement ou un rachat",
    "carte_bancaire": "souhaitez-vous faire opposition, modifier le plafond, "
                      "renouveler ou débloquer votre carte",
}



# Nom lisible du cas métier utilisé dans les questions
_CASE_LABELS = {
    "credit_immobilier":   "crédit immobilier",
    "credit_consommation": "crédit à la consommation",
    "assurance_vie":       "assurance vie",
    "carte_bancaire":      "carte bancaire",
    "virement":            "virement",
}

# Retourne une question naturelle pour un champ.
def _human_question(case_name: str, field: str) -> str:


    # "operation" dépend du cas métier
    if field == "operation":
        return OPERATION_QUESTION_BY_CASE.get(
            case_name, "quelle opération souhaitez-vous effectuer"
        )

    # Recherche une formulation prédéfinie
    q = FIELD_QUESTIONS.get(field)
    if q:
        return q
    # Sinon, utiliser une formulation générique
    return f"pourriez-vous préciser {field}"



# Construit la question à poser selon les champs manquants.
def _build_question(case_name: str, missing: list) -> str:
    # Nom lisible du cas métier
    label = _CASE_LABELS.get(case_name, case_name)

    # Convertit chaque champ en question naturelle
    human = [_human_question(case_name, f) for f in missing]

    # Cas d'un seul champ manquant
    if len(human) == 1:
        return (
            f"Pour traiter votre demande de {label}, {human[0]} ?"
        )

    # Assemble plusieurs questions dans une seule phrase
    parts = " ; ".join(human[:-1]) + f" ; et enfin {human[-1]}"
    return (
        f"Pour traiter votre demande de {label}, j'aurais besoin de quelques "
        f"précisions : {parts} ?"
    )



# Retourne les champs obligatoires encore manquants.
def _compute_missing(case_name: str, vd: dict) -> list[str]:

    # Liste des champs absents
    missing: list[str] = []

    # Vérifie les champs toujours obligatoires
    for f in REQUIRED_FIELDS.get(case_name, []):
        if vd.get(f) is None:

            # Champ absent → on l'ajoute
            missing.append(f)

    # Vérifie les champs obligatoires sous condition
    for trigger_field, trigger_value, required_field in CONDITIONAL_REQUIRED.get(case_name, []):

        # Valeur actuelle du champ déclencheur
        actual = vd.get(trigger_field)

        # Normalise la valeur pour une comparaison fiable
        actual_norm = str(actual).strip().lower() if actual is not None else None

        # Si la condition est remplie, le champ devient obligatoire
        if actual_norm == trigger_value and vd.get(required_field) is None:

            # Évite les doublons
            if required_field not in missing:
                missing.append(required_field)

    return missing


def validation_node(state: GraphState) -> GraphState:
    start = time.time()
    case_name    = state["case_selected"]

    # Paramètres extraits par le LLM
    params       = state.get("extracted_params", {}) or {}

    # Nombre d'échecs de validation précédents
    retry_count  = state.get("retry_count", 0)

    # Schéma Pydantic associé au cas métier
    schema_class = CASE_SCHEMA_MAP.get(case_name)

    # Aucun schéma trouvé → fallback
    if not schema_class:
        elapsed = (time.time() - start) * 1000
        return {
            **state,
            "fallback_type":   "C",
            "fallback_reason": "Schéma Pydantic introuvable",
            "latency_ms":      {**state.get("latency_ms", {}), "validation": elapsed},
        }

    try:

        # Ignore les champs dont la valeur est None
        filtered  = {k: v for k, v in params.items() if v is not None}

        # Vérifie les types et valide les données
        validated = schema_class(**filtered)

        # Convertit l'objet Pydantic en dictionnaire
        validated_dict = validated.model_dump()

        # Recherche les champs obligatoires manquants
        missing = _compute_missing(case_name, validated_dict)
        elapsed = (time.time() - start) * 1000

        if missing:

            # Génère une question naturelle
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

        # Incrémente le nombre d'échecs
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

        # Retourne l'erreur pour permettre une nouvelle tentative
        return {
            **state,
            "retry_count":       retry_count,
            "validation_errors": [str(e)],
            "latency_ms":        {**state.get("latency_ms", {}), "validation": elapsed},
        }