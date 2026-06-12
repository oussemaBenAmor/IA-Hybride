# === Destination : app/db/postgres.py (remplace l'existant) ===
"""
Couche PostgreSQL avec POOL de connexions.

Avant : une nouvelle connexion psycopg2 était ouverte à CHAQUE appel (audit,
vector store, endpoints) puis fermée — coûteux et lent, surtout quand le reste
ralentit. Maintenant un pool réutilise les connexions.

Rétro-compatible : `get_connection()` renvoie un proxy dont `.close()` REND la
connexion au pool au lieu de la fermer. Tous les appels existants
(`with conn.cursor()`, `conn.commit()`, `conn.rollback()`, `conn.close()`)
fonctionnent sans changement.
"""
import logging

import psycopg2
import psycopg2.extras
import psycopg2.pool

from app.config import settings

logger = logging.getLogger("postgres")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=settings.db_pool_min,
            maxconn=settings.db_pool_max,
            host=settings.postgres_host,
            port=settings.postgres_port,
            user=settings.postgres_user,
            password=settings.postgres_password,
            dbname=settings.postgres_db,
        )
        logger.info("pool PostgreSQL créé (min=%d, max=%d)", settings.db_pool_min, settings.db_pool_max)
    return _pool


class _PooledConnection:
    """Proxy autour d'une connexion poolée : .close() = retour au pool."""

    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool

    def __getattr__(self, name):
        # délègue cursor/commit/rollback/etc. à la vraie connexion
        return getattr(self._conn, name)

    def close(self):
        try:
            self._conn.rollback()   # nettoie toute transaction pendante avant restitution
        except Exception:
            pass
        self._pool.putconn(self._conn)


def get_connection() -> _PooledConnection:
    pool = _get_pool()
    return _PooledConnection(pool.getconn(), pool)


def init_db():
    """Crée toutes les tables si elles n'existent pas."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            cur.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')

            cur.execute("""
                        CREATE TABLE IF NOT EXISTS conversation_sessions (
                                                                             session_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                            created_at  TIMESTAMP DEFAULT NOW(),
                            updated_at  TIMESTAMP DEFAULT NOW(),
                            status      VARCHAR(50) DEFAULT 'active',
                            metadata    JSONB
                            );
                        """)

            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS semantic_cases (
                    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                    case_name   VARCHAR(255) NOT NULL UNIQUE,
                    description TEXT NOT NULL,
                    embedding   vector({settings.embedding_dimensions}),
                    created_at  TIMESTAMP DEFAULT NOW()
                );
            """)

            cur.execute("""
                        CREATE INDEX IF NOT EXISTS idx_semantic_cases_embedding
                            ON semantic_cases USING ivfflat (embedding vector_cosine_ops)
                            WITH (lists = 1);
                        """)

            cur.execute("""
                        CREATE TABLE IF NOT EXISTS audit_trail (
                                                                   id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                            session_id          UUID REFERENCES conversation_sessions(session_id),
                            created_at          TIMESTAMP DEFAULT NOW(),
                            raw_input           TEXT NOT NULL,
                            corrected_input     TEXT,
                            correction_applied  BOOLEAN DEFAULT FALSE,
                            business_case       VARCHAR(255),
                            router_score        FLOAT,
                            router_top2_gap     FLOAT,
                            extracted_params    JSONB,
                            pydantic_valid      BOOLEAN,
                            validation_attempts INTEGER DEFAULT 0,
                            odm_payload         JSONB,
                            odm_response        JSONB,
                            rules_triggered     JSONB,
                            odm_decision        TEXT,
                            llm_prompt          TEXT,
                            llm_response        TEXT,
                            fallback_type       VARCHAR(10),
                            latency_security    FLOAT,
                            latency_correction  FLOAT,
                            latency_router      FLOAT,
                            latency_extraction  FLOAT,
                            latency_validation  FLOAT,
                            latency_odm         FLOAT,
                            latency_generation  FLOAT,
                            latency_total       FLOAT,
                            security_blocked    BOOLEAN DEFAULT FALSE,
                            security_pattern    TEXT
                            );
                        """)

            cur.execute("""
                        CREATE TABLE IF NOT EXISTS security_logs (
                                                                     id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                            created_at      TIMESTAMP DEFAULT NOW(),
                            session_id      UUID,
                            raw_input       TEXT NOT NULL,
                            pattern_matched TEXT NOT NULL,
                            ip_address      VARCHAR(50)
                            );
                        """)

        conn.commit()
        logger.info("PostgreSQL connecté et tables créées")
    finally:
        conn.close()


def purge_old_checkpoints():
    """
    OPTIONNEL — ne garde que le dernier checkpoint par thread LangGraph.
    Évite l'accumulation qui ralentit les longues conversations.
    À tester avant usage : on conserve la reprise (seul le dernier checkpoint
    est nécessaire), on perd l'historique de « time-travel » (non utilisé ici).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                        DELETE FROM checkpoint_writes w
                        WHERE (w.thread_id, w.checkpoint_id) NOT IN (
                            SELECT thread_id, MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id
                        );
                        """)
            cur.execute("""
                        DELETE FROM checkpoints c
                        WHERE c.checkpoint_id <> (
                            SELECT MAX(checkpoint_id) FROM checkpoints c2 WHERE c2.thread_id = c.thread_id
                        );
                        """)
        conn.commit()
        logger.info("checkpoints anciens purgés")
    except Exception:
        conn.rollback()
        logger.exception("purge checkpoints échouée")
    finally:
        conn.close()