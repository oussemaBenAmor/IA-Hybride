"""
Exécution de scénarios de test fournis dans un fichier CSV ou Excel.

- Accepte .csv, .xlsx et .xls (détection par extension du nom de fichier).
- Exécute chaque scénario contre l'API /chat (= appel RÉEL du système hybride
  complet : sécurité → correction → router → extraction → validation → ODM →
  génération), les tours multiples étant séparés par le caractère "|".
- Compare le résultat obtenu au résultat attendu.
- Renvoie un résumé + les lignes détaillées + un rapport Excel téléchargeable.

Colonnes reconnues (l'ORDRE n'a pas d'importance, lecture par nom d'en-tête) :
    test_id                 (obligatoire)
    messages               (obligatoire — tours séparés par "|")
    description            (facultatif)
    category               (facultatif)
    expected_case          (facultatif)
    expected_fallback      (facultatif)
    expected_odm_decision  (facultatif)
    expected_params_needed (facultatif — true/false : le système doit-il demander
                            des paramètres manquants au dernier tour ?)

À placer dans : app/services/scenario_runner.py
"""
import io
import csv
import time
import uuid
import logging

import httpx
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger("scenario_runner")

REQUIRED_COLUMNS = {"test_id", "messages"}

# ── Styles du rapport Excel ────────────────────────────────────────────────────
HEADER_FILL = PatternFill("solid", start_color="1F4E78")
HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial", size=11)
TITLE_FONT  = Font(bold=True, color="1F4E78", name="Arial", size=14)
NORMAL_FONT = Font(name="Arial", size=10)
GREEN = PatternFill("solid", start_color="C6EFCE")
RED   = PatternFill("solid", start_color="FFC7CE")
GREY  = PatternFill("solid", start_color="EDEDED")
THIN  = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT   = Alignment(horizontal="left", vertical="center")


