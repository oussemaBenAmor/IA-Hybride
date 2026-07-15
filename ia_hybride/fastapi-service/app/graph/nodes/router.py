"""
Routeur hybride :
- utilise la recherche vectorielle pour trouver les cas proches ;
- utilise un chemin rapide si la demande est claire ;
- utilise le LLM pour résoudre les cas ambigus ;
- demande une clarification si plusieurs services sont possibles.
"""
import time
import logging

from app.graph.state import GraphState
from app.db.vector_store import search_similar_cases
from app.core.llm import generate_json, LLMError

logger = logging.getLogger("router")

# Liste des services bancaires disponibles
CASE_LABELS = {
    "credit_immobilier":   "Crédit immobilier",
    "credit_consommation": "Crédit à la consommation",
    "assurance_vie":       "Assurance vie",
    "carte_bancaire":      "Carte bancaire",
    "virement":            "Virement",
}

# Ensemble des services valides(les clés du dict CASE_LABELS
VALID_CASES = set(CASE_LABELS)


# Description courte envoyée au LLM pour chaque service
CASE_ONE_LINER = {
    "credit_immobilier":   "financer l'achat d'un logement, d'une maison ou d'un appartement (montants élevés, longue durée)",
    "credit_consommation": "prêt personnel pour voiture, travaux, loisirs ou petit besoin d'argent (petits montants, court terme)",
    "assurance_vie":       "épargne, placement long terme, retraite, souscription/rachat d'un contrat d'assurance vie",
    "carte_bancaire":      "opposition, plafond, renouvellement ou déblocage d'une carte de paiement",
    "virement":            "envoyer ou transférer de l'argent vers un bénéficiaire (SEPA, instantané, international)",
}

# Seuil pour considérer une demande hors domaine bancaire
OUT_OF_SCOPE_FLOOR  = 0.45

# Seuil pour accepter directement sans LLM
FAST_PATH_MIN_SCORE = 0.70

# Écart minimum entre le premier et le deuxième résultat
FAST_PATH_MIN_GAP   = 0.15

# Format attendu de la réponse du LLM
_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "case":         {"type": "string"},
        "confidence":   {"type": "number"},
        "ambiguous":    {"type": "boolean"},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "reason":       {"type": "string"},
    },
    "required": ["case", "confidence", "ambiguous"],
}



# Retourne le nom lisible d'un service (credit_immobilier => Crédit immobilier)
def _label(case_name: str) -> str:
    return CASE_LABELS.get(case_name, case_name)


# Génère une question lorsque plusieurs services sont possibles
def _clarification_question(cases: list[str]) -> str:

    # Transforme les cas valides en noms lisibles séparés par "ou"
    # Exemple : ["virement", "carte_bancaire"] → "Virement" ou "Carte bancaire"
    items = " ou ".join(f"« {_label(c)} »" for c in cases if c in VALID_CASES)
    if not items:

        # Aucun cas valide trouvé → utilise tous les services disponibles
        items = ", ".join(_label(c) for c in CASE_LABELS)
    return f"Votre demande concerne-t-elle {items} ? Pourriez-vous préciser ?"


# Garde uniquement les deux meilleurs candidats
def _top2(candidates: list[dict]) -> list[dict]:
    return [{"case": c["case_name"], "score": c["score"]} for c in candidates[:2]]


# Retourne un cas accepté par le router
def _accept(state, case_name, confidence, candidates, start) -> GraphState:
    elapsed = (time.time() - start) * 1000
    logger.info("cas sélectionné = %s (conf=%.3f)", case_name, confidence)
    return {
        **state,
        "case_selected": case_name,
        "confidence":    confidence,
        "top2_scores":   _top2(candidates),
        "latency_ms":    {**state.get("latency_ms", {}), "router": elapsed},
    }



