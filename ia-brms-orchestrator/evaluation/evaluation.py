"""
Script d'évaluation du conseiller bancaire intelligent.

Ce script permet d'évaluer et de comparer les performances de deux approches :
- le système hybride (LLM + moteur de règles ODM) via l'API /chat ;
- le LLM seul (plusieurs configurations : T=0, T=0.2 et mode raisonnement).

Il offre deux modes d'exécution :
- metrics : calcule les métriques du système (latences, fallbacks, décisions ODM) et génère un rapport Excel ;
- compare : compare les performances et la précision des différentes approches sur un ensemble de cas de test et génère un rapport Excel.

Le mode all exécute successivement les deux évaluations.
"""

import argparse    #Permet de lire les arguments passés lors de l'exécution du script
import csv
import json
import os
import re
import statistics    #Permet de calculer des statistiques comme la moyenne des latences.
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path



# Les cas de test et les réponses contiennent des caractères accentués ("é", "à", "ç"...). Sans cette
# configuration, PostgreSQL peut rencontrer des erreurs d'encodage lors de la lecture ou de l'écriture
# des données.
os.environ["PGCLIENTENCODING"] = "UTF8"

import httpx

#bibliothèque permet de créer des fichiers Excel
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
_FASTAPI_DIR = Path(__file__).resolve().parent.parent / "fastapi-service"
sys.path.insert(0, str(_FASTAPI_DIR))

from app.config import settings
# Cette dépendance est nécessaire pour le mode "metrics" (lecture de PostgreSQL),
# mais pas pour le mode "compare". Ainsi, le script peut fonctionner même si
# psycopg2 n'est pas installé.
try:
    import psycopg2
    import psycopg2.extras
    _HAS_PSYCOPG2 = True
except Exception:
    _HAS_PSYCOPG2 = False