# ── Utilitaires de parsing ─────────────────────────────────────────────────────
def _parse_optional_bool(v):
    """'true/1/oui' → True, 'false/0/non' → False, vide → None (non vérifié)."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s == "":
        return None
    if s in ("true", "1", "oui", "yes", "vrai", "x"):
        return True
    if s in ("false", "0", "non", "no", "faux"):
        return False
    return None


def _to_scenario(record: dict) -> dict | None:
    """Transforme un dict {en-tête: valeur} en scénario normalisé, ou None si vide."""
    def _get(key):
        v = record.get(key)
        return v if v not in (None, "") else None

    test_id  = _get("test_id")
    messages = _get("messages")
    if not test_id and not messages:
        return None
    if not messages:
        return None

    return {
        "test_id":               str(test_id or "").strip(),
        "description":           str(_get("description") or "").strip(),
        "category":              str(_get("category") or "").strip(),
        "messages":              str(messages).strip(),
        "expected_case":         (str(_get("expected_case")).strip()
                                  if _get("expected_case") else None),
        "expected_fallback":     (str(_get("expected_fallback")).strip()
                                  if _get("expected_fallback") else None),
        "expected_odm_decision": (str(_get("expected_odm_decision")).strip().upper()
                                  if _get("expected_odm_decision") else None),
        "expected_params_needed": _parse_optional_bool(record.get("expected_params_needed")),
    }


def _parse_csv(file_bytes: bytes) -> list[dict]:
    text = file_bytes.decode("utf-8-sig", errors="replace")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if reader.fieldnames is None:
        raise ValueError("Le fichier CSV est vide.")

    headers = [(h or "").strip().lower() for h in reader.fieldnames]
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        raise ValueError(
            f"Colonnes obligatoires manquantes : {', '.join(sorted(missing))}. "
            f"Colonnes trouvées : {', '.join(h for h in headers if h)}"
        )

    scenarios = []
    for raw in reader:
        record = {(k or "").strip().lower(): v for k, v in raw.items()}
        s = _to_scenario(record)
        if s:
            scenarios.append(s)
    return scenarios


def _parse_xlsx(file_bytes: bytes) -> list[dict]:
    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"Fichier Excel illisible : {e}") from e

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise ValueError("Le fichier est vide.")

    headers = [str(h).strip().lower() if h is not None else "" for h in rows[0]]
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        raise ValueError(
            f"Colonnes obligatoires manquantes : {', '.join(sorted(missing))}. "
            f"Colonnes trouvées : {', '.join(h for h in headers if h)}"
        )

    scenarios = []
    for raw in rows[1:]:
        record = {headers[i]: raw[i] for i in range(len(headers)) if headers[i]}
        s = _to_scenario(record)
        if s:
            scenarios.append(s)
    return scenarios


def parse_scenarios(file_bytes: bytes, filename: str = "") -> list[dict]:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        scenarios = _parse_csv(file_bytes)
    elif name.endswith((".xlsx", ".xls", ".xlsm")):
        scenarios = _parse_xlsx(file_bytes)
    else:
        try:
            scenarios = _parse_xlsx(file_bytes)
        except Exception:
            scenarios = _parse_csv(file_bytes)

    if not scenarios:
        raise ValueError("Aucun scénario exploitable trouvé dans le fichier.")
    return scenarios


# ── Exécution d'un scénario multi-tours contre le système hybride ─────────────
def _run_one(api_url: str, token: str | None, scenario: dict) -> dict:
    session_id = str(uuid.uuid4())
    turns = [t.strip() for t in scenario["messages"].split("|") if t.strip()]
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    last, error = {}, None
    t0 = time.time()
    with httpx.Client(timeout=180.0) as client:
        try:
            for turn in turns:
                resp = client.post(
                    f"{api_url}/chat",
                    json={"message": turn, "session_id": session_id},
                    headers=headers,
                )
                resp.raise_for_status()
                last = resp.json()
        except Exception as e:
            error = str(e)
    elapsed_ms = round((time.time() - t0) * 1000, 1)

    got_case     = last.get("case_selected")
    got_fallback = last.get("fallback_type")
    got_odm      = (last.get("odm_decision") or {}).get("decision")
    got_odm      = got_odm.upper() if isinstance(got_odm, str) else got_odm
    got_params_needed = bool(last.get("params_collection_needed"))

    # Évaluation : seuls les critères réellement renseignés sont comptés.
    checks = []
    if scenario["expected_case"]:
        checks.append(got_case == scenario["expected_case"])
    if scenario["expected_fallback"]:
        checks.append(got_fallback == scenario["expected_fallback"])
    if scenario["expected_odm_decision"]:
        checks.append(got_odm == scenario["expected_odm_decision"])
    if scenario["expected_params_needed"] is not None:
        checks.append(got_params_needed == scenario["expected_params_needed"])

    if error is not None:
        passed = False
    elif not checks:
        passed = None            # aucun critère attendu → non évalué
    else:
        passed = all(checks)

    return {
        "test_id":       scenario["test_id"],
        "description":   scenario["description"],
        "category":      scenario["category"],
        "n_turns":       len(turns),
        "expected_case": scenario["expected_case"],
        "got_case":      got_case,
        "expected_fallback": scenario["expected_fallback"],
        "got_fallback":  got_fallback,
        "expected_odm":  scenario["expected_odm_decision"],
        "got_odm":       got_odm,
        "expected_params_needed": scenario["expected_params_needed"],
        "got_params_needed":      got_params_needed,
        "latency_ms":    elapsed_ms,
        "passed":        passed,
        "error":         error,
        "final_response": last.get("response", ""),
    }


def run_scenarios(file_bytes: bytes, api_url: str,
                  token: str | None = None, filename: str = "") -> dict:
    scenarios = parse_scenarios(file_bytes, filename)
    logger.info("exécution de %d scénarios de test (%s)", len(scenarios), filename or "?")

    results = [_run_one(api_url, token, s) for s in scenarios]

    scored  = [r for r in results if r["passed"] is not None]
    n_pass  = sum(1 for r in scored if r["passed"])
    n_fail  = sum(1 for r in scored if not r["passed"])
    n_error = sum(1 for r in results if r["error"])
    lats    = [r["latency_ms"] for r in results if r["latency_ms"] is not None]

    summary = {
        "total":          len(results),
        "scored":         len(scored),
        "passed":         n_pass,
        "failed":         n_fail,
        "errors":         n_error,
        "pass_rate":      round(n_pass / len(scored), 4) if scored else None,
        "avg_latency_ms": round(sum(lats) / len(lats), 1) if lats else None,
    }

    report_bytes = _build_report(results, summary)
    return {"summary": summary, "results": results, "report_xlsx": report_bytes}


# ── Rapport Excel téléchargeable ───────────────────────────────────────────────
def _build_report(results: list[dict], summary: dict) -> bytes:
    wb = Workbook()

    ws = wb.active
    ws.title = "Résumé"
    ws["A1"] = "Rapport d'exécution des scénarios de test"
    ws["A1"].font = TITLE_FONT
    rows = [
        ("Indicateur", "Valeur"),
        ("Scénarios exécutés", summary["total"]),
        ("Scénarios évalués (avec attendu)", summary["scored"]),
        ("Réussis", summary["passed"]),
        ("Échoués", summary["failed"]),
        ("Erreurs d'exécution", summary["errors"]),
        ("Taux de réussite",
         f"{summary['pass_rate']:.1%}" if summary["pass_rate"] is not None else "—"),
        ("Latence moyenne (ms)",
         summary["avg_latency_ms"] if summary["avg_latency_ms"] is not None else "—"),
    ]
    for i, (k, v) in enumerate(rows, start=3):
        ws.cell(row=i, column=1, value=k).font = NORMAL_FONT
        ws.cell(row=i, column=2, value=v).font = NORMAL_FONT
        ws.cell(row=i, column=1).border = BORDER
        ws.cell(row=i, column=2).border = BORDER
    for col in (1, 2):
        ws.cell(row=3, column=col).font = HEADER_FONT
        ws.cell(row=3, column=col).fill = HEADER_FILL
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18

    ws2 = wb.create_sheet("Détail")
    headers = ["Test", "Catégorie", "Tours", "Résultat",
               "Cas att.", "Cas obt.",
               "Fallback att.", "Fallback obt.",
               "ODM att.", "ODM obt.",
               "Params att.", "Params obt.",
               "Latence (ms)", "Erreur"]
    for c, h in enumerate(headers, start=1):
        cell = ws2.cell(row=1, column=c, value=h)
        cell.fill = HEADER_FILL; cell.font = HEADER_FONT
        cell.alignment = CENTER; cell.border = BORDER

    def verdict(p):
        return "RÉUSSI" if p is True else ("ÉCHOUÉ" if p is False else "—")

    def bopt(b):
        return "—" if b is None else ("oui" if b else "non")

    for i, r in enumerate(results):
        row = 2 + i
        vals = [
            r["test_id"], r["category"], r["n_turns"], verdict(r["passed"]),
            r["expected_case"] or "—", r["got_case"] or "—",
            r["expected_fallback"] or "—", r["got_fallback"] or "—",
            r["expected_odm"] or "—", r["got_odm"] or "—",
            bopt(r["expected_params_needed"]), bopt(r["got_params_needed"]),
            r["latency_ms"], (r["error"] or "")[:60],
            ]
        for c, v in enumerate(vals, start=1):
            cell = ws2.cell(row=row, column=c, value=v)
            cell.border = BORDER; cell.font = NORMAL_FONT
            cell.alignment = LEFT if c in (2, 14) else CENTER
        if r["passed"] is True:
            ws2.cell(row=row, column=4).fill = GREEN
        elif r["passed"] is False:
            ws2.cell(row=row, column=4).fill = RED
        else:
            ws2.cell(row=row, column=4).fill = GREY

    ws2.freeze_panes = "A2"
    widths = [10, 16, 6, 10, 18, 18, 12, 12, 18, 18, 11, 11, 11, 40]
    for i, w in enumerate(widths, start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_template_xlsx() -> bytes:
    """Génère un modèle Excel vierge que l'admin peut télécharger et remplir."""
    wb = Workbook()
    ws = wb.active
    ws.title = "scenarios"
    headers = ["test_id", "description", "category", "messages",
               "expected_case", "expected_fallback", "expected_odm_decision",
               "expected_params_needed"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = HEADER_FILL; cell.font = HEADER_FONT; cell.alignment = CENTER
    ws.append([
        "T001", "Crédit immo complet", "nominal",
        "je veux emprunter 200000 euros sur 240 mois, je gagne 5000 par mois et j'ai 35 ans",
        "credit_immobilier", "", "ELIGIBLE", "",
    ])
    ws.append([
        "T002", "Multi-tours : collecte puis décision", "multitour",
        "je veux un crédit immobilier de 200000 euros sur 240 mois, je gagne 5000 euros|j'ai 35 ans",
        "credit_immobilier", "", "ELIGIBLE", "",
    ])
    ws.append([
        "T003", "Le système doit demander un paramètre manquant", "params_manquants",
        "je veux modifier le plafond de ma carte mais je ne sais pas encore combien",
        "carte_bancaire", "", "", "true",
    ])
    ws.append([
        "T004", "Hors périmètre", "hors_perimetre",
        "quelle est la météo demain", "", "A", "", "",
    ])
    widths = [10, 34, 20, 62, 20, 18, 22, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()