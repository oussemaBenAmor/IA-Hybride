"""
Génère la réponse finale du client à partir de la décision du moteur de règles
(ODM), sans inventer d'informations.
"""
import time
import json
import logging

from app.graph.state import GraphState
from app.core.llm import generate_text, LLMError

logger = logging.getLogger("generation")

# Cas pour lesquels les notions de crédit (mensualité, taux, capacité) ont un sens.
CREDIT_CASES = {"credit_immobilier", "credit_consommation"}

# Bloc injecté UNIQUEMENT pour les crédits.
_CREDIT_BLOCK = """CORRESPONDANCE QUESTION → CLÉ DES DÉTAILS (à respecter STRICTEMENT) :
- "montant maximal que je peux emprunter" → "montant_max_empruntable_sur_duree_demandee"
  (ou "montant_max_empruntable_sur_300_mois" si aucune durée n'est fixée).
  ⚠️ Ce n'est JAMAIS "montant_demande" : le montant demandé par le client n'est pas un maximum.
- "mensualité maximale" / "combien je peux rembourser par mois" → "mensualite_max_supportable"
- "durée minimale" / "sur combien de mois" → "duree_minimale_pour_montant_demande_mois"
- "ma mensualité" / "combien je paierai par mois" → "mensualite_estimee"
- "mon taux d'endettement" → "taux_endettement"
Si la clé correspondant à la question N'EXISTE PAS dans les détails ci-dessus,
dis honnêtement que cette information n'est pas disponible et qu'un conseiller
pourra la calculer — n'invente RIEN et ne substitue PAS une autre valeur.
"""


def build_generation_prompt(state: GraphState) -> str:

    # Récupère la décision renvoyée par ODM
    odm = state.get("odm_decision") or {}

    # Récupère les valeurs calculées par ODM
    details = odm.get("details") or {}
    case = state.get("case_selected")

    # Indique si le cas traité est un crédit
    is_credit = case in CREDIT_CASES

    # Le bloc crédit (et son vocabulaire) n'apparaît QUE pour les crédits.
    credit_block = _CREDIT_BLOCK if is_credit else ""

    # Consigne de cadrage différente selon crédit / non-crédit.
    if is_credit:
        scope_rule = (
            "4. Tu peux parler de mensualité, de taux d'endettement et de capacité "
            "d'emprunt, mais UNIQUEMENT à partir des clés présentes dans les détails "
            "ci-dessus (jamais de chiffre inventé)."
        )
    else:
        scope_rule = (
            "4. INTERDICTION ABSOLUE de mentionner les notions de crédit : ne parle "
            "JAMAIS de mensualité, de taux d'endettement, de capacité ou de montant "
            "empruntable, ni de durée de remboursement. Ces notions n'existent pas "
            "pour ce service. Ne dis pas non plus qu'elles « ne sont pas disponibles » "
            "— n'en parle pas du tout. Réponds seulement avec la décision et les "
            "règles déclenchées."
        )

    return f"""Tu es un conseiller bancaire professionnel. Réponds au message du client
en t'appuyant UNIQUEMENT sur la décision du moteur de règles et ses détails.

Message du client : "{state.get("input_corrected") or state["input_raw"]}"
Cas traité : {case}
Paramètres du dossier : {json.dumps(state.get("extracted_params"), ensure_ascii=False)}
Décision : {odm.get("decision")}
Règles déclenchées : {state.get("rules_fired")}
Détails calculés par le moteur de règles :
{json.dumps(details, ensure_ascii=False, indent=2)}
{credit_block}
CONSIGNES :
1. Si le message du client est une QUESTION, réponds D'ABORD et DIRECTEMENT à sa question.
2. Si la décision est un refus, explique brièvement pourquoi (règles déclenchées) et
   propose les alternatives chiffrées disponibles dans les détails, s'il y en a.
3. Si les détails contiennent "reorientation_suggeree", informe le client que sa
   demande correspond mieux à cet autre produit et explique pourquoi
   ("reorientation_raison").
{scope_rule}
5. N'INVENTE AUCUN CHIFFRE : chaque nombre de ta réponse doit provenir des détails
   ou des paramètres ci-dessus.
6. Réponds en français, professionnel et bienveillant, en 2 à 4 phrases.
"""


# Génère la réponse finale du client
def generation_node(state: GraphState) -> GraphState:
    start = time.time()
    try:
        text = generate_text(
            build_generation_prompt(state),
            task="generation",
            temperature=0.3,
        )
    except LLMError as e:
        logger.warning("génération LLM échouée : %s", e)
        text = "Une erreur est survenue lors de la génération de la réponse."

    elapsed = (time.time() - start) * 1000
    return {
        **state,
        "response_text": text,
        "latency_ms": {**state.get("latency_ms", {}), "generation": elapsed},
    }