def get_auth_token(api_url: str) -> str | None:
    """Récupère un token JWT via /auth/login, identifiants lus depuis la config."""
    if not settings.eval_password:
        print("⚠️  eval_password absent du .env — authentification impossible.")
        return None
    try:
        r = httpx.post(f"{api_url}/auth/login",
                       json={"username": settings.eval_username,
                             "password": settings.eval_password},
                       timeout=30)
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:
        print(f"⚠️  Échec de l'authentification : {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# STYLES EXCEL COMMUNS
# ══════════════════════════════════════════════════════════════════════════════

HEADER_FILL = PatternFill("solid", start_color="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial", size=11)
TITLE_FONT  = Font(bold=True, color="1F4E78", name="Arial", size=14)
NORMAL_FONT = Font(name="Arial", size=10)
BOLD_FONT   = Font(bold=True, name="Arial", size=10)
GREEN = PatternFill("solid", start_color="C6EFCE")
RED   = PatternFill("solid", start_color="FFC7CE")
AMBER = PatternFill("solid", start_color="FFEB9C")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT   = Alignment(horizontal="left", vertical="center")


def _style_header(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = CENTER
        cell.border = BORDER


def _autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ══════════════════════════════════════════════════════════════════════════════
# COMMUN : lecture des cas + percentile
# ══════════════════════════════════════════════════════════════════════════════
def load_cases(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # Sécurité : on retire tout espace ou BOM résiduel dans les clés
    cleaned = []
    for r in rows:
        cleaned.append({(k or "").strip().lstrip("\ufeff"): v for k, v in r.items()})
    return cleaned


def _percentile(values: list, p: float):

    # Trie les valeurs en supprimant les valeurs nulles (None)
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None

    # Calcule la position du percentile dans la liste triée
    # Exemple : pour le 50e percentile (médiane), p = 0.5
    k = (len(vals) - 1) * p

    # Partie entière de la position calculée
    f = int(k)

    # Indice de la valeur suivante pour l'interpolation
    c = min(f + 1, len(vals) - 1)

    # Si la position correspond exactement à un indice,
    # retourne directement cette valeur
    if f == c:
        return round(vals[f], 1)

    # Sinon, effectue une interpolation linéaire entre les deux valeurs voisines
    # afin d'obtenir une estimation plus précise du percentile
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 1)



# Transforme une réponse technique du moteur hybride en une décision métier lisible.
def _hybrid_label(last: dict) -> str | None:

    # Récupère le type de fallback retourné par le système
    fb = last.get("fallback_type")

    # Récupère la décision ODM si elle existe
    # Le "or {}" évite une erreur si odm_decision est absent ou None
    odm = (last.get("odm_decision") or {}).get("decision")
    if last.get("params_collection_needed"):
        return "PARAMS_REQUIS"
    if fb == "A":
        return "HORS_PERIMETRE"
    if fb == "D":
        return "BLOQUE"
    if fb == "B":
        return "AMBIGU"

    # la décision prise par le moteur de règles ODM
    if odm:
        return odm.upper()

    # Aucun label trouvé
    return None


def run_hybrid(api_url: str, case: dict, token: str | None = None) -> dict:
    """
    Exécute un scénario de test sur le système hybride via /chat.
    Mesure la latence de chaque tour et retourne les résultats.
    """
    session_id = str(uuid.uuid4())

    # Découpe les messages en plusieurs tours
    turns = [t.strip() for t in case["messages"].split("|") if t.strip()]
    last, error = None, None

    # En-tête d'authentification (l'endpoint /chat est protégé par JWT)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    # Stockage des temps de réponse par tour
    per_turn_ms = []
    with httpx.Client(timeout=180.0) as client:
        try:
            for turn in turns:
                t0 = time.time()
                resp = client.post(f"{api_url}/chat",
                                   json={"message": turn, "session_id": session_id},
                                   headers=headers)

                # Vérification du succès de la requête
                resp.raise_for_status()

                # Sauvegarde de la latence du tour courant
                per_turn_ms.append((time.time() - t0) * 1000)

                # Conservation de la dernière réponse
                last = resp.json()
        except Exception as e:
            error = str(e)

    last = last or {}

    # Calcul des métriques de performance
    mean_turn = round(statistics.mean(per_turn_ms), 1) if per_turn_ms else None
    last_turn = round(per_turn_ms[-1], 1) if per_turn_ms else None
    total_ms  = round(sum(per_turn_ms), 1) if per_turn_ms else None
    return {
        "session_id":         session_id,
        "label":              _hybrid_label(last),  # Décision finale normalisée
        "latency_per_req_ms": mean_turn,             # Latence moyenne d'un tour
        "latency_last_ms":    last_turn,              # Latence du dernier tour
        "latency_total_ms":   total_ms,               # Temps total de conversation
        "n_turns":            len(turns),             # Nombre de messages envoyés
        "raw":                last,                  # Réponse brute du système
        "error":              error,                 # Erreur éventuelle
    }



def _load_db_config() -> dict:
    """
    Charge les paramètres de connexion PostgreSQL
    depuis le fichier .env du service FastAPI.
    """

    # Construction du chemin vers le fichier .env
    env_path = Path(__file__).parent.parent / "fastapi-service" / ".env"

    # Dictionnaire contenant les variables récupérées
    vals = {}
    if env_path.exists():

        # Lecture du fichier ligne par ligne
        for line in env_path.read_text(encoding="utf-8").splitlines():

            # Suppression des espaces inutiles
            line = line.strip()

            # Ignore les lignes vides, commentaires ou invalides
            if not line or line.startswith("#") or "=" not in line:
                continue

            # Séparation entre le nom de variable et sa valeur
            k, v = line.split("=", 1)

            # Stockage dans le dictionnaire
            vals[k.strip()] = v.strip()
    return {
        "host": vals.get("POSTGRES_HOST"),
        "port": int(vals.get("POSTGRES_PORT")),
        "user": vals.get("POSTGRES_USER"),
        "password": vals.get("POSTGRES_PASSWORD"),
        "dbname": vals.get("POSTGRES_DB"),
    }


NODE_LATENCY_COLS = [
    "latency_security", "latency_correction", "latency_router",
    "latency_extraction", "latency_validation", "latency_odm",
    "latency_generation", "latency_total",
]


# Récupère les données d'audit du système hybride depuis PostgreSQL pour les
# sessions demandées afin de calculer les métriques de performance.
# Les résultats sont retournés sous forme de liste de dictionnaires.
def fetch_audit_rows(db_config: dict, session_ids: list[str]) -> list[dict]:

    # Aucun identifiant de session => aucune donnée à récupérer
    if not session_ids:
        return []

    # Vérification de la disponibilité du driver PostgreSQ
    if not _HAS_PSYCOPG2:
        print("   ⚠ psycopg2 non installé — audit_trail ignoré "
              "(pip install psycopg2-binary).")
        return []

    # Connexion à la base PostgreSQL
    conn = psycopg2.connect(**db_config, client_encoding="UTF8")
    try:
        # Création d'un curseur retournant des dictionnaires
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # Recherche des traces d'audit des sessions demandées
            cur.execute(
                "SELECT * FROM audit_trail WHERE session_id = ANY(%s::uuid[]) ORDER BY created_at",
                (session_ids,),
            )

            # Conversion des résultats SQL en liste de dictionnaires
            return [dict(r) for r in cur.fetchall()]
    finally:

        # Fermeture obligatoire de la connexion
        conn.close()


def run_cases_metrics(api_url: str, cases: list[dict]) -> list[dict]:
    """
    Exécute une liste de scénarios de test sur le système hybride
    et prépare les résultats pour le rapport de métriques.
    """

    # Authentification unique : on récupère le token JWT une seule fois
    token = get_auth_token(api_url)

    # Stocke les résultats de chaque scénario
    results = []

    # Exécution de chaque cas de test
    for case in cases:

        # Appel du système hybride via /chat (avec authentification)
        hyb = run_hybrid(api_url, case, token)

        # Récupération de la dernière réponse du système
        last = hyb["raw"]
        results.append({
            "test_id":           case["test_id"],
            "description":       case["description"],
            "category":          case["category"],
            "session_id":        hyb["session_id"],
            "n_turns":           hyb["n_turns"],
            "wall_ms":           hyb["latency_total_ms"],   # Performance
            "error":             hyb["error"],
            "expected_case":     case.get("expected_case", "") or None,  # Résultats attendus
            "expected_fallback": case.get("expected_fallback", "") or None,
            "expected_odm":      case.get("expected_odm_decision", "") or None,
            "got_case":          last.get("case_selected"),     # Résultats réellement obtenus
            "got_fallback":      last.get("fallback_type"),
            "got_odm":           (last.get("odm_decision") or {}).get("decision"),
        })

        # Affichage du statut du test
        status = "OK" if not hyb["error"] else f"ERREUR ({hyb['error'][:40]})"
        print(f"  [{case['test_id']}] {case['description'][:45]:45s} → {status}")
    return results


def compute_metrics(audit_rows: list[dict]) -> dict:

    """
    Calcule les métriques système à partir des traces d'audit :
    latences, fallback et décisions ODM.
    """


    # Nombre total d'exécutions analysées
    n = len(audit_rows)

    latency_stats = {}

    # Calcul des statistiques de latence pour chaque composant
    for col in NODE_LATENCY_COLS:

        # Extraction des valeurs disponibles
        series = [float(r[col]) for r in audit_rows if r.get(col) is not None]
        latency_stats[col] = {
            "n":    len(series),    # Nombre de mesures disponibles
            "mean": round(statistics.mean(series), 1) if series else None,   # Temps moyen
            "p50":  _percentile(series, 0.50), # Percentiles de latence
            "p95":  _percentile(series, 0.95),
            "p99":  _percentile(series, 0.99),
            "max":  round(max(series), 1) if series else None,   # Temps maximum observé
        }

    # Comptage des différents types de fallback
    fb_counts = {k: 0 for k in ("A", "B", "C", "D", "E")}
    for r in audit_rows:
        ft = r.get("fallback_type")
        if ft in fb_counts:
            fb_counts[ft] += 1
    total_fb = sum(fb_counts.values())

    odm_decisions = {}
    odm_unavailable = 0
    odm_calls = 0
    for r in audit_rows:
        dec = r.get("odm_decision")

        # Comptage des décisions retournées par ODM
        if dec:
            odm_decisions[dec] = odm_decisions.get(dec, 0) + 1
            odm_calls += 1

        # Comptage des indisponibilités ODM
        if r.get("fallback_type") == "E":
            odm_unavailable += 1

    return {
        "n_rows":          n,
        "latency_stats":   latency_stats,
        "fb_counts":       fb_counts,
        "total_fallback":  total_fb,
        "odm_decisions":   odm_decisions,
        "odm_calls":       odm_calls,
        "odm_unavailable": odm_unavailable,
    }





def build_excel_metrics(results, audit_rows, metrics, out_path):


    """
  Génère un rapport Excel complet des métriques système du conseiller bancaire IA.

  La fonction crée plusieurs feuilles contenant :
  - un résumé global des performances (latence, fallbacks, appels ODM) ;
  - les statistiques de latence par nœud du pipeline ;
  - la répartition des différents types de fallback ;
  - les décisions retournées par le moteur ODM ;
  - le détail des résultats obtenus pour chaque cas de test ;
  - les données brutes d'audit utilisées pour les calculs.

  Les informations proviennent des résultats des tests, des traces d'audit
  PostgreSQL et des métriques calculées précédemment.
    """
    wb = Workbook()

    # ── Feuille 1 : Résumé ───────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Résumé"
    ws["A1"] = "Métriques système — Conseiller Bancaire IA"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Généré le {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    ws["A2"].font = Font(italic=True, name="Arial", size=9)

    lt = metrics["latency_stats"]["latency_total"]
    n = metrics["n_rows"]
    summary_rows = [
        ("Indicateur", "Valeur"),
        ("Nombre de requêtes auditées", n),
        ("Latence totale — moyenne (ms)", lt["mean"]),
        ("Latence totale — p50 (ms)", lt["p50"]),
        ("Latence totale — p95 (ms)", lt["p95"]),
        ("Latence totale — p99 (ms)", lt["p99"]),
        ("Latence totale — max (ms)", lt["max"]),
        ("Total fallbacks", metrics["total_fallback"]),
        ("Taux de fallback global", None),
        ("Appels ODM réussis", metrics["odm_calls"]),
        ("ODM indisponible (Fallback E)", metrics["odm_unavailable"]),
    ]
    start = 4
    row_of = {}
    for i, (k, v) in enumerate(summary_rows):
        r = start + i
        row_of[k] = r
        ws.cell(row=r, column=1, value=k)
        if v is not None:
            ws.cell(row=r, column=2, value=v)
        if i == 0:
            _style_header(ws, r, 2)
        else:
            ws.cell(row=r, column=1).font = NORMAL_FONT
            ws.cell(row=r, column=2).font = BOLD_FONT
            ws.cell(row=r, column=1).border = BORDER
            ws.cell(row=r, column=2).border = BORDER
    fr = row_of["Taux de fallback global"]
    nr = row_of["Nombre de requêtes auditées"]
    tr = row_of["Total fallbacks"]
    ws.cell(row=fr, column=2, value=f"=IF(B{nr}=0,0,B{tr}/B{nr})")
    ws.cell(row=fr, column=2).number_format = "0.0%"
    _autosize(ws, [38, 22])

    # ── Feuille 2 : Latences par nœud ────────────────────────────────────────
    ws2 = wb.create_sheet("Latences par nœud")
    ws2["A1"] = "Latence par nœud (ms)"
    ws2["A1"].font = TITLE_FONT
    headers = ["Nœud", "N", "Moyenne", "p50", "p95", "p99", "Max"]
    hr = 3
    for c, h in enumerate(headers, start=1):
        ws2.cell(row=hr, column=c, value=h)
    _style_header(ws2, hr, len(headers))
    node_label = {
        "latency_security": "security", "latency_correction": "correction",
        "latency_router": "router", "latency_extraction": "extraction",
        "latency_validation": "validation", "latency_odm": "odm_call",
        "latency_generation": "generation", "latency_total": "TOTAL",
    }
    for i, col in enumerate(NODE_LATENCY_COLS):
        r = hr + 1 + i
        s = metrics["latency_stats"][col]
        vals = [node_label[col], s["n"], s["mean"], s["p50"], s["p95"], s["p99"], s["max"]]
        for c, v in enumerate(vals, start=1):
            cell = ws2.cell(row=r, column=c, value=v)
            cell.border = BORDER
            cell.font = BOLD_FONT if col == "latency_total" else NORMAL_FONT
            cell.alignment = LEFT if c == 1 else CENTER
    _autosize(ws2, [16, 8, 12, 10, 10, 10, 10])

    # ── Feuille 3 : Fallbacks ────────────────────────────────────────────────
    ws3 = wb.create_sheet("Fallbacks")
    ws3["A1"] = "Taux de fallback par type"
    ws3["A1"].font = TITLE_FONT
    fb_labels = {
        "A": "A — Hors périmètre", "B": "B — Ambiguïté",
        "C": "C — Params invalides", "D": "D — Sécurité",
        "E": "E — ODM indisponible",
    }
    headers = ["Type", "Libellé", "Nombre", "Taux"]
    hr = 3
    for c, h in enumerate(headers, start=1):
        ws3.cell(row=hr, column=c, value=h)
    _style_header(ws3, hr, len(headers))
    for i, (k, label) in enumerate(fb_labels.items()):
        r = hr + 1 + i
        ws3.cell(row=r, column=1, value=k)
        ws3.cell(row=r, column=2, value=label)
        ws3.cell(row=r, column=3, value=metrics["fb_counts"][k])
        ws3.cell(row=r, column=4, value=f"=C{r}/{max(n,1)}")
        ws3.cell(row=r, column=4).number_format = "0.0%"
        for c in range(1, 5):
            ws3.cell(row=r, column=c).border = BORDER
            ws3.cell(row=r, column=c).font = NORMAL_FONT
    tot_r = hr + 1 + len(fb_labels)
    ws3.cell(row=tot_r, column=2, value="TOTAL")
    ws3.cell(row=tot_r, column=3, value=f"=SUM(C{hr+1}:C{tot_r-1})")
    ws3.cell(row=tot_r, column=4, value=f"=C{tot_r}/{max(n,1)}")
    ws3.cell(row=tot_r, column=4).number_format = "0.0%"
    for c in range(1, 5):
        ws3.cell(row=tot_r, column=c).font = BOLD_FONT
        ws3.cell(row=tot_r, column=c).border = BORDER
    _autosize(ws3, [8, 24, 10, 10])

    # ── Feuille 4 : ODM ──────────────────────────────────────────────────────
    ws4 = wb.create_sheet("ODM")
    ws4["A1"] = "Décisions du moteur de règles (ODM)"
    ws4["A1"].font = TITLE_FONT
    headers = ["Décision", "Nombre", "Taux (sur appels ODM)"]
    hr = 3
    for c, h in enumerate(headers, start=1):
        ws4.cell(row=hr, column=c, value=h)
    _style_header(ws4, hr, len(headers))
    odm_calls = max(metrics["odm_calls"], 1)
    decs = metrics["odm_decisions"] or {"(aucun)": 0}
    r = hr
    for i, (dec, cnt) in enumerate(sorted(decs.items())):
        r = hr + 1 + i
        ws4.cell(row=r, column=1, value=dec)
        ws4.cell(row=r, column=2, value=cnt)
        ws4.cell(row=r, column=3, value=f"=B{r}/{odm_calls}")
        ws4.cell(row=r, column=3).number_format = "0.0%"
        for c in range(1, 4):
            ws4.cell(row=r, column=c).border = BORDER
            ws4.cell(row=r, column=c).font = NORMAL_FONT
    r += 2
    ws4.cell(row=r, column=1, value="ODM indisponible (Fallback E)")
    ws4.cell(row=r, column=2, value=metrics["odm_unavailable"])
    ws4.cell(row=r, column=1).font = BOLD_FONT
    ws4.cell(row=r, column=2).font = BOLD_FONT
    ws4.cell(row=r + 1, column=1, value="Taux d'indisponibilité ODM")
    ws4.cell(row=r + 1, column=2, value=f"=B{r}/{max(n,1)}")
    ws4.cell(row=r + 1, column=2).number_format = "0.0%"
    ws4.cell(row=r + 1, column=1).font = BOLD_FONT
    _autosize(ws4, [34, 12, 22])

    # ── Feuille 5 : Détail des cas ───────────────────────────────────────────
    ws5 = wb.create_sheet("Cas de test")
    headers = ["Test", "Description", "Catégorie", "Tours", "Wall (ms)",
               "Cas attendu", "Cas obtenu", "Fallback att.", "Fallback obt.",
               "ODM att.", "ODM obt.", "Routing OK", "Erreur"]
    for c, h in enumerate(headers, start=1):
        ws5.cell(row=1, column=c, value=h)
    _style_header(ws5, 1, len(headers))
    for i, res in enumerate(results):
        r = 2 + i
        routing_ok = ""
        if res["expected_case"]:
            routing_ok = "✓" if res["got_case"] == res["expected_case"] else "✗"
        elif res["expected_fallback"]:
            routing_ok = "✓" if res["got_fallback"] == res["expected_fallback"] else "✗"
        vals = [
            res["test_id"], res["description"], res["category"], res["n_turns"],
            res["wall_ms"], res["expected_case"], res["got_case"],
            res["expected_fallback"], res["got_fallback"],
            res["expected_odm"], res["got_odm"], routing_ok, res["error"] or "",
                                                             ]
        for c, v in enumerate(vals, start=1):
            cell = ws5.cell(row=r, column=c, value=v)
            cell.border = BORDER
            cell.font = NORMAL_FONT
            cell.alignment = LEFT if c in (2, 13) else CENTER
            if c == 12 and v == "✗":
                cell.fill = RED
            elif c == 12 and v == "✓":
                cell.fill = GREEN
    ws5.freeze_panes = "A2"
    _autosize(ws5, [7, 40, 12, 7, 10, 16, 16, 12, 12, 11, 11, 10, 30])

    # ── Feuille 6 : Audit brut ───────────────────────────────────────────────
    ws6 = wb.create_sheet("Audit brut")
    if audit_rows:
        cols = list(audit_rows[0].keys())
        for c, h in enumerate(cols, start=1):
            ws6.cell(row=1, column=c, value=h)
        _style_header(ws6, 1, len(cols))
        for i, row in enumerate(audit_rows):
            for c, col in enumerate(cols, start=1):
                v = row[col]
                if not isinstance(v, (int, float, str, type(None))):
                    v = str(v)
                ws6.cell(row=2 + i, column=c, value=v)
        ws6.freeze_panes = "A2"
        _autosize(ws6, [18] * len(cols))

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"\n✅ Excel métriques généré : {out_path}")





def cmd_metrics(args) -> list[dict]:
    """
       Exécute le mode métriques système.

       Cette fonction orchestre l'ensemble du pipeline d'évaluation :
       - chargement des cas de test ;
       - exécution des scénarios via l'API du système hybride ;
       - récupération des traces d'audit depuis PostgreSQL ;
       - calcul des métriques de performance ;
       - génération du rapport Excel.

       Elle retourne les résultats bruts des tests pour permettre
       leur réutilisation dans d'autres traitements.
    """

    print("═══ MODE MÉTRIQUES SYSTÈME ═══")

    # 1) Chargement et exécution des scénarios de test
    print("1) Envoi des cas de test à l'API…")
    cases = load_cases(args.cases)
    results = run_cases_metrics(args.api, cases)

    # 2) Récupération des traces d'exécution depuis PostgreSQL
    print("\n2) Lecture de audit_trail…")
    session_ids = [r["session_id"] for r in results]
    time.sleep(1.0)  # laisser l'audit s'écrire

    # Chargement de la configuration PostgreSQL
    db_config = _load_db_config()

    # Récupération des traces d'audit associées aux sessions
    audit_rows = fetch_audit_rows(db_config, session_ids)
    print(f"   {len(audit_rows)} lignes d'audit récupérées")

    # 3) Calcul des statistiques système
    print("\n3) Calcul des métriques…")
    metrics = compute_metrics(audit_rows)
    lt = metrics["latency_stats"]["latency_total"]
    print(f"   latence totale p95 = {lt['p95']} ms")
    print(f"   fallbacks = {metrics['fb_counts']}")
    print(f"   décisions ODM = {metrics['odm_decisions']}")

    # 4) Génération du rapport Excel
    print("\n4) Génération de l'Excel…")
    build_excel_metrics(results, audit_rows, metrics, args.out_metrics)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
#  MODE 2 : COMPARAISON HYBRIDE vs LLM SEUL
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════


# Ensemble des décisions autorisées par le système.
# Permet de vérifier que les réponses générées par les LLM
# respectent les catégories attendues pour la comparaison.
VALID_DECISIONS = {"ELIGIBLE", "REFUSE", "APPROUVE", "VERIFICATION_REQUISE",
                   "HORS_PERIMETRE", "BLOQUE", "PARAMS_REQUIS"}


def expected_label(case: dict) -> str | None:

    """
    Détermine la décision attendue pour un cas de test à partir du fichier de tests.

    Ordre de priorité :
    1. expected_params_needed = true  -> on attend que le système RÉCLAME les
       paramètres manquants (label PARAMS_REQUIS), produit par _hybrid_label
       lorsque params_collection_needed est vrai.
    2. expected_odm_decision          -> la décision ODM (référence métier).
    3. expected_fallback              -> déduit du fallback attendu (A, D).
    Sinon, cas non scoré (None).
    """
    # 1. Cas où l'on attend explicitement une demande de paramètres manquants.
    pn = (case.get("expected_params_needed") or "").strip().lower()
    if pn in ("true", "1", "oui", "vrai", "yes", "x"):
        return "PARAMS_REQUIS"

    # 2. Décision ODM attendue (prioritaire car c'est la référence métier).
    odm = (case.get("expected_odm_decision") or "").strip().upper()
    if odm:
        return odm

    # 3. À défaut, on déduit le label du fallback attendu.
    fb = (case.get("expected_fallback") or "").strip().upper()
    if fb == "A":
        return "HORS_PERIMETRE"
    if fb == "D":
        return "BLOQUE"

    return None


# Bloc de règles métier injecté dans le prompt du LLM seul.
# Il reproduit EXACTEMENT les seuils et les formules du moteur ODM (rulesets +
# ruleflows + méthode Decision.calculer). L'objectif est une comparaison
# « à information égale » : le LLM dispose des mêmes règles que le système hybride,
# de sorte que la seule différence testée est l'ABSENCE du moteur déterministe
# (calcul exact, déterminisme, traçabilité des règles déclenchées).
RULES_CONTEXT = """RÈGLES MÉTIER OFFICIELLES DE LA BANQUE (à appliquer strictement) :

Valeurs calculées pour le crédit (immobilier et consommation) :
- mensualite = montant / duree_mois
- taux_endettement = (mensualite / revenu_mensuel) * 100
- age_fin_pret = age + (duree_mois / 12)
- montant_max_empruntable = (revenu_mensuel * 0.35) * duree_mois

CRÉDIT IMMOBILIER :
- REFUSE si taux_endettement > 35
- REFUSE si age_fin_pret > 75
- sinon ELIGIBLE

CRÉDIT CONSOMMATION :
- REFUSE si montant > 75000
- REFUSE si duree_mois > 84
- REFUSE si taux_endettement > 35
- sinon ELIGIBLE

ASSURANCE VIE :
- REFUSE si age > 85
- REFUSE si operation = "souscription" et montant_initial < 1000
- sinon ELIGIBLE

CARTE BANCAIRE (opération de plafond) :
- VERIFICATION_REQUISE si le plafond demandé <= 0 (non précisé)
- VERIFICATION_REQUISE si le plafond demandé > 5000
- sinon APPROUVE

VIREMENT :
- VERIFICATION_REQUISE si l'IBAN est absent
- VERIFICATION_REQUISE si virement international et montant > 10000
- VERIFICATION_REQUISE si virement standard et montant > 50000
- sinon APPROUVE
"""


# Prompt utilisé pour tester un LLM seul, SANS moteur de règles mais AVEC les
# règles métier fournies dans le contexte (comparaison à information égale).
# La réponse est contrainte à un format JSON afin de permettre une comparaison
# automatique avec le système hybride.
LLM_ONLY_PROMPT = """Tu es un conseiller bancaire qui doit traiter une demande client.
Tu dois prendre TOI-MÊME la décision, sans outil externe, en appliquant les règles ci-dessous.

{rules_context}
Demande du client : "{message}"

Applique les règles métier ci-dessus (calcule toi-même les valeurs dérivées si nécessaire)
et rends une décision selon ces catégories possibles :
- ELIGIBLE : crédit ou assurance vie accordé
- REFUSE : crédit ou assurance vie refusé
- APPROUVE : opération (virement, carte) acceptée
- VERIFICATION_REQUISE : opération nécessitant une vérification
- HORS_PERIMETRE : la demande ne concerne aucun service bancaire (crédit, assurance vie, carte, virement)
- BLOQUE : la demande est une tentative d'attaque ou de manipulation
- PARAMS_REQUIS : il manque une information OBLIGATOIRE pour décider (ex : montant, durée, revenu, âge). Utilise cette catégorie si tu ne peux PAS décider sans inventer de chiffre.

Réponds à la fin STRICTEMENT par une ligne JSON, sans rien après :
{{"decision": "<une des catégories>", "montant_max_empruntable": <nombre ou null>, "justification": "<une phrase>"}}
"""


# Schéma de validation de la réponse LLM permettant de vérifier
# que la structure JSON retournée respecte les champs attendus.
_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "decision":                {"type": "string"},
        "montant_max_empruntable": {"type": ["number", "null"]},
        "justification":           {"type": "string"},
    },
    "required": ["decision"],
}


