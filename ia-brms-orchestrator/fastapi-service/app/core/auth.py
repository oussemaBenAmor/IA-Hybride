"""
Dépendances FastAPI pour sécuriser les endpoints.

- get_current_user : décode le Bearer token et renvoie l'identité.
- require_admin     : refuse l'accès si l'utilisateur n'est pas admin.

À placer dans : app/core/auth.py
"""
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.db.auth import decode_token

logger = logging.getLogger("auth")

# Schéma "Authorization: Bearer <token>" — FastAPI affichera le cadenas dans /docs.
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
        creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """Renvoie {id, username, role} ou lève 401 si le token est absent/invalide."""
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton d'authentification manquant",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(creds.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Jeton invalide ou expiré",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {
        "id":       payload.get("sub"),
        "username": payload.get("username"),
        "role":     payload.get("role", "user"),
    }


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Autorise uniquement les administrateurs (dashboard, exécution de tests)."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Accès réservé aux administrateurs",
        )
    return user