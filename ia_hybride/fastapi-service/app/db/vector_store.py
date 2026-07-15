"""
Gestion du vector store pgvector pour la recherche sémantique des cas métier.
Permet de créer les embeddings, rechercher les cas similaires et alimenter la base.
"""
import logging

import httpx
import psycopg2.extras   # Fournit des curseurs PostgreSQL avancés

from app.db.postgres import get_connection   # Ouvre une connexion PostgreSQL
from app.config import settings

logger = logging.getLogger("vector_store")



# Génère le vecteur (embedding) représentant le texte.
def get_embedding(text: str) -> list[float]:

    try:

        # Envoie le texte au modèle d'embedding
        response = httpx.post(
            f"{settings.ollama_base_url}/api/embed",
            json={"model": settings.embedding_model, "input": text},
            timeout=30.0,
        )

        # Déclenche une erreur si la requête HTTP échoue
        response.raise_for_status()

        # Convertit la réponse JSON en dictionnaire Python
        data = response.json()
        if "embeddings" in data:

            # Retourne le premier embedding généré
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



# Convertit une liste Python au format attendu par pgvector
def format_vector(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"



# Ajoute ou met à jour un cas métier dans la base
def upsert_case(case_name: str, description: str):

    # Génère l'embedding de la description
    embedding = get_embedding(description)
    conn = get_connection()
    try:

        # Ouvre un curseur pour exécuter des requêtes SQL
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



# Recherche les cas métier les plus proches de la requête
def search_similar_cases(query: str, top_k: int = 5) -> list[dict]:

    # Génère l'embedding de la requête utilisateur
    embedding = get_embedding(query)

    # Connexion à PostgreSQL
    conn = get_connection()
    try:

        # Recherche vectorielle des cas les plus similaires sous forme de dictionnaires.
        # et  Convertit la distance cosinus en score de similarité (0 → 1)
        # et Retourne uniquement les top_k meilleurs résultats
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


# Descriptions naturelles utilisées pour créer les embeddings des cas métier
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


# Met à jour tous les cas métier dans le vector store
def seed_cases():

    # Parcourt chaque cas métier défini
    for case_name, description in CASE_DESCRIPTIONS.items():

        # Insère ou met à jour le cas dans PostgreSQL
        upsert_case(case_name, description)
        logger.info("cas métier mis à jour : %s", case_name)



# Permet d'exécuter ce fichier directement
if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    logging.basicConfig(level=logging.INFO)
    print("🌱 Re-seeding du vector store (descriptions naturelles)...")

    # Lance le remplissage du vector store
    seed_cases()
    print("✅ Terminé !")