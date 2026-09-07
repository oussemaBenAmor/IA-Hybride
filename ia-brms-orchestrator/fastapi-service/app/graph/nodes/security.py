"""
Nœud de sécurité :
détecte les tentatives d'attaque avant le traitement de la demande.

Types détectés :
- injection SQL
- prompt injection
- XSS
- commandes système dangereuses
"""
import re
import time
import logging
from urllib.parse import unquote

from app.graph.state import GraphState

logger = logging.getLogger("security")

# Motifs dangereux regroupés par type d'attaque
DANGEROUS_PATTERNS = {


    # Détection des injections SQL
    "sql_injection": [

        # Commandes SQL destructrices sur des tables

        r"(?i)\b(DROP|TRUNCATE|DELETE|ALTER|CREATE)\s+TABLE\b",


        # Commandes SQL avec leurs clauses habituelles

        r"(?i)\b(DELETE|INSERT|UPDATE|SELECT)\b\s+.*\b(FROM|INTO)\b",


        # Lecture de données SQL

        r"(?i)\bSELECT\b\s+.*\b(FROM)\b",


        # Tentative d'union SQL

        r"(?i)\bUNION\b\s+\bSELECT\b",


        # Accès aux métadonnées SQL
        r"(?i)information_schema",


        # Injection par condition toujours vraie
        # Exemple : ' OR 1=1
        r"(?i)'\s*OR\s*'?1'?\s*=\s*'?1",


        # Pause forcée dans SQL Server
        r"(?i)WAITFOR\s+DELAY",


        # Commande SQL après un point-virgule

        r"(?i);\s*(DROP|DELETE|UPDATE|INSERT|SELECT|TRUNCATE|ALTER)\b",


        # Commande destructrice même mal formée
        # Exemple : drop users
        r"(?i)\b(DROP|TRUNCATE)\s+\w+",
    ],



    # Tentatives de manipulation du modèle IA
    "prompt_injection": [

        # Demandes pour ignorer les instructions précédentes
        r"(?i)(ignore (previous|all)|disregard (the|all|previous)|forget your|tu es maintenant|oublie (tes|les)|do anything now)",


        # Tentatives de modification du rôle du modèle
        r"(?i)(system prompt|you are now|act as if|réponds uniquement par)",
    ],



    # Attaques par injection HTML/JavaScript
    "xss": [

        # Balise script
        r"(?i)<\s*script\b",

        # Événement JavaScript dangereux
        r"(?i)onerror\s*=",

        # Exécution JavaScript dans une URL
        r"(?i)javascript\s*:",


        # Image contenant du JavaScript
        r"(?i)<\s*img[^>]*onerror\s*=",
    ],



    # Commandes système dangereuses
    "system_cmd": [

        # Commandes de suppression, formatage ou administration
        r"(?i)(rm\s+-rf|format\s+c:|shutdown|cmd\.exe|powershell|del\s+/f|mkfs|net\s+user)",
    ],
}


"""
   Analyse un texte et retourne le type d'attaque détectée.
   Retourne None si aucun danger n'est trouvé.
   """

def detect_attack(text: str) -> tuple[str, str] | None:

    # Aucun texte à analyser
    if not text:
        return None
    # Conversion en minuscules pour ignorer la casse
    low = text.lower()

    # Décodage des caractères encodés dans l'URL
    # Exemple : %3Cscript%3E → <script>
    decoded = unquote(low)

    # Options de recherche :
    # IGNORECASE = ignore majuscules/minuscules
    # DOTALL = . accepte les retours à la ligne
    flags = re.IGNORECASE | re.DOTALL

    # Parcours des catégories d'attaques
    for attack_type, patterns in DANGEROUS_PATTERNS.items():

        # Test de chaque expression régulière
        for pattern in patterns:

            # Recherche dans le texte normal ou décodé
            if re.search(pattern, low, flags) or re.search(pattern, decoded, flags):

                # Retourne le type et le motif détecté
                return attack_type, pattern

    # Aucun motif dangereux trouvé
    return None


def security_node(state: GraphState) -> GraphState:
    start = time.time()

    # Analyse du message utilisateur
    hit = detect_attack(state.get("input_raw", ""))
    elapsed = (time.time() - start) * 1000

    # Une attaque a été détectée
    if hit:
        attack_type, pattern = hit
        logger.warning("requête bloquée (%s)", attack_type)

        # Bloque la requête et prépare une réponse de sécurité
        return {
            **state,
            "is_blocked":       True,
            "attack_type":      attack_type,  #par exemple "sql_injection"
            "fallback_type":    "D",
            "fallback_reason":  f"Pattern {attack_type} détecté",
            "security_pattern": pattern,     # Expression régulière qui a déclenché l'alerte
            "response_text":    "Votre demande a été bloquée pour des raisons de sécurité.",
            "latency_ms":       {**state.get("latency_ms", {}), "security": elapsed},
        }

    return {**state, "latency_ms": {**state.get("latency_ms", {}), "security": elapsed}}