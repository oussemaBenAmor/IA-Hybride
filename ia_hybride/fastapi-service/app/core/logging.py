"""
Configuration de logs structurés pour tout le backend.
À appeler UNE fois au démarrage (lifespan de main.py), avant tout le reste.
Remplace les print() éparpillés dans les nœuds par des loggers nommés.
"""
import logging
import sys

from app.config import settings


def setup_logging() -> None:

    # Récupère le niveau de log défini dans la configuration
    # et le convertit en constante logging (logging.INFO, logging.WARNING...).
    # Si le niveau est invalide, utilise INFO par défaut.
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Envoie les logs vers la console
    handler = logging.StreamHandler(sys.stdout)

    # Définit le format d'affichage des logs
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(name)-12s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )


    # Récupère le logger principal
    root = logging.getLogger()

    # Supprime les anciennes configurations
    root.handlers.clear()

    # Ajoute notre format de logs
    root.addHandler(handler)

    # Définit le niveau minimum affiché
    root.setLevel(level)

    # Réduit les logs trop verbeux des librairies externes
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


    # Indique que la configuration est terminée
    logging.getLogger("startup").info("Logging configuré (niveau=%s)", settings.log_level.upper())