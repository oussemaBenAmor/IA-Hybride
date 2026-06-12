"""
Reranker basé sur un cross-encoder français.
Chargé une seule fois au démarrage (singleton).
"""
from sentence_transformers import CrossEncoder
RERANKER_MODEL = "./models/reranker"


# Modèle français léger (~66MB) — bon compromis vitesse/qualité
# Alternative plus précise mais plus lente : antoinelouis/crossencoder-camembert-base-mmarcoFR
#RERANKER_MODEL = "antoinelouis/crossencoder-mMiniLMv2-L12-mmarcoFR"

_reranker: CrossEncoder | None = None


def get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        print(f"⏳ Chargement du cross-encoder : {RERANKER_MODEL}")
        _reranker = CrossEncoder(RERANKER_MODEL)
        print("✅ Cross-encoder prêt")
    return _reranker


def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """
    Reranke les candidats issus du vector search.

    Args:
        query      : texte de la requête utilisateur
        candidates : liste de dicts avec au moins 'case_name' et 'description'

    Returns:
        Liste triée par score décroissant, chaque item enrichi de 'rerank_score'
    """
    if not candidates:
        return []

    reranker = get_reranker()

    pairs = [(query, c["description"]) for c in candidates]
    scores = reranker.predict(pairs)   # ndarray de floats

    ranked = []
    for candidate, score in zip(candidates, scores):
        ranked.append({**candidate, "score": float(score), "vector_score": candidate.get("score", 0)})

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked