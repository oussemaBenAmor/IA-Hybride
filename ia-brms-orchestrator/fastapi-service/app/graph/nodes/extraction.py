"""
Extraction des paramètres métier avec un LLM.
Les valeurs extraites sont nettoyées et vérifiées pour éviter
les hallucinations, notamment sur les nombres.
"""
import re
import time
import json
import typing
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

def _is_numeric_annotation(annotation) -> bool:
    # Récupère les types internes  dans Optional[int], Union[int, None],Optional[float]
    args = typing.get_args(annotation)
    if args:
        # Vérifie si l'un des types est int ou float
        return any(a in (int, float) for a in args)
    # Cas simple : annotation directement int ou float
    return annotation in (int, float)


#Detecte automatiquement les champs numeriques de tous les schemas metier
def _compute_numeric_fields() -> set:
    # Ensemble qui contiendra les noms des champs numériques trouvés.
    numeric = set()

    # Parcourt tous les schémas métier enregistrés.
    for schema_class in CASE_SCHEMA_MAP.values():

        # Parcourt tous les champs définis dans le schéma courant.
        for field_name, field_info in schema_class.model_fields.items():

            # Vérifie si le type du champ est numérique (int ou float).
            if _is_numeric_annotation(field_info.annotation):

                # Ajoute le nom du champ dans l'ensemble des champs numériques.
                numeric.add(field_name)

    # Retourne tous les champs numériques détectés.
    return numeric


# Champs numeriques detectes depuis les schemas.
_NUMERIC_FIELDS = _compute_numeric_fields()

# Consigne commune sur la fidelite des nombres
_NUMBER_RULES = """RÈGLES STRICTES SUR LES NOMBRES :
- Recopie chaque nombre EXACTEMENT comme il est écrit dans la demande, chiffre pour chiffre.
- N'ARRONDIS PAS, ne tronque pas, ne supprime pas de zéro. "5000" doit rester 5000 (et non 500).
- "10000 euros" → 10000 ; "200 mois" → 200 ; "5000 euros par mois" → 5000.
- Ignore les séparateurs de milliers : "200 000" ou "200.000" → 200000.
- Montants en euros (nombre décimal), durées en mois (nombre entier).

RÈGLE ABSOLUE SUR LES CHAMPS ABSENTS (anti-invention) :
- Si une information n'apparaît PAS littéralement dans le message, mets null.
- N'invente JAMAIS un montant, une durée, un revenu ou un âge "plausible".
- Un champ vide (null) est TOUJOURS préférable à une valeur devinée.
- Avant de remplir un champ numérique, vérifie que le chiffre est ÉCRIT dans le
  message. Si tu ne le vois pas écrit, mets null — ne le déduis pas, ne l'estime pas.
- Ne fournis pas d'IBAN, de bénéficiaire ou de nombre fictif : en cas de doute, null."""




#Convertit les valeurs parasites ('null', 'none', '', ...) en None.
def _clean_params(params: dict) -> dict:
    if not params:
        return {}

    # Nouveau dictionnaire contenant les valeurs nettoyées.
    cleaned = {}

    # Parcourt chaque paramètre sous forme clé / valeur.
    for k, v in params.items():


        # Si la valeur est une chaîne et correspond à une valeur vide connue,
        # on la remplace par None.
        if isinstance(v, str) and v.strip().lower() in _NULLISH:
            cleaned[k] = None
        else:
            cleaned[k] = v

    # Retourne les paramètres nettoyés.
    return cleaned




# Extrait les nombres présents dans un texte
def _numbers_in_text(text: str) -> set:

    # Aucun texte => aucun nombre trouvé
    if not text:
        return set()

    # Supprime les séparateurs
    cleaned = re.sub(r"(?<=\d)[ .\u00a0](?=\d{3}\b)", "", text)

    # Ensemble qui contiendra les nombres trouvés.
    nums: set = set()

    # Recherche tous les nombres dans le texte
    for m in re.findall(r"\d+(?:[.,]\d+)?", cleaned):
        try:

            # Convertit le texte trouvé en nombre décimal
            f = float(m.replace(",", "."))


        # Ignore les valeurs impossibles à convertir
        except ValueError:
            continue

        # Ajoute la version float du nombre
        nums.add(f)

        # Ajoute la version entière si le nombre est un entier.
        if f.is_integer():
            nums.add(int(f))

    # Retourne tous les nombres détectés.
    return nums


