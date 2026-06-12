# === Destination : app/core/logging.py (nouveau fichier) ===
"""
Configuration de logs structurés pour tout le backend.
À appeler UNE fois au démarrage (lifespan de main.py), avant tout le reste.
Remplace les print() éparpillés dans les nœuds par des loggers nommés.
"""
import logging
import sys

from app.config import settings


def setup_logging() -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # On calme les librairies trop bavardes
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    logging.getLogger("startup").info("Logging configuré (niveau=%s)", settings.log_level.upper())