# Extrait un JSON valide depuis la réponse brute du LLM,
# même si le modèle ajoute du texte autour de la sortie attendue.
def _safe_json(text: str) -> dict | None:
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else None
    except (json.JSONDecodeError, TypeError):
        matches = re.findall(r"\{[^{}]*\}", text, re.DOTALL)
        for m in reversed(matches):
            try:
                out = json.loads(m)
                if isinstance(out, dict) and "decision" in out:
                    return out
            except json.JSONDecodeError:
                continue
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


def run_llm_only(ollama_url: str, model: str, case: dict, *,
                 think: bool, temperature: float, keep_alive: str = "30m") -> dict:


    """
   Exécute un cas de test avec un LLM seul via Ollama.

   Construit le prompt, appelle le modèle avec ou sans raisonnement,
   extrait la réponse JSON, valide la décision obtenue et retourne
   les informations nécessaires à la comparaison (décision, latence,
   justification et erreurs).
    """

    # Fusionne les différents tours du scénario en un seul message utilisateur
    full_msg = " ".join(t.strip() for t in case["messages"].split("|") if t.strip())

    # Insère les règles métier ET la demande utilisateur dans le prompt d'évaluation du LLM.
    # Les règles fournies sont identiques à celles du moteur ODM (comparaison à information égale).
    body = LLM_ONLY_PROMPT.format(rules_context=RULES_CONTEXT, message=full_msg)


    # Prépare les paramètres de l'appel vers Ollama
    payload = {
        "model": model,
        "stream": False,     # Retourne toute la réponse en une seule fois
        "keep_alive": keep_alive,
        "options": {"temperature": temperature},    # Contrôle la variabilité des réponses
    }
    if think:
        # Active le mode raisonnement pour tester un LLM avec réflexion
        payload["prompt"] = body
        payload["think"] = True
    else:
        # Désactive explicitement le raisonnement pour le LLM classique
        payload["prompt"] = "/no_think\n" + body

        # Force une structure JSON conforme au format attendu
        payload["format"] = _LLM_SCHEMA

    t0 = time.time()
    error, parsed, thinking_len = None, {}, 0
    try:

        # Envoie la requête au serveur Ollama
        resp = httpx.post(f"{ollama_url}/api/generate", json=payload, timeout=300.0)
        resp.raise_for_status()

        # Convertit la réponse JSON d'Ollama en dictionnaire Python
        data = resp.json()

        # Récupère la réponse finale générée par le LLM
        raw = (data.get("response") or "").strip()

        # Mesure la taille du raisonnement généré (uniquement en mode thinking)
        # Permet d'évaluer le coût du raisonnement du modèle
        thinking_len = len(data.get("thinking") or "")

        # Extrait le JSON contenant la décision depuis la réponse du LLM
        parsed = _safe_json(raw) or {}
    except Exception as e:
        error = str(e)
    elapsed = (time.time() - t0) * 1000

    # Récupère la décision retournée par le LLM et la normalise
    decision = (parsed.get("decision") or "").strip().upper()
    # Filet de sécurité : si le LLM n'a pas mis PARAMS_REQUIS mais que sa
    # justification réclame clairement une info manquante, on le crédite quand même.
    justif = (parsed.get("justification") or "").lower()
    _MANQUE = ("manque", "information manquante", "informations manquantes",
               "préciser", "preciser", "il faut connaître", "il faut connaitre",
               "besoin de plus", "données insuffisantes", "donnees insuffisantes",
               "revenu n'est pas", "montant n'est pas", "durée n'est pas",
               "impossible de déterminer", "impossible de determiner")

    # Correction d'un cas fréquent :
    # le LLM explique qu'il manque des informations mais oublie de retourner
    # explicitement la catégorie PARAMS_REQUIS
    if decision not in VALID_DECISIONS and any(k in justif for k in _MANQUE):
        decision = "PARAMS_REQUIS"

    # Vérifie que la décision appartient aux catégories autorisées
    # Sinon, indique une réponse vide ou invalide
    if decision not in VALID_DECISIONS:
        decision = decision or "(vide)"
    return {
        "label": decision,        # Décision finale du LLM
        "montant_max": parsed.get("montant_max_empruntable"),
        "justification": parsed.get("justification", ""),
        "latency_ms": round(elapsed, 1),
        "thinking_len": thinking_len,     # Taille du raisonnement
        "error": error,
    }


