"""
Gestion des conversations et des messages persistés.

- Rattache une conversation à un utilisateur.
- Enregistre chaque message (user / assistant).
- Liste les conversations d'un utilisateur (barre latérale).
- Recharge tous les messages d'une conversation (avec contrôle de propriété).

À placer dans : app/db/conversations.py
"""
import logging

import psycopg2.extras

from app.db.postgres import get_connection

logger = logging.getLogger("conversations")


def ensure_conversation(session_id: str, user_id: str, first_message: str) -> None:
    """
    Crée la conversation si elle n'existe pas encore, en la rattachant à l'utilisateur
    et en lui donnant un titre dérivé du premier message. Ne fait rien si elle existe
    déjà (le titre et le propriétaire sont posés une seule fois).
    """
    title = (first_message or "Nouvelle conversation").strip()
    if len(title) > 60:
        title = title[:57] + "…"

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Crée la ligne si absente, avec user_id + titre.
            cur.execute(
                """
                INSERT INTO conversation_sessions (session_id, user_id, title)
                VALUES (%s::uuid, %s::uuid, %s)
                    ON CONFLICT (session_id) DO UPDATE
                                                    SET user_id = COALESCE(conversation_sessions.user_id, EXCLUDED.user_id),
                                                    title   = COALESCE(conversation_sessions.title,   EXCLUDED.title),
                                                    updated_at = NOW()
                """,
                (session_id, user_id, title),
            )
        conn.commit()
    finally:
        conn.close()


def save_message(session_id: str, role: str, content: str,
                 fallback_type: str | None = None) -> None:
    """Enregistre un message et met à jour la date de la conversation."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, fallback_type)
                VALUES (%s::uuid, %s, %s, %s)
                """,
                (session_id, role, content, fallback_type),
            )
            cur.execute(
                "UPDATE conversation_sessions SET updated_at = NOW() WHERE session_id = %s::uuid",
                (session_id,),
            )
        conn.commit()
    finally:
        conn.close()


def list_conversations(user_id: str) -> list[dict]:
    """Liste les conversations d'un utilisateur, de la plus récente à la plus ancienne."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT cs.session_id::text AS session_id,
                    COALESCE(cs.title, 'Conversation') AS title,
                       cs.updated_at,
                       COUNT(cm.id) AS message_count
                FROM conversation_sessions cs
                         LEFT JOIN chat_messages cm ON cm.session_id = cs.session_id
                WHERE cs.user_id = %s::uuid
                GROUP BY cs.session_id, cs.title, cs.updated_at
                HAVING COUNT(cm.id) > 0
                ORDER BY cs.updated_at DESC
                """,
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_conversation_messages(session_id: str, user_id: str) -> list[dict] | None:
    """
    Renvoie tous les messages d'une conversation, DANS L'ORDRE, si elle appartient
    à l'utilisateur. Renvoie None si la conversation n'existe pas ou ne lui appartient
    pas (contrôle de propriété — essentiel en bancaire).
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Vérifie la propriété AVANT de renvoyer quoi que ce soit.
            cur.execute(
                "SELECT user_id::text FROM conversation_sessions WHERE session_id = %s::uuid",
                (session_id,),
            )
            owner = cur.fetchone()
            if not owner or owner["user_id"] != str(user_id):
                return None

            cur.execute(
                """
                SELECT role, content, fallback_type, created_at
                FROM chat_messages
                WHERE session_id = %s::uuid
                ORDER BY created_at ASC
                """,
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()