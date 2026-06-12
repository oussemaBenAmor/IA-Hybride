# === Destination : app/graph/nodes/generation.py (remplace l'existant) ===
"""
Nœud de génération : répond au message du client en s'appuyant UNIQUEMENT sur
la décision du moteur de règles (ODM) et ses détails chiffrés.

DURCISSEMENT : le LLM avait présenté `montant_demande` (8 500 €) comme étant le
"montant maximal empruntable". Le prompt mappe maintenant EXPLICITEMENT chaque
type de question vers la bonne clé des détails, et interdit de présenter le
montant demandé comme un maximum. Si la clé attendue est absente des détails
(ex: ancien OdmService non redéployé), le LLM doit le dire honnêtement.
"""
import time
import json
import logging

from app.graph.state import GraphState
from app.core.llm import generate_text, LLMError

logger = logging.getLogger("generation")


def build_generation_prompt(state: GraphState) -> str:
    odm = state.get("odm_decision") or {}
    details = odm.get("details") or {}
    return f"""Tu es un conseiller bancaire professionnel. Réponds au message du client
en t'appuyant UNIQUEMENT sur la décision du moteur de règles et ses détails chiffrés.

Message du client : "{state.get("input_corrected") or state["input_raw"]}"
Cas traité : {state.get("case_selected")}
Paramètres du dossier : {json.dumps(state.get("extracted_params"), ensure_ascii=False)}
Décision : {odm.get("decision")}
Règles déclenchées : {state.get("rules_fired")}
Détails et alternatives calculés par le moteur de règles :
{json.dumps(details, ensure_ascii=False, indent=2)}

CORRESPONDANCE QUESTION → CLÉ DES DÉTAILS (à respecter STRICTEMENT) :
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

CONSIGNES :
1. Si le message du client est une QUESTION, réponds D'ABORD et DIRECTEMENT à sa
   question en utilisant la clé correspondante (voir la table ci-dessus).
2. Si la décision est un refus, explique brièvement pourquoi (règles déclenchées) et
   propose les alternatives chiffrées disponibles dans les détails.
3. Si les détails contiennent "reorientation_suggeree", informe le client que sa
   demande correspond mieux à cet autre produit et explique pourquoi
   ("reorientation_raison").
4. N'INVENTE AUCUN CHIFFRE : chaque nombre de ta réponse doit provenir des détails
   ou des paramètres ci-dessus.
5. Réponds en français, professionnel et bienveillant, en 2 à 4 phrases.
"""


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