def warm_up(ollama_url: str, model: str, n: int = 2):

    """
    Charge le modèle Ollama en mémoire avant l'évaluation.

    Effectue des appels simples afin d'éviter que le temps
    de chargement initial du modèle influence les mesures de latence.
    """

    # Indique le nombre d'appels de préchauffage effectués
    print(f"Warm-up Ollama ({n} appels)…")

    # Réalise des requêtes simples vers Ollama
    for _ in range(n):
        try:

            # Appel léger au modèle uniquement pour le charger en mémoire
            httpx.post(f"{ollama_url}/api/generate",
                       json={"model": model, "prompt": "/no_think\nbonjour",
                             "stream": False, "keep_alive": "30m"},
                       timeout=120.0)
        except Exception:
            # Ignore les erreurs de warm-up pour ne pas bloquer l'évaluation
            pass


def compare(api_url, ollama_url, model, cases) -> list[dict]:

    """
    Compare les performances du système hybride avec plusieurs configurations
    de LLM seul.

    Pour chaque cas de test, exécute :
    - le système hybride ;
    - un LLM seul avec température 0 ;
    - un LLM seul avec température 0.2 ;
    - un LLM avec raisonnement.

    Retourne les décisions obtenues, leur exactitude, les latences
    et les informations complémentaires nécessaires à l'analyse.
    """

    # Liste contenant les résultats de chaque cas testé
    rows = []

    # Authentification unique pour tous les appels au système hybride
    token = get_auth_token(api_url)

    # Parcourt tous les scénarios de test
    for case in cases:

        # Récupère la décision correcte attendue pour comparer les modèles
        exp = expected_label(case)

        # Exécution du système hybride (règles + ODM + LLM)
        hyb  = run_hybrid(api_url, case, token)

        # LLM seul sans raisonnement, température faible: mesurer un comportement déterministe
        fast = run_llm_only(ollama_url, model, case, think=False, temperature=0.0)

        # LLM seul sans raisonnement, température plus élevée : mesurer l'impact de la variabilité
        mid  = run_llm_only(ollama_url, model, case, think=False, temperature=0.2)

        # LLM avec raisonnement activé: mesurer l'apport du raisonnement sur les décisions complexes
        reas = run_llm_only(ollama_url, model, case, think=True,  temperature=0.2)

        # Vérifie si ce cas possède une réponse attendue permettant une évaluation
        scored = exp is not None

        # Compare chaque résultat avec la vérité terrain
        hyb_ok  = (hyb["label"]  == exp) if scored else None
        fast_ok = (fast["label"] == exp) if scored else None
        mid_ok  = (mid["label"]  == exp) if scored else None
        reas_ok = (reas["label"] == exp) if scored else None


        # Stocke tous les résultats du test courant
        rows.append({
            "test_id": case["test_id"],
            "description": case["description"],
            "category": case["category"],
            "expected": exp or "(non scoré)",
            "scored": scored,
            "n_turns": hyb["n_turns"],

            # Résultat système hybride
            "hybrid": hyb["label"] or "(vide)",
            "hybrid_ok": hyb_ok,
            "hybrid_lat": hyb["latency_per_req_ms"],
            "hybrid_lat_total": hyb["latency_total_ms"],

            # Résultat LLM température 0
            "fast": fast["label"],
            "fast_ok": fast_ok,
            "fast_lat": fast["latency_ms"],
            "fast_montant": fast.get("montant_max"),

            # Résultat LLM température 0.2
            "mid": mid["label"],
            "mid_ok": mid_ok,
            "mid_lat": mid["latency_ms"],
            "mid_montant": mid.get("montant_max"),

            # Résultat LLM avec raisonnement
            "reas": reas["label"],
            "reas_ok": reas_ok,
            "reas_lat": reas["latency_ms"],
            "reas_montant": reas.get("montant_max"),
            "reas_think_len": reas.get("thinking_len", 0),    # Taille du raisonnement généré
            "reas_justif": reas.get("justification", ""),
        })

        # Fonction locale pour afficher le résultat sous forme lisible
        def m(ok): return "✓" if ok else ("✗" if ok is False else "–")

        # Indique si un raisonnement a réellement été généré
        tflag = "🧠" if reas.get("thinking_len", 0) > 0 else "·"

        # Affichage résumé du test dans le terminal
        print(f"  [{case['test_id']}] {case['category']:18s} att={exp or '-':16s} "
              f"hyb={hyb['label'] or '-':9s}[{m(hyb_ok)}] "
              f"fast={fast['label']:9s}[{m(fast_ok)}] "
              f"mid={mid['label']:9s}[{m(mid_ok)}] "
              f"reas={reas['label']:9s}[{m(reas_ok)}]{tflag}")

    # Retourne tous les résultats pour génération des rapports
    return rows



