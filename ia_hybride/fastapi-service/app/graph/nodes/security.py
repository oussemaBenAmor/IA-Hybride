# === Destination : app/graph/nodes/security.py (remplace l'existant) ===
"""
Nœud sécurité : détection de patterns dangereux (injection SQL, prompt injection,
XSS, commandes système).

Améliorations :
  - `detect_attack(text)` extrait et réutilisable → appelé AUSSI depuis main.py
    pour screener TOUS les tours (y compris suivi / clarification, qui sautaient
    le nœud sécurité).
  - Regex SQL resserrées : on ne bloque plus un simple « ; », « # » ou « -- »
    isolé (faux positifs sur des phrases normales) ; on exige un contexte SQL.
"""
import re
import time
import logging
from urllib.parse import unquote

from app.graph.state import GraphState

logger = logging.getLogger("security")

DANGEROUS_PATTERNS = {
    "sql_injection": [
        r"(?i)\b(DROP|TRUNCATE)\s+TABLE\b",
        r"(?i)\b(DELETE|INSERT|UPDATE|SELECT)\b\s+.*\b(FROM|INTO)\b",
        r"(?i)\bUNION\b\s+\bSELECT\b",
        r"(?i)information_schema",
        r"(?i)'\s*OR\s*'?1'?\s*=\s*'?1",          # tautologie ' OR '1'='1
        r"(?i)WAITFOR\s+DELAY",
        r"(?i);\s*(DROP|DELETE|UPDATE|INSERT|SELECT)\b",   # ; suivi d'une commande SQL
    ],
    "prompt_injection": [
        r"(?i)(ignore (previous|all)|disregard (the|all|previous)|forget your|tu es maintenant|oublie (tes|les)|do anything now)",
        r"(?i)(system prompt|you are now|act as if|réponds uniquement par)",
    ],
    "xss": [
        r"(?i)<\s*script\b",
        r"(?i)onerror\s*=",
        r"(?i)javascript\s*:",
        r"(?i)<\s*img[^>]*onerror\s*=",
    ],
    "system_cmd": [
        r"(?i)(rm\s+-rf|format\s+c:|shutdown|cmd\.exe|powershell|del\s+/f|mkfs|net\s+user)",
    ],
}


def detect_attack(text: str) -> tuple[str, str] | None:
    """Renvoie (type_attaque, pattern) si une attaque est détectée, sinon None."""
    if not text:
        return None
    low = text.lower()
    decoded = unquote(low)
    flags = re.IGNORECASE | re.DOTALL
    for attack_type, patterns in DANGEROUS_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, low, flags) or re.search(pattern, decoded, flags):
                return attack_type, pattern
    return None


def security_node(state: GraphState) -> GraphState:
    start = time.time()
    hit = detect_attack(state.get("input_raw", ""))
    elapsed = (time.time() - start) * 1000

    if hit:
        attack_type, pattern = hit
        logger.warning("requête bloquée (%s)", attack_type)
        return {
            **state,
            "is_blocked":       True,
            "attack_type":      attack_type,
            "fallback_type":    "D",
            "fallback_reason":  f"Pattern {attack_type} détecté",
            "security_pattern": pattern,
            "response_text":    "Votre demande a été bloquée pour des raisons de sécurité.",
            "latency_ms":       {**state.get("latency_ms", {}), "security": elapsed},
        }

    return {**state, "latency_ms": {**state.get("latency_ms", {}), "security": elapsed}}