"""
Page « Dashboard » (administrateur).

Suit le thème Light / Dark de l'utilisateur (variables de thème + Plotly theme="streamlit").
- Donuts : pourcentages À L'INTÉRIEUR des parts + noms dans une LÉGENDE en bas
  → plus aucun libellé coupé.
- « Attaques bloquées » = fallback Sécurité (D) → cohérent avec la barre du graphe.
- Section temps de réponse : médiane + 95 % des cas (percentiles de latence totale).

À placer dans : frontend/dashboard.py
Dépendances : pip install plotly pandas
"""
import base64

import httpx
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

FASTAPI_URL = "http://localhost:8000"

C_BLUE, C_VIOLET, C_TEAL = "#4361ee", "#7209b7", "#4cc9f0"
C_GREEN, C_AMBER, C_ROSE = "#16a34a", "#f9a03f", "#e11d48"
PALETTE = [C_BLUE, C_VIOLET, C_TEAL, C_GREEN, C_AMBER, C_ROSE]

# Libellés courts pour les décisions ODM (évite les noms trop longs dans la légende).
ODM_SHORT = {"VERIFICATION_REQUISE": "Vérif. requise", "ELIGIBLE": "Éligible",
             "REFUSE": "Refusé", "APPROUVE": "Approuvé"}

# Couleurs FIXES par cas métier — teintes bien séparées (bleu, violet, cyan, orange, rose).
CASE_COLORS = {
    "credit_immobilier":   "#4361ee",   # bleu
    "credit_consommation": "#7209b7",   # violet profond
    "assurance_vie":       "#4cc9f0",   # cyan
    "carte_bancaire":      "#f9a03f",   # orange
    "virement":            "#f72585",   # rose/magenta
}

# Couleurs FIXES par décision ODM — Éligible et Approuvé désormais BIEN distincts.
DEC_COLORS = {
    "ELIGIBLE":             "#16a34a",   # vert  (favorable)
    "APPROUVE":             "#0891b2",   # cyan-canard (favorable, distinct du vert)
    "REFUSE":               "#e11d48",   # rouge (défavorable)
    "VERIFICATION_REQUISE": "#f9a03f",   # orange (à vérifier)
}


def _auth_headers() -> dict:
    tok = st.session_state.get("token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


if not st.session_state.get("token"):
    st.warning("Veuillez vous connecter.")
    st.stop()
if st.session_state.get("role") != "admin":
    st.error("⛔ Accès réservé aux administrateurs.")
    st.stop()


st.markdown(
    """
    <style>
    .kpi-grid{ display:grid; grid-template-columns:repeat(4,1fr); gap:1rem; margin:.3rem 0 1.2rem; }
    .kpi{
        border-radius:18px; padding:1.05rem 1.2rem; position:relative; overflow:hidden;
        background:var(--secondary-background-color);
        border:1px solid color-mix(in srgb, var(--text-color) 14%, transparent);
        box-shadow:0 20px 40px -34px color-mix(in srgb, var(--text-color) 55%, transparent);
    }
    .kpi::before{ content:""; position:absolute; left:0; top:0; bottom:0; width:4px; }
    .kpi.b::before{ background:#3b5bdb; } .kpi.v::before{ background:#8b5cf6; }
    .kpi.g::before{ background:#16a34a; } .kpi.r::before{ background:#e11d48; }
    .kpi .lbl{ font-size:.72rem; text-transform:uppercase; letter-spacing:.05em;
        color:color-mix(in srgb, var(--text-color) 60%, transparent); font-weight:700; }
    .kpi .val{ font-family:'Sora',sans-serif; font-size:1.95rem; font-weight:800;
        color:var(--text-color); line-height:1.1; margin-top:.25rem; }
    .kpi .sub{ font-size:.74rem; color:color-mix(in srgb, var(--text-color) 45%, transparent); margin-top:.15rem; }
    .sec-title{ font-family:'Sora',sans-serif; font-weight:800; font-size:1.15rem;
        color:var(--text-color); margin:1.5rem 0 .3rem; }
    @media(max-width:1100px){ .kpi-grid{ grid-template-columns:repeat(2,1fr); } }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📊 Dashboard administrateur")
st.caption("Vue agrégée de la trace d'audit. Les chiffres reflètent le dernier chargement — "
           "cliquez sur « Actualiser » pour les recharger.")

if st.button("🔄 Actualiser les données"):
    st.rerun()


def fetch_dashboard():
    r = httpx.get(f"{FASTAPI_URL}/admin/dashboard", headers=_auth_headers(), timeout=60)
    r.raise_for_status()
    return r.json()


try:
    data = fetch_dashboard()
except Exception as e:
    st.error(f"Impossible de charger le dashboard : {e}")
    st.stop()


def _num(x, d=0):
    return d if x is None else x


totals = data.get("totals", {})

st.markdown(
    f"""
    <div class="kpi-grid">
      <div class="kpi b"><div class="lbl">Requêtes traitées</div>
        <div class="val">{_num(totals.get('requests'))}</div>
        <div class="sub">tours passés dans le pipeline</div></div>
      <div class="kpi v"><div class="lbl">Conversations</div>
        <div class="val">{_num(totals.get('sessions'))}</div>
        <div class="sub">sessions avec au moins un message</div></div>
      <div class="kpi g"><div class="lbl">Utilisateurs</div>
        <div class="val">{_num(totals.get('users'))}</div>
        <div class="sub">comptes enregistrés</div></div>
      <div class="kpi r"><div class="lbl">Attaques bloquées</div>
        <div class="val">{_num(totals.get('attacks'))}</div>
        <div class="sub">requêtes stoppées par la sécurité</div></div>
    </div>
    """,
    unsafe_allow_html=True,
)


# ── Graphes ───────────────────────────────────────────────────────────────────
def donut(labels, values, colors=None):
    """Donut avec % à l'intérieur et légende (noms) en bas → aucun label coupé."""
    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=.60, sort=False,
        marker=dict(colors=colors or PALETTE, line=dict(width=1)),
        textinfo="percent", textposition="inside", insidetextorientation="horizontal",
    ))
    fig.update_layout(
        height=360, margin=dict(l=10, r=10, t=10, b=70), showlegend=True,
        legend=dict(orientation="h", yanchor="top", y=-0.02, xanchor="center", x=0.5,
                    font=dict(size=11)),
        uniformtext_minsize=10, uniformtext_mode="hide",
    )
    return fig


