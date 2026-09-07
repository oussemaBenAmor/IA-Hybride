"""
Couche d'authentification (base de données + JWT).

- Hachage des mots de passe avec bcrypt (passlib).
- Création / vérification des utilisateurs.
- Génération et décodage des jetons JWT.

À placer dans : app/db/auth.py
"""
import uuid
import logging
from datetime import datetime, timedelta, timezone

import psycopg2.extras
from passlib.context import CryptContext
from jose import jwt, JWTError

from app.config import settings
from app.db.postgres import get_connection

logger = logging.getLogger("auth")

# Contexte de hachage : bcrypt est l'algorithme recommandé pour les mots de passe.
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Hachage ───────────────────────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


# ── JWT ────────────────────────────────────────────────────────────────────────
def create_access_token(user_id: str, username: str, role: str) -> str:
    """Crée un jeton signé contenant l'identité et le rôle de l'utilisateur."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {
        "sub":      str(user_id),   # identifiant standard JWT
        "username": username,
        "role":     role,
        "exp":      expire,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict | None:
    """Décode et vérifie un jeton. Retourne le payload ou None si invalide/expiré."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as e:
        logger.info("JWT invalide : %s", e)
        return None


# ── CRUD utilisateurs ───────────────────────────────────────────────────────────
def create_user(username: str, password: str, role: str = "user") -> dict:
    """Crée un utilisateur. Lève ValueError si le nom est déjà pris."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
            if cur.fetchone():
                raise ValueError("Nom d'utilisateur déjà utilisé")

            user_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO users (id, username, password_hash, role)
                VALUES (%s::uuid, %s, %s, %s)
                    RETURNING id, username, role, created_at
                """,
                (user_id, username, hash_password(password), role),
            )
            row = cur.fetchone()
        conn.commit()
        logger.info("utilisateur créé : %s (role=%s)", username, role)
        return dict(row)
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, password_hash, role FROM users WHERE username = %s",
                (username,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def authenticate(username: str, password: str) -> dict | None:
    """Vérifie les identifiants. Retourne l'utilisateur (sans le hash) ou None."""
    user = get_user_by_username(username)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


def ensure_default_admin() -> None:
    """
    Crée un compte admin par défaut au premier démarrage s'il n'existe aucun admin.
    Identifiants par défaut : admin / admin123  (à changer !).
    Appelé une fois dans le lifespan de main.py.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1")
            has_admin = cur.fetchone() is not None
    finally:
        conn.close()

    if not has_admin:
        try:
            create_user("admin", "admin123", role="admin")
            logger.warning("compte admin par défaut créé : admin / admin123 "
                           "— PENSEZ À CHANGER LE MOT DE PASSE")
        except ValueError:
            pass