# Vérifie que le nombre extrait existe dans le texte source.
def _is_number_grounded(value, text_numbers: set) -> bool:

    if value is None:
        return True

    # Convertit la valeur en nombre si possible
    try:
        v = float(value)
    except (ValueError, TypeError):

        # Ignore les champs non numériques
        return True

        # Vérifie la présence du nombre décimal
    if v in text_numbers:
        return True

    # Vérifie aussi la version entière
    if v.is_integer() and int(v) in text_numbers:
        return True

    # Nombre absent du texte
    return False



# Supprime les valeurs numériques absentes du texte source
def _drop_hallucinated_numbers(params: dict, source_text: str) -> dict:


    # Extrait tous les nombres présents dans le texte
    text_numbers = _numbers_in_text(source_text)

    out = dict(params)

    # Vérifie chaque champ numérique
    for field in _NUMERIC_FIELDS:
        if field in out and out[field] is not None:

            # Annule les nombres qui n'existent pas dans le texte
            if not _is_number_grounded(out[field], text_numbers):
                logger.warning(
                    "valeur non ancrée annulée : %s=%s absent du texte %r "
                    "(nombres détectés=%s)",
                    field, out[field], source_text[:60],
                    sorted(n for n in text_numbers if isinstance(n, int)),
                )
                out[field] = None
    return out


# ==============================================================================

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

    # Vérifie qu'un schéma existe pour ce cas métier
    if not schema_class:
        elapsed = (time.time() - start) * 1000
        return {**state, "fallback_type": "C",
                "fallback_reason": f"Schéma inconnu pour {case_name}",
                "latency_ms": {**state.get("latency_ms", {}), "extraction": elapsed}}

    # Prépare la description des champs pour guider le LLM
    props = schema_class.model_json_schema().get("properties", {})
    fields_desc = "\n".join(f'- {k} : {v.get("description", "")}' for k, v in props.items())

    try:

        # Extrait les paramètres métier avec le LLM
        freshly_extracted = generate_json(
            build_extraction_prompt(case_name, text, fields_desc),
            schema_class, task="extraction", temperature=0.0,   # 0.0 : fidelite maximale
        )
    except LLMError as e:
        logger.warning("extraction LLM échouée : %s", e)
        freshly_extracted = {}

    # Nettoyage des valeurs parasites ("null", "none"...)
    freshly_extracted = _clean_params(freshly_extracted)


    # Supprime les nombres absents du texte utilisateur
    freshly_extracted = _drop_hallucinated_numbers(freshly_extracted, text)

    # Récupère les paramètres valides des tours précédents
    inherited = {k: v for k, v in (state.get("extracted_params") or {}).items() if v is not None}

    # Fusionne les anciennes valeurs avec les nouvelles
    merged = {**inherited}
    for k, v in freshly_extracted.items():
        if v is not None:
            merged[k] = v

    elapsed = (time.time() - start) * 1000
    logger.info("extraction %s → %s", case_name, merged)
    return {**state, "extracted_params": merged,
            "latency_ms": {**state.get("latency_ms", {}), "extraction": elapsed}}




# Extrait les paramètres métier depuis une réponse de suivi de l'utilisateur.
def extract_params_with_llm(
        case_name: str,
        text: str,
        existing_params: dict,
        missing_fields: list,
        question_asked: str = "",
) -> dict:


    # Récupère le schéma associé au cas métier
    schema_class = CASE_SCHEMA_MAP.get(case_name)
    if not schema_class:
        return {}

    # Prépare la liste des champs attendus pour le LLM
    full_schema = schema_class.model_json_schema()
    props = full_schema.get("properties", {})
    fields_desc = "\n".join(f'- {k} : {v.get("description", "")}' for k, v in props.items())

    # Ajoute les informations du tour précédent pour aider le LLM à interpréter la réponse
    context = ""

    # Indique la question posée afin de comprendre à quel besoin répond l'utilisateur
    if question_asked:
        context += f'\nQuestion qui a été posée à l\'utilisateur : "{question_asked}"'

    # Précise les champs encore attendus pour orienter l'extraction
    if missing_fields:
        context += f"\nInformations principalement attendues : {missing_fields}"

    # Construit le prompt d'extraction
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

        # Extrait les paramètres avec le LLM
        extracted = generate_json(prompt, full_schema, task="extraction", temperature=0.0)
    except LLMError as e:
        logger.warning("extract_params_with_llm échouée : %s", e)
        return {}


    # Nettoie les valeurs invalides ou parasites
    extracted = _clean_params(extracted)

    # Supprime les nombres inventés par rapport à la réponse utilisateur
    extracted = _drop_hallucinated_numbers(extracted, text)
    return extracted