def aggregate(rows: list[dict]) -> dict:

    # Agrège les résultats des tests par catégorie et globalement.
    # Compte le nombre de tests évalués ainsi que les réussites
    # pour chaque stratégie (hybride, rapide, intermédiaire, raisonnement).


    # Stockage des statistiques par catégorie.
    # Chaque nouvelle catégorie commence avec des compteurs à zéro.
    by_cat = defaultdict(lambda: {"n": 0, "hyb": 0, "fast": 0, "mid": 0, "reas": 0})

    # Statistiques globales toutes catégories confondues
    glob = {"n": 0, "hyb": 0, "fast": 0, "mid": 0, "reas": 0}

    # Parcours de chaque résultat de test
    for r in rows:
        # Ignore les tests qui n'ont pas été évalués/scorés
        if not r["scored"]:
            continue

        # Récupère le compteur correspondant à la catégorie du test
        c = by_cat[r["category"]]

        # Incrémente le nombre de tests pour la catégorie et globalement
        c["n"] += 1; glob["n"] += 1

        # Compte les réussites de chaque stratégie
        # Une réussite est ajoutée à la catégorie et au total global
        if r["hybrid_ok"]: c["hyb"]  += 1; glob["hyb"]  += 1
        if r["fast_ok"]:   c["fast"] += 1; glob["fast"] += 1
        if r["mid_ok"]:    c["mid"]  += 1; glob["mid"]  += 1
        if r["reas_ok"]:   c["reas"] += 1; glob["reas"] += 1
    return {"by_cat": dict(by_cat), "glob": glob}