def hbar(labels, values, colors=None):
    fig = go.Figure(go.Bar(x=values, y=labels, orientation="h",
                           marker=dict(color=colors or C_BLUE),
                           text=values, textposition="outside", cliponaxis=False))
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
    return fig


def vbar(labels, values, colors=None):
    fig = go.Figure(go.Bar(x=labels, y=values, marker=dict(color=colors or C_TEAL),
                           text=[f"{v:.0f}" for v in values], textposition="outside", cliponaxis=False))
    fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10), showlegend=False)
    return fig


_CHART = dict(use_container_width=True, theme="streamlit", config={"displayModeBar": False})

c1, c2 = st.columns(2)
with c1:
    st.markdown("<div class='sec-title'>Répartition des cas métier</div>", unsafe_allow_html=True)
    by_case = data.get("by_case", [])
    if by_case:
        _lab = [r["business_case"] for r in by_case]
        _col = [CASE_COLORS.get(l, "#94a3b8") for l in _lab]
        st.plotly_chart(donut(_lab, [r["n"] for r in by_case], _col), **_CHART)
    else:
        st.info("Aucune donnée.")

with c2:
    st.markdown("<div class='sec-title'>Analyse des fallbacks</div>", unsafe_allow_html=True)
    fb = data.get("by_fallback", [])
    if fb:
        fb_labels = {"A": "A · Hors périmètre", "B": "B · Ambiguïté", "C": "C · Params invalides",
                     "D": "D · Sécurité", "E": "E · ODM indispo."}
        fb_colors = {"A": C_AMBER, "B": C_TEAL, "C": C_VIOLET, "D": C_ROSE, "E": "#fb7185"}
        labels = [fb_labels.get(r["fallback_type"], r["fallback_type"]) for r in fb]
        colors = [fb_colors.get(r["fallback_type"], C_BLUE) for r in fb]
        st.plotly_chart(hbar(labels, [r["n"] for r in fb], colors), **_CHART)
    else:
        st.info("Aucun fallback enregistré.")

c3, c4 = st.columns(2)
with c3:
    st.markdown("<div class='sec-title'>Décisions ODM</div>", unsafe_allow_html=True)
    dec = data.get("by_decision", [])
    if dec:
        labels = [ODM_SHORT.get(r["odm_decision"], r["odm_decision"]) for r in dec]
        colors = [DEC_COLORS.get(r["odm_decision"], "#94a3b8") for r in dec]
        st.plotly_chart(donut(labels, [r["n"] for r in dec], colors), **_CHART)
    else:
        st.info("Aucune décision ODM.")

