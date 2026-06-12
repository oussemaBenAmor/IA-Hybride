# === Destination : app/db/vector_store.py (remplace l'existant) ===
"""
Vector store pgvector pour le router sémantique.

Changement clé : les descriptions de cas sont désormais en LANGAGE NATUREL
(phrases représentatives) au lieu de sacs de mots-clés. Le bi-encoder sépare
bien mieux, et le LLM du router les comprend directement.

⚠️ Après modification des descriptions, il faut re-seeder (fait automatiquement
au démarrage via le lifespan de main.py, grâce au ON CONFLICT ... DO UPDATE).
"""
import logging

import httpx
import psycopg2.extras

from app.db.postgres import get_connection
from app.config import settings

logger = logging.getLogger("vector_store")


def get_embedding(text: str) -> list[float]:
    """Génère un embedding via Ollama (passe par le tunnel SSH → Ollama distant)."""
    try:
        response = httpx.post(
            f"{settings.ollama_base_url}/api/embed",
            json={"model": settings.embedding_model, "input": text},
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        if "embeddings" in data:
            return data["embeddings"][0]
    except Exception:
        pass

    # Fallback ancienne route Ollama
    response = httpx.post(
        f"{settings.ollama_base_url}/api/embeddings",
        json={"model": settings.embedding_model, "prompt": text},
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["embedding"]


def format_vector(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def upsert_case(case_name: str, description: str):
    embedding = get_embedding(description)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO semantic_cases (case_name, description, embedding)
                VALUES (%s, %s, %s::vector)
                    ON CONFLICT (case_name) DO UPDATE
                                                   SET description = EXCLUDED.description,
                                                   embedding   = EXCLUDED.embedding
                """,
                (case_name, description, format_vector(embedding)),
            )
        conn.commit()
    finally:
        conn.close()


def search_similar_cases(query: str, top_k: int = 5) -> list[dict]:
    embedding = get_embedding(query)
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT case_name, description,
                       1 - (embedding <=> %s::vector) AS score
                FROM semantic_cases
                ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """,
                (format_vector(embedding), format_vector(embedding), top_k),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ── Descriptions en langage naturel ───────────────────────────────────────────
CASE_DESCRIPTIONS = {
    "credit_immobilier": (
        "Financer l'achat d'un bien immobilier : maison, appartement, résidence "
        "principale ou secondaire, investissement locatif, terrain ou construction. "
        "Le client veut emprunter pour acheter un logement, connaître sa capacité "
        "d'emprunt, sa mensualité ou son taux, généralement sur une longue durée "
        "(15 à 25 ans) et pour un montant élevé. "
        "Exemples : « je veux acheter une maison », « prêt pour financer mon "
        "appartement », « emprunter 200000 euros sur 20 ans pour un logement »."
    ),
    "credit_consommation": (
        "Prêt personnel ou crédit à la consommation pour financer une voiture, des "
        "travaux, des loisirs, de l'électroménager ou un besoin d'argent ponctuel. "
        "Montants plus petits et durées plus courtes que l'immobilier, sans garantie "
        "hypothécaire. "
        "Exemples : « je veux un crédit de 5000 euros pour ma voiture », « prêt "
        "personnel sur 24 mois », « financer des travaux dans mon appartement »."
    ),
    "assurance_vie": (
        "Épargne et placement à long terme via un contrat d'assurance vie : "
        "souscription, versements, rachat partiel ou total, arbitrage, préparation "
        "de la retraite ou transmission de patrimoine. "
        "Exemples : « ouvrir une assurance vie », « placer mon argent pour la "
        "retraite », « racheter une partie de mon contrat d'assurance vie »."
    ),
    "carte_bancaire": (
        "Gestion d'une carte de paiement : faire opposition après une perte ou un "
        "vol, augmenter ou modifier le plafond, renouveler une carte expirée, "
        "débloquer une carte. "
        "Exemples : « ma carte a été volée, je veux faire opposition », « augmenter "
        "le plafond de ma carte », « renouveler ma carte bancaire »."
    ),
    "virement": (
        "Envoyer ou transférer de l'argent vers un bénéficiaire : virement SEPA, "
        "instantané, international ou programmé, ajout d'un bénéficiaire avec son IBAN. "
        "Exemples : « faire un virement de 1000 euros à Jean Dupont », « envoyer de "
        "l'argent à l'étranger », « virement instantané vers un compte »."
    ),
}


def seed_cases():
    for case_name, description in CASE_DESCRIPTIONS.items():
        upsert_case(case_name, description)
        logger.info("cas métier mis à jour : %s", case_name)


if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    logging.basicConfig(level=logging.INFO)
    print("🌱 Re-seeding du vector store (descriptions naturelles)...")
    seed_cases()
    print("✅ Terminé !")