def latency_stats(rows, key):

    # Calcule les statistiques de latence
    # Retourne le nombre de mesures, la moyenne, les percentiles
    # P50/P95/P99 ainsi que la latence maximale.


    # Extraction des valeurs de latence disponibles.
    # Les valeurs absentes (None) sont ignorées.
    series = [r[key] for r in rows if r.get(key) is not None]

    # Aucun résultat disponible : retourne des valeurs vides
    if not series:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}

    # Calcul des statistiques de performance
    return {
        "n": len(series),
        "mean": round(statistics.mean(series), 1),
        "p50": _percentile(series, 0.50),
        "p95": _percentile(series, 0.95),
        "p99": _percentile(series, 0.99),
        "max": round(max(series), 1),
    }




"""
   Génère un fichier Excel comparatif entre le système hybride et les différents
   modes LLM seuls avec les métriques de précision, latence et résultats détaillés.
"""
def build_excel_compare(rows, agg, out_path, model):
    wb = Workbook()

    # ── Feuille 1 : Synthèse ─────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Synthèse"
    ws["A1"] = "Comparaison : Hybride (LLM+BRMS) vs LLM seul (2 modes)"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = f"Modèle : {model} — généré le {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    ws["A2"].font = Font(italic=True, name="Arial", size=9)

    g = agg["glob"]; n = max(g["n"], 1)

    # Nombre de cas où le mode raisonnement a réellement produit un bloc de

    # ── Feuille 2 : Par catégorie ────────────────────────────────────────────
    ws2 = wb.create_sheet("Par catégorie")
    ws2["A1"] = "Taux de réussite par catégorie"
    ws2["A1"].font = TITLE_FONT
    headers = ["Catégorie", "N", "Hybride %", "LLM T=0 %", "LLM T=0.2 %", "LLM T=0.2+rais. %"]
    hr = 3
    for c, h in enumerate(headers, start=1):
        ws2.cell(row=hr, column=c, value=h)
    _style_header(ws2, hr, len(headers))
    order = ["piege_endettement", "piege_reorientation", "limite", "params_partiels",
             "params_manquants", "nominal", "multitour", "hors_perimetre",
             "securite", "ambiguite", "greeting"]
    cats = sorted(agg["by_cat"].keys(), key=lambda x: order.index(x) if x in order else 99)
    for i, cat in enumerate(cats):
        st = agg["by_cat"][cat]; nn = max(st["n"], 1)
        r = hr + 1 + i
        vals = [cat, st["n"], st["hyb"]/nn, st["fast"]/nn, st["mid"]/nn, st["reas"]/nn]
        for c, v in enumerate(vals, start=1):
            cell = ws2.cell(row=r, column=c, value=v)
            cell.border = BORDER; cell.font = NORMAL_FONT
            cell.alignment = LEFT if c == 1 else CENTER
            if c in (3, 4, 5, 6):
                cell.number_format = "0.0%"
        if st["fast"]/nn < 0.5: ws2.cell(row=r, column=4).fill = RED
        if st["mid"]/nn  < 0.5: ws2.cell(row=r, column=5).fill = RED
        if st["reas"]/nn < 0.5: ws2.cell(row=r, column=6).fill = RED
        if st["hyb"]/nn >= 0.99: ws2.cell(row=r, column=3).fill = GREEN
    _autosize(ws2, [20, 6, 12, 12, 12, 16])

    # ── Feuille 3 : Latence ──────────────────────────────────────────────────
    ws3 = wb.create_sheet("Latence")
    ws3["A1"] = "Comparaison de latence PAR REQUÊTE (ms)"
    ws3["A1"].font = TITLE_FONT
    ws3["A2"] = ("Hybride = latence MOYENNE PAR TOUR de l'API /chat (un tour = un appel, "
                 "comparable au LLM seul). Le LLM seul fait un seul appel par cas.")
    ws3["A2"].font = Font(italic=True, name="Arial", size=9)
    headers = ["Système", "N", "Moyenne", "p50", "p95", "p99", "Max"]
    hr = 4
    for c, h in enumerate(headers, start=1):
        ws3.cell(row=hr, column=c, value=h)
    _style_header(ws3, hr, len(headers))
    systems = [
        ("Hybride (par requête)", latency_stats(rows, "hybrid_lat")),
        ("LLM T=0 (no_think)", latency_stats(rows, "fast_lat")),
        ("LLM T=0.2 (no_think)", latency_stats(rows, "mid_lat")),
        ("LLM T=0.2 + raisonnement", latency_stats(rows, "reas_lat")),
    ]
    for i, (label, st) in enumerate(systems):
        r = hr + 1 + i
        vals = [label, st["n"], st["mean"], st["p50"], st["p95"], st["p99"], st["max"]]
        for c, v in enumerate(vals, start=1):
            cell = ws3.cell(row=r, column=c, value=v)
            cell.border = BORDER; cell.font = NORMAL_FONT
            cell.alignment = LEFT if c == 1 else CENTER
    st_tot = latency_stats(rows, "hybrid_lat_total")
    r = hr + 1 + len(systems) + 1
    ws3.cell(row=r, column=1, value="(info) Hybride conversation complète").font = Font(italic=True, name="Arial", size=9)
    for c, v in zip(range(2, 8), [st_tot["n"], st_tot["mean"], st_tot["p50"], st_tot["p95"], st_tot["p99"], st_tot["max"]]):
        ws3.cell(row=r, column=c, value=v).font = Font(italic=True, name="Arial", size=9)
    _autosize(ws3, [30, 6, 11, 10, 10, 10, 10])

    # ── Feuille 4 : Détail par cas ───────────────────────────────────────────
    ws4 = wb.create_sheet("Détail par cas")
    headers = ["Test", "Catégorie", "Tours", "Attendu",
               "Hybride", "OK", "LLM T=0", "OK", "LLM T=0.2", "OK",
               "LLM T=0.2+rais.", "OK", "Réflexion",
               "Lat. hyb/req", "Lat. T=0", "Lat. T=0.2", "Lat. rais."]
    for c, h in enumerate(headers, start=1):
        ws4.cell(row=1, column=c, value=h)
    _style_header(ws4, 1, len(headers))
    def mk(ok): return "✓" if ok else ("✗" if ok is False else "–")
    for i, row in enumerate(rows):
        r = 2 + i
        think_flag = "oui" if row.get("reas_think_len", 0) > 0 else "non"
        vals = [row["test_id"], row["category"], row["n_turns"], row["expected"],
                row["hybrid"], mk(row["hybrid_ok"]),
                row["fast"],   mk(row["fast_ok"]),
                row["mid"],    mk(row["mid_ok"]),
                row["reas"],   mk(row["reas_ok"]), think_flag,
                row["hybrid_lat"], row["fast_lat"], row["mid_lat"], row["reas_lat"]]
        for c, v in enumerate(vals, start=1):
            cell = ws4.cell(row=r, column=c, value=v)
            cell.border = BORDER; cell.font = NORMAL_FONT
            cell.alignment = LEFT if c == 2 else CENTER
        if row["hybrid_ok"] is True:  ws4.cell(row=r, column=6).fill = GREEN
        if row["hybrid_ok"] is False: ws4.cell(row=r, column=6).fill = RED
        if row["fast_ok"] is True:    ws4.cell(row=r, column=8).fill = GREEN
        if row["fast_ok"] is False:   ws4.cell(row=r, column=8).fill = RED
        if row["mid_ok"] is True:     ws4.cell(row=r, column=10).fill = GREEN
        if row["mid_ok"] is False:    ws4.cell(row=r, column=10).fill = RED
        if row["reas_ok"] is True:    ws4.cell(row=r, column=12).fill = GREEN
        if row["reas_ok"] is False:   ws4.cell(row=r, column=12).fill = RED
    ws4.freeze_panes = "A2"
    _autosize(ws4, [7, 18, 6, 16, 11, 4, 11, 4, 11, 4, 14, 4, 9, 12, 11, 11, 11])

    # ── Feuille 5 : Hallucinations de montant ────────────────────────────────
    ws5 = wb.create_sheet("Hallucinations montant")
    ws5["A1"] = "Montant maximal empruntable inventé par le LLM seul"
    ws5["A1"].font = TITLE_FONT
    headers = ["Test", "Description", "Catégorie", "Montant (LLM T=0)",
               "Montant (LLM T=0.2)", "Montant (LLM T=0.2+rais.)", "Décision attendue"]
    hr = 3
    for c, h in enumerate(headers, start=1):
        ws5.cell(row=hr, column=c, value=h)
    _style_header(ws5, hr, len(headers))
    credit_rows = [r for r in rows
                   if "credit" in r["category"] or "credit" in r["expected"].lower()
                   or r["fast_montant"] is not None or r["mid_montant"] is not None
                   or r["reas_montant"] is not None]
    for i, row in enumerate(credit_rows):
        r = hr + 1 + i
        vals = [row["test_id"], row["description"], row["category"],
                row["fast_montant"], row["mid_montant"], row["reas_montant"], row["expected"]]
        for c, v in enumerate(vals, start=1):
            cell = ws5.cell(row=r, column=c, value=v)
            cell.border = BORDER; cell.font = NORMAL_FONT
            cell.alignment = LEFT if c == 2 else CENTER
    _autosize(ws5, [7, 34, 18, 18, 20, 22, 18])

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    print(f"\n✅ Excel comparatif généré : {out_path}")



