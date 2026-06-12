#!/usr/bin/env python3
# === Destination : evaluation/run_evaluation.py ===
"""
Harnais d'évaluation système.

Pipeline :
  1. Lit les cas de test (test_cases.csv).
  2. Envoie chaque cas à l'API /chat (gère le multi-tour avec '|' comme séparateur).
  3. Lit la table audit_trail (PostgreSQL) pour récupérer les latences/fallbacks/ODM.
  4. Calcule les métriques système : latence p50/p95/p99 par nœud et globale,
     taux de fallback par type, taux d'erreur/refus ODM, taux de réussite du routing.
  5. Exporte un classeur Excel formaté (plusieurs feuilles, formules Excel).

Usage :
  python run_evaluation.py
  python run_evaluation.py --api http://localhost:8000 --cases test_cases.csv --out metriques.xlsx

Prérequis : FastAPI + Spring Boot + Ollama + PostgreSQL lancés.
Dépendances : pip install httpx psycopg2-binary openpyxl
"""

import argparse
import csv
import time
import sys
import os
os.environ["PGCLIENTENCODING"] = "UTF8"
import uuid
import statistics
from datetime import datetime, timezone
from pathlib import Path


import httpx
import psycopg2
import psycopg2.extras
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from pathlib import Path

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

DB_CONFIG = _load_db_config()

def fetch_audit_rows(session_ids: list[str]) -> list[dict]:
    if not session_ids:
        return []
    conn = psycopg2.connect(**DB_CONFIG, client_encoding="UTF8")
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM audit_trail WHERE session_id = ANY(%s::uuid[]) ORDER BY created_at",
                (session_ids,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
NODE_LATENCY_COLS = [
    "latency_security", "latency_correction", "latency_router",
    "latency_extraction", "latency_validation", "latency_odm",
    "latency_generation", "latency_total",
]

# Couleurs / styles
HEADER_FILL = PatternFill("solid", start_color="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial", size=11)
TITLE_FONT  = Font(bold=True, color="1F4E78", name="Arial", size=14)
NORMAL_FONT = Font(name="Arial", size=10)
BOLD_FONT   = Font(bold=True, name="Arial", size=10)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT   = Alignment(horizontal="left", vertical="center")


# ══════════════════════════════════════════════════════════════════════════════
# 1. ENVOI DES CAS DE TEST
# ══════════════════════════════════════════════════════════════════════════════

def load_cases(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_cases(api_url: str, cases: list[dict]) -> list[dict]:
    """Envoie chaque cas (mono ou multi-tour) et renvoie le résultat du DERNIER tour."""
    results = []
    with httpx.Client(timeout=180.0) as client:
        for case in cases:
            session_id = str(uuid.uuid4())
            turns = [t.strip() for t in case["messages"].split("|") if t.strip()]
            last = None
            t0 = time.time()
            error = None
            try:
                for turn in turns:
                    resp = client.post(
                        f"{api_url}/chat",
                        json={"message": turn, "session_id": session_id},
                    )
                    resp.raise_for_status()
                    last = resp.json()
            except Exception as e:
                error = str(e)
            wall_ms = (time.time() - t0) * 1000

            results.append({
                "test_id":          case["test_id"],
                "description":      case["description"],
                "category":        case["category"],
                "session_id":       session_id,
                "n_turns":          len(turns),
                "wall_ms":          round(wall_ms, 1),
                "error":            error,
                "expected_case":    case.get("expected_case", "") or None,
                "expected_fallback":case.get("expected_fallback", "") or None,
                "expected_odm":     case.get("expected_odm_decision", "") or None,
                "got_case":         (last or {}).get("case_selected"),
                "got_fallback":     (last or {}).get("fallback_type"),
                "got_odm":          ((last or {}).get("odm_decision") or {}).get("decision"),
            })
            status = "OK" if not error else f"ERREUR ({error[:40]})"
            print(f"  [{case['test_id']}] {case['description'][:45]:45s} → {status}")
    return results





# ══════════════════════════════════════════════════════════════════════════════
# 3. CALCUL DES MÉTRIQUES
# ══════════════════════════════════════════════════════════════════════════════

def _percentile(values: list[float], p: float) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    vals.sort()
    k = (len(vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(vals) - 1)
    if f == c:
        return round(vals[f], 1)
    return round(vals[f] + (vals[c] - vals[f]) * (k - f), 1)


def compute_metrics(audit_rows: list[dict]) -> dict:
    n = len(audit_rows)

    # Latences par nœud (p50/p95/p99/moyenne)
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

    # Taux de fallback par type
    fb_counts = {k: 0 for k in ("A", "B", "C", "D", "E")}
    for r in audit_rows:
        ft = r.get("fallback_type")
        if ft in fb_counts:
            fb_counts[ft] += 1
    total_fb = sum(fb_counts.values())

    # ODM : décisions et "erreurs" (Fallback E = ODM indisponible)
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


# ══════════════════════════════════════════════════════════════════════════════
# 4. EXPORT EXCEL
# ══════════════════════════════════════════════════════════════════════════════

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


def build_excel(results: list[dict], audit_rows: list[dict], metrics: dict, out_path: str):
    wb = Workbook()

    # ── Feuille 1 : Résumé exécutif ──────────────────────────────────────────
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
        ("Taux de fallback global", None),   # formule posée après (réf. dynamiques)
        ("Appels ODM réussis", metrics["odm_calls"]),
        ("ODM indisponible (Fallback E)", metrics["odm_unavailable"]),
    ]
    start = 4
    row_of = {}   # libellé -> n° de ligne Excel
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
    # Taux de fallback global = Total fallbacks / Nombre de requêtes (réf. dynamiques)
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

    # ── Feuille 5 : Détail des cas (attendu vs obtenu) ───────────────────────
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
                cell.fill = PatternFill("solid", start_color="FFC7CE")
            elif c == 12 and v == "✓":
                cell.fill = PatternFill("solid", start_color="C6EFCE")
    ws5.freeze_panes = "A2"
    _autosize(ws5, [7, 40, 12, 7, 10, 16, 16, 12, 12, 11, 11, 10, 30])

    # ── Feuille 6 : Données brutes audit ─────────────────────────────────────
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
    print(f"\n✅ Excel généré : {out_path}")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--cases", default=str(Path(__file__).parent / "test_cases.csv"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "metriques_systeme.xlsx"))
    args = ap.parse_args()



    print("1) Envoi des cas de test à l'API…")
    cases = load_cases(args.cases)
    results = run_cases(args.api, cases)

    print("\n2) Lecture de audit_trail…")
    session_ids = [r["session_id"] for r in results]
    # petite pause pour laisser l'audit s'écrire
    time.sleep(1.0)
    audit_rows = fetch_audit_rows(session_ids)
    print(f"   {len(audit_rows)} lignes d'audit récupérées")

    print("\n3) Calcul des métriques…")
    metrics = compute_metrics(audit_rows)
    print(f"   latence totale p95 = {metrics['latency_stats']['latency_total']['p95']} ms")
    print(f"   fallbacks = {metrics['fb_counts']}")
    print(f"   décisions ODM = {metrics['odm_decisions']}")

    print("\n4) Génération de l'Excel…")
    build_excel(results, audit_rows, metrics, args.out)


if __name__ == "__main__":
    main()