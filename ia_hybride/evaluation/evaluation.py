#!/usr/bin/env python3
# === Destination : evaluation/run_full_evaluation.py ===
"""
Harnais d'évaluation UNIFIÉ pour le conseiller bancaire hybride (LLM + BRMS/ODM).

Ce fichier FUSIONNE les deux anciens scripts (evaluation.py + comparaison.py) en
un seul, avec du code commun partagé (styles Excel, lecture des cas, percentiles,
appel du système hybride). Il fonctionne avec l'ODM RÉEL : les scripts passent par
l'API /chat (donc par Spring → ODM), ils ne parlent jamais directement au moteur ;
le passage mock→ODM réel est transparent (on lit toujours odm_decision.decision).

────────────────────────────────────────────────────────────────────────────────
DEUX MODES (sous-commandes) :

  1) metrics      → métriques SYSTÈME (ex-evaluation.py)
     Envoie les cas à /chat, lit la table audit_trail (PostgreSQL), calcule les
     latences p50/p95/p99 par nœud, les taux de fallback, les décisions ODM, et
     exporte un Excel (Résumé, Latences par nœud, Fallbacks, ODM, Cas de test,
     Audit brut).

  2) compare      → COMPARAISON hybride vs LLM seul (ex-comparaison.py)
     Compare 3 systèmes sur les mêmes cas : Hybride (/chat), LLM seul rapide
     (Ollama /no_think, T=0), LLM seul raisonnement (think=true, T=0.2). Exporte
     un Excel (Synthèse, Par catégorie, Latence, Détail par cas, Hallucinations).

  3) all          → lance metrics PUIS compare (deux fichiers Excel générés).

────────────────────────────────────────────────────────────────────────────────
USAGE :
  python run_full_evaluation.py metrics
  python run_full_evaluation.py compare
  python run_full_evaluation.py all

  # options communes :
  #   --api http://localhost:8000    --cases test_cases.csv
  # options metrics :
  #   --out-metrics metriques_systeme.xlsx
  # options compare :
  #   --ollama http://localhost:11434 --model qwen3:8b
  #   --out-compare comparaison_hybride_vs_llm.xlsx

Prérequis : FastAPI + Spring Boot + ODM + Ollama + PostgreSQL lancés.
Dépendances : pip install httpx psycopg2-binary openpyxl
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

os.environ["PGCLIENTENCODING"] = "UTF8"

import httpx
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# psycopg2 n'est nécessaire QUE pour le mode "metrics" (audit_trail).
# On l'importe paresseusement pour que "compare" tourne même sans psycopg2 installé.
try:
    import psycopg2
    import psycopg2.extras
    _HAS_PSYCOPG2 = True
except Exception:
    _HAS_PSYCOPG2 = False


# ══════════════════════════════════════════════════════════════════════════════
# STYLES EXCEL COMMUNS (partagés par les deux modes)
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
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _percentile(values: list, p: float):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return round(vals[f], 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 1)


# ══════════════════════════════════════════════════════════════════════════════
# COMMUN : appel du SYSTÈME HYBRIDE (/chat)
# ══════════════════════════════════════════════════════════════════════════════

def _hybrid_label(last: dict) -> str | None:
    """Déduit un label normalisé depuis la dernière réponse /chat.

    Cas PARAMS_REQUIS : si le système hybride réclame un paramètre manquant
    (params_collection_needed) au lieu de rendre une décision, c'est la BONNE
    réponse pour un dossier incomplet — on le marque PARAMS_REQUIS. C'est
    précisément l'avantage du moteur de règles : il refuse de décider sans
    données complètes (là où le LLM seul invente une décision)."""
    fb = last.get("fallback_type")
    odm = (last.get("odm_decision") or {}).get("decision")
    if last.get("params_collection_needed"):
        return "PARAMS_REQUIS"
    if fb == "A":
        return "HORS_PERIMETRE"
    if fb == "D":
        return "BLOQUE"
    if fb == "B":
        return "AMBIGU"
    if odm:
        return odm.upper()
    return None


def run_hybrid(api_url: str, case: dict) -> dict:
    """Envoie un cas (mono/multi-tour) à /chat. Latence mesurée PAR TOUR.

    Un tour = un appel /chat, comparable au LLM seul (un appel). On expose la
    latence moyenne par tour (métrique principale), la latence du dernier tour,
    et le total de la conversation (pour information).
    """
    session_id = str(uuid.uuid4())
    turns = [t.strip() for t in case["messages"].split("|") if t.strip()]
    last, error = None, None
    per_turn_ms = []
    with httpx.Client(timeout=180.0) as client:
        try:
            for turn in turns:
                t0 = time.time()
                resp = client.post(f"{api_url}/chat",
                                   json={"message": turn, "session_id": session_id})
                resp.raise_for_status()
                per_turn_ms.append((time.time() - t0) * 1000)
                last = resp.json()
        except Exception as e:
            error = str(e)

    last = last or {}
    mean_turn = round(statistics.mean(per_turn_ms), 1) if per_turn_ms else None
    last_turn = round(per_turn_ms[-1], 1) if per_turn_ms else None
    total_ms  = round(sum(per_turn_ms), 1) if per_turn_ms else None
    return {
        "session_id":         session_id,
        "label":              _hybrid_label(last),
        "latency_per_req_ms": mean_turn,
        "latency_last_ms":    last_turn,
        "latency_total_ms":   total_ms,
        "n_turns":            len(turns),
        "raw":                last,
        "error":              error,
    }


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
#  MODE 1 : MÉTRIQUES SYSTÈME  (ex-evaluation.py)
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════

def _load_db_config() -> dict:
    """Lit les identifiants PostgreSQL depuis le .env du service FastAPI."""
    env_path = Path(__file__).parent.parent / "fastapi-service" / ".env"
    vals = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    return {
        "host":     vals.get("POSTGRES_HOST", "localhost"),
        "port":     int(vals.get("POSTGRES_PORT", "5432")),
        "user":     vals.get("POSTGRES_USER", "oussema"),
        "password": vals.get("POSTGRES_PASSWORD", "081002"),
        "dbname":   vals.get("POSTGRES_DB", "test"),
    }


NODE_LATENCY_COLS = [
    "latency_security", "latency_correction", "latency_router",
    "latency_extraction", "latency_validation", "latency_odm",
    "latency_generation", "latency_total",
]


def fetch_audit_rows(db_config: dict, session_ids: list[str]) -> list[dict]:
    if not session_ids:
        return []
    if not _HAS_PSYCOPG2:
        print("   ⚠ psycopg2 non installé — audit_trail ignoré "
              "(pip install psycopg2-binary).")
        return []
    conn = psycopg2.connect(**db_config, client_encoding="UTF8")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM audit_trail WHERE session_id = ANY(%s::uuid[]) ORDER BY created_at",
                (session_ids,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def run_cases_metrics(api_url: str, cases: list[dict]) -> list[dict]:
    """Envoie chaque cas (mono/multi-tour) et renvoie le résultat du DERNIER tour.
    Réutilise run_hybrid pour l'appel, puis reformate pour le rapport métriques."""
    results = []
    for case in cases:
        hyb = run_hybrid(api_url, case)
        last = hyb["raw"]
        results.append({
            "test_id":           case["test_id"],
            "description":       case["description"],
            "category":          case["category"],
            "session_id":        hyb["session_id"],
            "n_turns":           hyb["n_turns"],
            "wall_ms":           hyb["latency_total_ms"],
            "error":             hyb["error"],
            "expected_case":     case.get("expected_case", "") or None,
            "expected_fallback": case.get("expected_fallback", "") or None,
            "expected_odm":      case.get("expected_odm_decision", "") or None,
            "got_case":          last.get("case_selected"),
            "got_fallback":      last.get("fallback_type"),
            "got_odm":           (last.get("odm_decision") or {}).get("decision"),
        })
        status = "OK" if not hyb["error"] else f"ERREUR ({hyb['error'][:40]})"
        print(f"  [{case['test_id']}] {case['description'][:45]:45s} → {status}")
    return results