def cmd_compare(args):
    """
    Lance la comparaison entre le système hybride et les différents modes LLM,
    calcule les métriques de performance et génère le rapport Excel.
    """


    print("═══ MODE COMPARAISON HYBRIDE vs LLM SEUL ═══")
    print("   (baseline LLM exécuté À INFORMATION ÉGALE : règles métier ODM injectées dans le prompt)")

    # Charge les scénarios de test depuis le fichier fourni
    cases = load_cases(args.cases)

    # Affiche les informations générales de l'expérience
    print(f"Comparaison sur {len(cases)} cas — modèle : {args.model}")
    print("4 systèmes : Hybride(LLM+ODM) | LLM T=0 | LLM T=0.2 | LLM T=0.2+raisonnement")
    print("(🧠 = le mode raisonnement a bien produit un bloc de réflexion)\n")

    # Préchauffe le modèle Ollama afin d'éviter que le temps de chargement
    warm_up(args.ollama, args.model)

    print("\nÉvaluation :")

    # Exécute chaque cas de test avec les 4 configurations et Retourne les résultats détaillés de
    # chaque exécution
    rows = compare(args.api, args.ollama, args.model, cases)


    # Regroupe les résultats individuels pour obtenir
    # les statistiques globales de précision
    print("\nAgrégation…")
    agg = aggregate(rows)

    # Récupère les métriques globales
    g = agg["glob"]; n = max(g["n"], 1)

    # Affiche le taux de réussite de chaque système

    print(f"   Hybride (LLM+ODM)      : {g['hyb']}/{g['n']} = {g['hyb']/n:.1%}")
    print(f"   LLM T=0 (no_think)     : {g['fast']}/{g['n']} = {g['fast']/n:.1%}")
    print(f"   LLM T=0.2 (no_think)   : {g['mid']}/{g['n']} = {g['mid']/n:.1%}")
    print(f"   LLM T=0.2 + raisonn.   : {g['reas']}/{g['n']} = {g['reas']/n:.1%}")

    # Compte les cas où le modèle avec raisonnement
    # a réellement généré un bloc de réflexion
    n_think = sum(1 for r in rows if r.get("reas_think_len", 0) > 0)
    print(f"   (raisonnement réellement activé sur {n_think}/{len(rows)} cas)")

    # Calcule les statistiques de latence pour chaque système
    # afin de comparer leur coût en temps d'exécution
    lat_h = latency_stats(rows, "hybrid_lat")
    lat_f = latency_stats(rows, "fast_lat")
    lat_m = latency_stats(rows, "mid_lat")
    lat_r = latency_stats(rows, "reas_lat")

    # Affiche la latence au percentile 95
    print(f"   Latence/req p95 — hybride={lat_h['p95']}ms  T=0={lat_f['p95']}ms  "
          f"T=0.2={lat_m['p95']}ms  T=0.2+rais.={lat_r['p95']}ms")



    # Génère le fichier Excel contenant :
    # - comparaison des taux de réussite
    # - résultats par catégorie
    # - statistiques de latence
    # - détails de chaque cas
    # - analyse des montants générés par les LLM
    print("\nGénération de l'Excel…")
    build_excel_compare(rows, agg, args.out_compare, args.model)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — sous-commandes metrics / compare / all