# Retourne une réponse hors périmètre
def _fallback_a(state, reason, candidates, start) -> GraphState:
    elapsed = (time.time() - start) * 1000
    logger.info("Fallback A : %s", reason)
    return {
        **state,
        "fallback_type":   "A",
        "fallback_reason": reason,
        "response_text":   "Cette demande est hors périmètre de nos services.",
        "top2_scores":     _top2(candidates),
        "latency_ms":      {**state.get("latency_ms", {}), "router": elapsed},
    }



# Retourne une demande de clarification
def _fallback_b(state, cases, candidates, start) -> GraphState:
    elapsed = (time.time() - start) * 1000
    question = _clarification_question(cases)
    logger.info("Fallback B (ambiguïté) entre %s", cases)

    # Récupération des scores des candidats
    score_map = {c["case_name"]: c["score"] for c in candidates}
    presented = [{"case": c, "score": score_map.get(c, 0.0)} for c in cases[:3]]
    return {
        **state,
        "fallback_type":          "B",
        "fallback_reason":        f"Ambiguïté entre {cases}",
        "clarification_needed":   True,
        "clarification_question": question,
        "response_text":          question,
        "top2_scores":            presented,
        "latency_ms":             {**state.get("latency_ms", {}), "router": elapsed},
    }



# Classification avec le LLM pour les cas ambigus
def _llm_classify(query: str, candidates: list[dict] | None = None) -> dict | None:

    # Prépare la liste des services à donner au LLM
    options = "\n".join(
        f"- {case_name} : {CASE_ONE_LINER[case_name]}"
        for case_name in CASE_LABELS
    )
    prompt = f"""Tu es un routeur de demandes bancaires. Ton rôle est de déterminer
le service demandé À PARTIR DE CE QUE L'UTILISATEUR A RÉELLEMENT ÉCRIT.

Demande de l'utilisateur : "{query}"

Services possibles :
{options}
- hors_perimetre : la demande ne correspond à AUCUN service bancaire ci-dessus

DISTINCTION IMPORTANTE — "hors_perimetre" et "ambiguous" sont MUTUELLEMENT EXCLUSIFS :
- "case": "hors_perimetre" → la demande ne correspond à AUCUN des services listés.
  Exemples : "quelle est la météo", "raconte-moi une blague", "j'ai faim".
- "ambiguous": true → la demande correspond à PLUSIEURS services listés sans pouvoir trancher.
  Exemples : "je veux un crédit" (immo OU conso), "je cherche à placer mon argent" (plusieurs produits).

⚠️ Une demande ambiguë N'EST PAS hors périmètre. Si tu hésites entre 2 services bancaires
LISTÉS, mets ambiguous=true et choisis le plus plausible dans "case" ; ne mets PAS "hors_perimetre".

RÈGLE FONDAMENTALE :
Tu ne dois choisir un service unique QUE si la demande contient une information qui le
désigne SANS DOUTE possible. Si la demande est compatible avec PLUSIEURS services parce
qu'elle est trop vague, tu DOIS répondre ambiguous=true et lister ces services dans
"alternatives".

N'utilise JAMAIS la probabilité ou la fréquence pour deviner. Si l'utilisateur dit
"je veux un crédit" sans préciser, c'est ambigu (immo OU conso) : tu ne dois pas choisir
le plus courant, tu dois mettre ambiguous=true et alternatives=["credit_immobilier", "credit_consommation"].

Avant de répondre, demande-toi :
- La demande désigne-t-elle un service précis sans ambiguïté ? → si oui, ce case + ambiguous=false.
- La demande pourrait-elle correspondre à plusieurs services listés ? → si oui, ambiguous=true + alternatives remplis.
- La demande ne correspond à AUCUN service bancaire listé ? → seulement alors, case="hors_perimetre".

Réponds en JSON :
- "case" : l'identifiant EXACT du service (ex: "credit_immobilier"), OU "hors_perimetre"
  uniquement si aucun service ne convient, OU le service le plus plausible si ambigu
- "confidence" : entre 0 et 1, ta certitude que la demande DÉSIGNE ce service sans ambiguïté
- "ambiguous" : true si la demande est compatible avec plusieurs services listés
- "alternatives" : OBLIGATOIRE si ambiguous=true — liste d'AU MOINS 2 identifiants de services
  ci-dessus qui correspondent au doute. Sinon, liste vide [].
- "reason" : une courte phrase justifiant ton choix
"""
    try:

        # Appel LLM avec sortie JSON contrôlée et avec prompt générée
        return generate_json(prompt, _CLASSIFY_SCHEMA, task="classifier", temperature=0.0)
    except LLMError as e:
        logger.warning("classify LLM indisponible : %s", e)
        return None