def compute_metrics(audit_rows: list[dict]) -> dict:
    n = len(audit_rows)

    latency_stats = {}
    for col in NODE_LATENCY_COLS:
        series = [float(r[col]) for r in audit_rows if r.get(col) is not None]
        latency_stats[col] = {
            "n":    len(series),
            "mean": round(statistics.mean(series), 1) if series else None,
            "p50":  _percentile(series, 0.50),
            "p95":  _percentile(series, 0.95),
            "p99":  _percentile(series, 0.99),
            "max":  round(max(series), 1) if series else None,
        }

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
        if dec:
            odm_decisions[dec] = odm_decisions.get(dec, 0) + 1
            odm_calls += 1
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
    """Exécute le mode métriques système. Renvoie les résultats bruts (réutilisables)."""
    print("═══ MODE MÉTRIQUES SYSTÈME ═══")
    print("1) Envoi des cas de test à l'API…")
    cases = load_cases(args.cases)
    results = run_cases_metrics(args.api, cases)

    print("\n2) Lecture de audit_trail…")
    session_ids = [r["session_id"] for r in results]
    time.sleep(1.0)  # laisser l'audit s'écrire
    db_config = _load_db_config()
    audit_rows = fetch_audit_rows(db_config, session_ids)
    print(f"   {len(audit_rows)} lignes d'audit récupérées")

    print("\n3) Calcul des métriques…")
    metrics = compute_metrics(audit_rows)
    lt = metrics["latency_stats"]["latency_total"]
    print(f"   latence totale p95 = {lt['p95']} ms")
    print(f"   fallbacks = {metrics['fb_counts']}")
    print(f"   décisions ODM = {metrics['odm_decisions']}")

    print("\n4) Génération de l'Excel…")
    build_excel_metrics(results, audit_rows, metrics, args.out_metrics)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