# ══════════════════════════════════════════════════════════════════════════════

def main():

    # Récupère le dossier contenant le script courant
    # afin de construire les chemins par défaut des fichiers
    here = Path(__file__).parent

    # Création du parseur d'arguments permettant d'exécuter
    # le programme avec différents paramètres depuis le terminal
    ap = argparse.ArgumentParser(
        description="Harnais d'évaluation unifié (métriques système + comparaison LLM).")
    ap.add_argument("mode", choices=["metrics", "compare", "all"],
                    help="metrics = métriques système ; compare = hybride vs LLM ; all = les deux")
    # communs
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--cases", default=str(here / "test_cases.csv"))

    # Chemin du fichier Excel contenant les métriques système
    ap.add_argument("--out-metrics", default=str(here / "metriques_systeme.xlsx"))
    # compare
    ap.add_argument("--ollama", default="http://localhost:11434")
    ap.add_argument("--model", default="qwen3:8b")
    ap.add_argument("--out-compare", default=str(here / "comparaison_hybride_vs_llm.xlsx"))
    args = ap.parse_args()

    if args.mode in ("metrics", "all"):
        cmd_metrics(args)
    if args.mode in ("compare", "all"):
        if args.mode == "all":
            print("\n" + "═" * 70 + "\n")
        cmd_compare(args)


if __name__ == "__main__":
    main()