with c4:
    st.markdown("<div class='sec-title'>Latence moyenne par nœud (ms)</div>", unsafe_allow_html=True)
    lat = data.get("latency_by_node", {})
    name_map = {"s": "security", "c": "correction", "r": "router", "e": "extraction",
                "v": "validation", "o": "odm", "g": "generation"}
    order = ["s", "c", "r", "e", "v", "o", "g"]
    rows = [(name_map[k], lat.get(k)) for k in order if lat.get(k) is not None]
    if rows:
        st.plotly_chart(vbar([n for n, _ in rows], [v for _, v in rows]), **_CHART)
        if lat.get("t") is not None:
            st.caption(f"Latence totale moyenne : **{lat['t']:.0f} ms**")
    else:
        st.info("Aucune latence enregistrée.")


# ── Temps de réponse (médiane + 95 % des cas) ─────────────────────────────────
def _fmt_ms(v):
    if v is None:
        return "—"
    return f"{v/1000:.1f} s" if v >= 1000 else f"{v:.0f} ms"


pct = data.get("latency_pct", {})
st.markdown("<div class='sec-title'>Temps de réponse</div>", unsafe_allow_html=True)
st.caption("Temps total de traitement d'une requête, de bout en bout.")
t1, t2 = st.columns(2)
t1.metric("Temps de réponse pour la moitié des demandes", _fmt_ms(pct.get("p50")),
          help="La moitié des requêtes sont traitées plus rapidement que cette valeur.")
t2.metric("Temps de réponse pour 95 % des demandes", _fmt_ms(pct.get("p95")),
          help="95 % des requêtes sont traitées plus rapidement que cette valeur.")

st.divider()

# ══════════════════════════════════════════════════════════════════════════════
# EXÉCUTION DE SCÉNARIOS DE TEST (CSV ou Excel)
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("<div class='sec-title'>🧪 Exécution de scénarios de test</div>", unsafe_allow_html=True)
st.write("")

try:
    tpl = httpx.get(f"{FASTAPI_URL}/admin/test-template", headers=_auth_headers(), timeout=30)
    if tpl.status_code == 200:
        st.download_button("⬇️ Télécharger le modèle Excel", data=tpl.content,
                           file_name="modele_scenarios.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
except Exception:
    pass

st.session_state.setdefault("test_running", False)
st.session_state.setdefault("test_payload", None)

uploaded = st.file_uploader("Fichier de scénarios (.csv ou .xlsx)", type=["csv", "xlsx", "xls"])

clicked = st.button("▶️ Exécuter les scénarios", type="primary",
                    disabled=st.session_state.test_running or uploaded is None)

if clicked and not st.session_state.test_running:
    st.session_state.test_running = True
    st.session_state.test_payload = None
    st.rerun()

if st.session_state.test_running:
    with st.spinner("Exécution en cours… chaque scénario interroge le système hybride."):
        try:
            if uploaded is None:
                raise ValueError("Aucun fichier fourni.")
            files = {"file": (uploaded.name, uploaded.getvalue(), "application/octet-stream")}
            r = httpx.post(f"{FASTAPI_URL}/admin/run-tests", files=files,
                           headers=_auth_headers(), timeout=3600)
            r.raise_for_status()
            st.session_state.test_payload = r.json()
        except httpx.HTTPStatusError as e:
            try:
                detail = e.response.json().get("detail", str(e))
            except Exception:
                detail = str(e)
            st.session_state.test_payload = {"error": detail}
        except Exception as e:
            st.session_state.test_payload = {"error": str(e)}
    st.session_state.test_running = False
    st.rerun()

payload = st.session_state.test_payload
if payload:
    if payload.get("error"):
        st.error(f"Erreur : {payload['error']}")
    else:
        summary = payload["summary"]
        st.success("Exécution terminée.")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total", summary["total"])
        m2.metric("Réussis", summary["passed"])
        m3.metric("Échoués", summary["failed"])
        m4.metric("Erreurs", summary["errors"])
        m5.metric("Taux de réussite",
                  f"{summary['pass_rate']:.1%}" if summary["pass_rate"] is not None else "—")
        if summary.get("avg_latency_ms") is not None:
            st.caption(f"Latence moyenne par scénario : **{summary['avg_latency_ms']} ms**")

        df = pd.DataFrame(payload["results"])
        show_cols = ["test_id", "category", "n_turns", "passed",
                     "expected_case", "got_case", "expected_fallback", "got_fallback",
                     "expected_odm", "got_odm",
                     "expected_params_needed", "got_params_needed",
                     "latency_ms", "error"]
        df = df[[c for c in show_cols if c in df.columns]]

        def _verdict(v):
            return "✅ Réussi" if v is True else ("❌ Échoué" if v is False else "➖ Non évalué")
        if "passed" in df.columns:
            df["passed"] = df["passed"].map(_verdict)

        st.dataframe(df, use_container_width=True, hide_index=True)

        report_bytes = base64.b64decode(payload["report_b64"])
        st.download_button("⬇️ Télécharger le rapport Excel complet", data=report_bytes,
                           file_name="rapport_execution.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")