#  MODE 2 : COMPARAISON HYBRIDE vs LLM SEUL  (ex-comparaison.py)
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════

VALID_DECISIONS = {"ELIGIBLE", "REFUSE", "APPROUVE", "VERIFICATION_REQUISE",
                   "HORS_PERIMETRE", "BLOQUE", "PARAMS_REQUIS"}


def expected_label(case: dict) -> str | None:
    odm = (case.get("expected_odm_decision") or "").strip().upper()
    if odm:
        return odm  # inclut PARAMS_REQUIS : la bonne réponse est de RÉCLAMER l'info manquante
    fb = (case.get("expected_fallback") or "").strip().upper()
    if fb == "A":
        return "HORS_PERIMETRE"
    if fb == "D":
        return "BLOQUE"
    return None


LLM_ONLY_PROMPT = """Tu es un conseiller bancaire qui doit traiter une demande client.
Tu dois prendre TOI-MÊME la décision, sans outil externe.

Demande du client : "{message}"

Rends une décision selon ces catégories possibles :
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

_LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "decision":                {"type": "string"},
        "montant_max_empruntable": {"type": ["number", "null"]},
        "justification":           {"type": "string"},
    },
    "required": ["decision"],
}


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
    full_msg = " ".join(t.strip() for t in case["messages"].split("|") if t.strip())
    body = LLM_ONLY_PROMPT.format(message=full_msg)

    payload = {
        "model": model,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": temperature},
    }
    if think:
        payload["prompt"] = body
        payload["think"] = True
    else:
        payload["prompt"] = "/no_think\n" + body
        payload["format"] = _LLM_SCHEMA

    t0 = time.time()
    error, parsed, thinking_len = None, {}, 0
    try:
        resp = httpx.post(f"{ollama_url}/api/generate", json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        raw = (data.get("response") or "").strip()
        thinking_len = len(data.get("thinking") or "")
        parsed = _safe_json(raw) or {}
    except Exception as e:
        error = str(e)
    elapsed = (time.time() - t0) * 1000

    decision = (parsed.get("decision") or "").strip().upper()
    # Filet de sécurité : si le LLM n'a pas mis PARAMS_REQUIS mais que sa
    # justification réclame clairement une info manquante, on le crédite quand même.
    justif = (parsed.get("justification") or "").lower()
    _MANQUE = ("manque", "information manquante", "informations manquantes",
               "préciser", "preciser", "il faut connaître", "il faut connaitre",
               "besoin de plus", "données insuffisantes", "donnees insuffisantes",
               "revenu n'est pas", "montant n'est pas", "durée n'est pas",
               "impossible de déterminer", "impossible de determiner")
    if decision not in VALID_DECISIONS and any(k in justif for k in _MANQUE):
        decision = "PARAMS_REQUIS"
    if decision not in VALID_DECISIONS:
        decision = decision or "(vide)"
    return {
        "label": decision,
        "montant_max": parsed.get("montant_max_empruntable"),
        "justification": parsed.get("justification", ""),
        "latency_ms": round(elapsed, 1),
        "thinking_len": thinking_len,
        "error": error,
    }


def warm_up(ollama_url: str, model: str, n: int = 2):
    print(f"Warm-up Ollama ({n} appels)…")
    for _ in range(n):
        try:
            httpx.post(f"{ollama_url}/api/generate",
                       json={"model": model, "prompt": "/no_think\nbonjour",
                             "stream": False, "keep_alive": "30m"},
                       timeout=120.0)
        except Exception:
            pass


def compare(api_url, ollama_url, model, cases) -> list[dict]:
    rows = []
    for case in cases:
        exp = expected_label(case)
        hyb  = run_hybrid(api_url, case)
        fast = run_llm_only(ollama_url, model, case, think=False, temperature=0.0)
        mid  = run_llm_only(ollama_url, model, case, think=False, temperature=0.2)
        reas = run_llm_only(ollama_url, model, case, think=True,  temperature=0.2)

        scored = exp is not None
        hyb_ok  = (hyb["label"]  == exp) if scored else None
        fast_ok = (fast["label"] == exp) if scored else None
        mid_ok  = (mid["label"]  == exp) if scored else None
        reas_ok = (reas["label"] == exp) if scored else None

        rows.append({
            "test_id": case["test_id"],
            "description": case["description"],
            "category": case["category"],
            "expected": exp or "(non scoré)",
            "scored": scored,
            "n_turns": hyb["n_turns"],
            "hybrid": hyb["label"] or "(vide)",
            "hybrid_ok": hyb_ok,
            "hybrid_lat": hyb["latency_per_req_ms"],
            "hybrid_lat_total": hyb["latency_total_ms"],
            "fast": fast["label"],
            "fast_ok": fast_ok,
            "fast_lat": fast["latency_ms"],
            "fast_montant": fast.get("montant_max"),
            "mid": mid["label"],
            "mid_ok": mid_ok,
            "mid_lat": mid["latency_ms"],
            "mid_montant": mid.get("montant_max"),
            "reas": reas["label"],
            "reas_ok": reas_ok,
            "reas_lat": reas["latency_ms"],
            "reas_montant": reas.get("montant_max"),
            "reas_think_len": reas.get("thinking_len", 0),
            "reas_justif": reas.get("justification", ""),
        })
        def m(ok): return "✓" if ok else ("✗" if ok is False else "–")
        tflag = "🧠" if reas.get("thinking_len", 0) > 0 else "·"
        print(f"  [{case['test_id']}] {case['category']:18s} att={exp or '-':16s} "
              f"hyb={hyb['label'] or '-':9s}[{m(hyb_ok)}] "
              f"fast={fast['label']:9s}[{m(fast_ok)}] "
              f"mid={mid['label']:9s}[{m(mid_ok)}] "
              f"reas={reas['label']:9s}[{m(reas_ok)}]{tflag}")
    return rows


def aggregate(rows: list[dict]) -> dict:
    by_cat = defaultdict(lambda: {"n": 0, "hyb": 0, "fast": 0, "mid": 0, "reas": 0})
    glob = {"n": 0, "hyb": 0, "fast": 0, "mid": 0, "reas": 0}
    for r in rows:
        if not r["scored"]:
            continue
        c = by_cat[r["category"]]
        c["n"] += 1; glob["n"] += 1
        if r["hybrid_ok"]: c["hyb"]  += 1; glob["hyb"]  += 1
        if r["fast_ok"]:   c["fast"] += 1; glob["fast"] += 1
        if r["mid_ok"]:    c["mid"]  += 1; glob["mid"]  += 1
        if r["reas_ok"]:   c["reas"] += 1; glob["reas"] += 1
    return {"by_cat": dict(by_cat), "glob": glob}


def latency_stats(rows, key):
    series = [r[key] for r in rows if r.get(key) is not None]
    if not series:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "n": len(series),
        "mean": round(statistics.mean(series), 1),
        "p50": _percentile(series, 0.50),
        "p95": _percentile(series, 0.95),
        "p99": _percentile(series, 0.99),
        "max": round(max(series), 1),
    }


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
    hr = 4
    headers = ["Indicateur", "Hybride (LLM+ODM)", "LLM T=0 (no_think)",
               "LLM T=0.2 (no_think)", "LLM T=0.2 + raisonnement"]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=hr, column=c, value=h)
    _style_header(ws, hr, len(headers))
    data = [
        ("Cas évalués (avec vérité terrain)", g["n"], g["n"], g["n"], g["n"]),
        ("Décisions correctes", g["hyb"], g["fast"], g["mid"], g["reas"]),
        ("Décisions erronées", g["n"]-g["hyb"], g["n"]-g["fast"], g["n"]-g["mid"], g["n"]-g["reas"]),
        ("Taux de réussite", g["hyb"]/n, g["fast"]/n, g["mid"]/n, g["reas"]/n),
        ("Taux d'erreur", 1-g["hyb"]/n, 1-g["fast"]/n, 1-g["mid"]/n, 1-g["reas"]/n),
    ]
    for i, (k, *vals4) in enumerate(data):
        r = hr + 1 + i
        ws.cell(row=r, column=1, value=k).font = NORMAL_FONT
        ws.cell(row=r, column=1).border = BORDER
        for col, v in zip((2, 3, 4, 5), vals4):
            cell = ws.cell(row=r, column=col, value=v)
            cell.font = BOLD_FONT; cell.alignment = CENTER; cell.border = BORDER
            if "Taux" in k:
                cell.number_format = "0.0%"
    _autosize(ws, [38, 18, 20, 20, 24])

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
    print("═══ MODE COMPARAISON HYBRIDE vs LLM SEUL ═══")
    cases = load_cases(args.cases)
    print(f"Comparaison sur {len(cases)} cas — modèle : {args.model}")
    print("4 systèmes : Hybride(LLM+ODM) | LLM T=0 | LLM T=0.2 | LLM T=0.2+raisonnement")
    print("(🧠 = le mode raisonnement a bien produit un bloc de réflexion)\n")
    warm_up(args.ollama, args.model)

    print("\nÉvaluation :")
    rows = compare(args.api, args.ollama, args.model, cases)

    print("\nAgrégation…")
    agg = aggregate(rows)
    g = agg["glob"]; n = max(g["n"], 1)
    print(f"   Hybride (LLM+ODM)      : {g['hyb']}/{g['n']} = {g['hyb']/n:.1%}")
    print(f"   LLM T=0 (no_think)     : {g['fast']}/{g['n']} = {g['fast']/n:.1%}")
    print(f"   LLM T=0.2 (no_think)   : {g['mid']}/{g['n']} = {g['mid']/n:.1%}")
    print(f"   LLM T=0.2 + raisonn.   : {g['reas']}/{g['n']} = {g['reas']/n:.1%}")
    n_think = sum(1 for r in rows if r.get("reas_think_len", 0) > 0)
    print(f"   (raisonnement réellement activé sur {n_think}/{len(rows)} cas)")
    lat_h = latency_stats(rows, "hybrid_lat")
    lat_f = latency_stats(rows, "fast_lat")
    lat_m = latency_stats(rows, "mid_lat")
    lat_r = latency_stats(rows, "reas_lat")
    print(f"   Latence/req p95 — hybride={lat_h['p95']}ms  T=0={lat_f['p95']}ms  "
          f"T=0.2={lat_m['p95']}ms  T=0.2+rais.={lat_r['p95']}ms")

    print("\nGénération de l'Excel…")
    build_excel_compare(rows, agg, args.out_compare, args.model)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — sous-commandes metrics / compare / all
# ══════════════════════════════════════════════════════════════════════════════

def main():
    here = Path(__file__).parent
    ap = argparse.ArgumentParser(
        description="Harnais d'évaluation unifié (métriques système + comparaison LLM).")
    ap.add_argument("mode", choices=["metrics", "compare", "all"],
                    help="metrics = métriques système ; compare = hybride vs LLM ; all = les deux")
    # communs
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--cases", default=str(here / "test_cases.csv"))
    # metrics
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