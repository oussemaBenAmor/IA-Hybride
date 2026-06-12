# === Destination : app/graph/nodes/router.py (remplace l'existant) ===
"""
Router hybride simplifié.

Avant : bi-encoder + cross-encoder (reranker) + 3 modes + ~6 seuils réglés à la
main, fragiles (on a vu 3 seuils mal calibrés casser des requêtes valides).

Maintenant :
  1. Le bi-encoder (pgvector) fournit une *liste courte* de candidats plausibles
     (rapide, scalable). Il sert UNIQUEMENT de filtre de vitesse :
       - décider si on rejette tout de suite (hors périmètre),
       - décider si c'est assez évident pour court-circuiter le LLM (chemin rapide).
  2. Dès qu'on appelle le LLM, on lui présente TOUS les cas métier dans un ordre
     FIXE et NEUTRE — on ne lui transmet pas le classement (bruité) du bi-encoder,
     pour ne pas biaiser sa décision d'ambiguïté par l'ordre des scores.
  3. Le prompt force le LLM à n'choisir un cas QUE si un élément de la demande le
     désigne sans doute ; sinon il doit déclarer ambiguous=true. On neutralise
     ainsi son réflexe de deviner le cas le plus fréquent (ex : "je veux un crédit"
     → il ne doit PAS défaulter vers conso, il doit demander immo ou conso).
  4. L'AMBIGUÏTÉ est prioritaire sur le hors-périmètre : si le LLM hésite entre
     deux cas, on demande à l'utilisateur, même s'il a (à tort) coché aussi
     "hors_perimetre".
"""
import time
import logging

from app.graph.state import GraphState
from app.db.vector_store import search_similar_cases
from app.core.llm import generate_json, LLMError

logger = logging.getLogger("router")

# ── Cas métier ────────────────────────────────────────────────────────────────
CASE_LABELS = {
    "credit_immobilier":   "Crédit immobilier",
    "credit_consommation": "Crédit à la consommation",
    "assurance_vie":       "Assurance vie",
    "carte_bancaire":      "Carte bancaire",
    "virement":            "Virement",
}
VALID_CASES = set(CASE_LABELS)

CASE_ONE_LINER = {
    "credit_immobilier":   "financer l'achat d'un logement, d'une maison ou d'un appartement (montants élevés, longue durée)",
    "credit_consommation": "prêt personnel pour voiture, travaux, loisirs ou petit besoin d'argent (petits montants, court terme)",
    "assurance_vie":       "épargne, placement long terme, retraite, souscription/rachat d'un contrat d'assurance vie",
    "carte_bancaire":      "opposition, plafond, renouvellement ou déblocage d'une carte de paiement",
    "virement":            "envoyer ou transférer de l'argent vers un bénéficiaire (SEPA, instantané, international)",
}

# ── Seuils (à recalibrer sur l'éval) ──────────────────────────────────────────
OUT_OF_SCOPE_FLOOR  = 0.45
FAST_PATH_MIN_SCORE = 0.70
FAST_PATH_MIN_GAP   = 0.15

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


def _label(case_name: str) -> str:
    return CASE_LABELS.get(case_name, case_name)


def _clarification_question(cases: list[str]) -> str:
    items = " ou ".join(f"« {_label(c)} »" for c in cases if c in VALID_CASES)
    if not items:
        items = ", ".join(_label(c) for c in CASE_LABELS)
    return f"Votre demande concerne-t-elle {items} ? Pourriez-vous préciser ?"


def _top2(candidates: list[dict]) -> list[dict]:
    return [{"case": c["case_name"], "score": c["score"]} for c in candidates[:2]]


# ── Sorties standard ──────────────────────────────────────────────────────────

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


def _fallback_b(state, cases, candidates, start) -> GraphState:
    elapsed = (time.time() - start) * 1000
    question = _clarification_question(cases)
    logger.info("Fallback B (ambiguïté) entre %s", cases)
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


# ── Décision LLM ──────────────────────────────────────────────────────────────

def _llm_classify(query: str, candidates: list[dict] | None = None) -> dict | None:
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
        return generate_json(prompt, _CLASSIFY_SCHEMA, task="classifier", temperature=0.0)
    except LLMError as e:
        logger.warning("classify LLM indisponible : %s", e)
        return None


# ── Nœud ──────────────────────────────────────────────────────────────────────

def router_node(state: GraphState) -> GraphState:
    start = time.time()
    query = state.get("input_corrected") or state["input_raw"]

    candidates = search_similar_cases(query, top_k=5)
    if not candidates:
        return _fallback_a(state, "Aucun cas métier trouvé", [], start)

    top1_score = candidates[0]["score"]
    gap = top1_score - candidates[1]["score"] if len(candidates) >= 2 else 1.0
    logger.info("bi-encoder top1=%s (%.3f) gap=%.3f",
                candidates[0]["case_name"], top1_score, gap)

    # 1) Filet hors-périmètre bon marché
    if top1_score < OUT_OF_SCOPE_FLOOR:
        return _fallback_a(state, f"cosinus trop faible ({top1_score:.3f})", candidates, start)

    # 2) Chemin rapide
    if top1_score >= FAST_PATH_MIN_SCORE and gap >= FAST_PATH_MIN_GAP:
        logger.info("chemin rapide (sans LLM)")
        return _accept(state, candidates[0]["case_name"], top1_score, candidates, start)

    # 3) Zone incertaine → LLM avec tous les cas
    decision = _llm_classify(query)
    if decision is None:
        return _accept(state, candidates[0]["case_name"], top1_score, candidates, start)

    case      = (decision.get("case") or "").strip()
    conf      = float(decision.get("confidence") or top1_score)
    ambiguous = bool(decision.get("ambiguous"))
    alts      = [c for c in (decision.get("alternatives") or []) if c in VALID_CASES]
    reason    = decision.get("reason", "")
    if reason:
        logger.info("LLM: case=%s ambiguous=%s alts=%s reason=%s",
                    case, ambiguous, alts, reason)

    # 4) AMBIGUÏTÉ — PRIORITAIRE sur tout le reste
    # Si le LLM signale une ambiguïté avec au moins 2 alternatives valides,
    # on demande à l'utilisateur — même si le LLM a (à tort) coché "hors_perimetre".
    if ambiguous:
        primary = [case] if case in VALID_CASES else []
        choices = list(dict.fromkeys(primary + alts))   # dédupe en préservant l'ordre
        if len(choices) >= 2:
            return _fallback_b(state, choices[:3], candidates, start)
        # ambiguous=true mais alternatives insuffisantes → on continue

    # 5) Hors-périmètre explicite (sans ambiguïté résolvable)
    if case == "hors_perimetre":
        return _fallback_a(state, "LLM → hors_perimetre", candidates, start)

    # 6) Hallucination (cas inconnu, ni valide ni hors_perimetre)
    if case not in VALID_CASES:
        if top1_score >= FAST_PATH_MIN_SCORE:
            return _accept(state, candidates[0]["case_name"], top1_score, candidates, start)
        return _fallback_a(state, f"LLM → {case!r}", candidates, start)

    # 7) Cas valide → acceptation
    return _accept(state, case, conf, candidates, start)