# Nœud principal du routeur
def router_node(state: GraphState) -> GraphState:
    start = time.time()

    # Utilise le texte corrigé si disponible
    query = state.get("input_corrected") or state["input_raw"]

    # Recherche des services proches avec le bi-encoder
    candidates = search_similar_cases(query, top_k=5)

    # Aucun service trouvé
    if not candidates:
        return _fallback_a(state, "Aucun cas métier trouvé", [], start)

    # Score du meilleur candidat
    top1_score = candidates[0]["score"]

    # Différence entre les deux meilleurs scores
    gap = top1_score - candidates[1]["score"] if len(candidates) >= 2 else 1.0
    logger.info("bi-encoder top1=%s (%.3f) gap=%.3f",
                candidates[0]["case_name"], top1_score, gap)


    # Cas trop éloigné des services bancaires
    if top1_score < OUT_OF_SCOPE_FLOOR:
        return _fallback_a(state, f"cosinus trop faible ({top1_score:.3f})", candidates, start)

    # Cas évident : pas besoin du LLM
    if top1_score >= FAST_PATH_MIN_SCORE and gap >= FAST_PATH_MIN_GAP:
        logger.info("chemin rapide (sans LLM)")
        return _accept(state, candidates[0]["case_name"], top1_score, candidates, start)

    # Cas incertain : appel au LLM avec tous les cas
    decision = _llm_classify(query)

    # Si le LLM échoue, on utilise le meilleur résultat trouvé
    if decision is None:
        return _accept(state, candidates[0]["case_name"], top1_score, candidates, start)

    # Extraction de la décision du LLM
    case      = (decision.get("case") or "").strip()
    conf      = float(decision.get("confidence") or top1_score)
    ambiguous = bool(decision.get("ambiguous"))

    # Garde uniquement les alternatives proposées par le LLM qui existent réellement
    alts      = [c for c in (decision.get("alternatives") or []) if c in VALID_CASES]

    # Récupère l'explication du choix donnée par le LLM
    reason    = decision.get("reason", "")

    # Affiche la décision du LLM dans les logs pour le suivi
    if reason:
        logger.info("LLM: case=%s ambiguous=%s alts=%s reason=%s",
                    case, ambiguous, alts, reason)

    # Si plusieurs services sont possibles, demander une précision à l'utilisateur
    if ambiguous:

        # Garde le cas principal seulement s'il existe
        primary = [case] if case in VALID_CASES else []

        # Fusion sans doublons
        choices = list(dict.fromkeys(primary + alts))

        # Plusieurs choix restants → clarification nécessaire
        if len(choices) >= 2:
            return _fallback_b(state, choices[:3], candidates, start)


    # Le LLM confirme que la demande ne correspond à aucun service bancaire
    if case == "hors_perimetre":
        return _fallback_a(state, "LLM → hors_perimetre", candidates, start)

    # Protection contre un cas inventé par le LLM
    # Si le cas n'existe pas dans la liste de services
    if case not in VALID_CASES:
        if top1_score >= FAST_PATH_MIN_SCORE:   # Le bi-encoder est très sûr → on utilise son résultat
            return _accept(state, candidates[0]["case_name"], top1_score, candidates, start)

        # Ni le LLM ni le bi-encoder ne sont fiables
        return _fallback_a(state, f"LLM → {case!r}", candidates, start)


    # Cas valide choisi par le LLM → on accepte la classification
    return _accept(state, case, conf, candidates, start)