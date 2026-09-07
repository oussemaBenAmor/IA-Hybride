"""
Page « Assistant » (chat).

Aucune couleur codée en dur : l'habillage vient du CSS de app.py (variables de
thème Streamlit) → l'interface suit le mode Light / Dark choisi par l'utilisateur.

À placer dans : frontend/chat.py
(chargée par st.navigation depuis app.py — ne PAS appeler st.set_page_config ici.)
"""
import uuid

import httpx
import streamlit as st
import streamlit.components.v1 as components

FASTAPI_URL = "http://localhost:8000"
CHAT_HEIGHT = 600   # zone de conversation agrandie


def _auth_headers() -> dict:
    tok = st.session_state.get("token")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


FALLBACK_USER_MESSAGES = {
    "A": "ℹ️ Je suis spécialisé dans les services bancaires : crédits, assurance vie, carte bancaire et virements.",
    "C": "ℹ️ Je n'ai pas réussi à valider les informations fournies. Pourriez-vous reformuler votre demande ?",
    "D": "🔒 Par mesure de sécurité, ce message n'a pas pu être traité.",
    "E": "⏳ Notre service de décision est momentanément indisponible. Merci de réessayer dans quelques instants.",
}
FALLBACK_TECH_LABELS = {
    "A": "Type A — Hors périmètre", "B": "Type B — Ambiguïté (clarification)",
    "C": "Type C — Paramètres invalides", "D": "Type D — Sécurité",
    "E": "Type E — ODM indisponible",
}
RELEVANT_FIELDS = {
    "credit_immobilier":   ["montant", "duree_mois", "revenu_mensuel", "age", "apport"],
    "credit_consommation": ["montant", "duree_mois", "revenu_mensuel"],
    "assurance_vie":       ["operation", "age", "montant_initial"],
    "carte_bancaire":      ["operation", "plafond_souhaite", "type_carte"],
    "virement":            ["type_virement", "montant", "iban", "beneficiaire"],
}
PARAM_LABELS = {
    "montant": "Montant", "duree_mois": "Durée (mois)", "revenu_mensuel": "Revenu mensuel",
    "age": "Âge", "apport": "Apport", "operation": "Opération",
    "montant_initial": "Versement initial", "plafond_souhaite": "Plafond souhaité",
    "type_carte": "Type de carte", "type_virement": "Type de virement",
    "iban": "IBAN", "beneficiaire": "Bénéficiaire",
}


def _is_meaningful(v) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    return True


if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_trace" not in st.session_state:
    st.session_state.last_trace = None
if "pending" not in st.session_state:
    st.session_state.pending = None


def render_message(msg: dict):
    with st.chat_message(msg["role"]):
        st.write(msg["content"])
        ft = msg.get("fallback_type")
        if ft and ft != "B":
            note = FALLBACK_USER_MESSAGES.get(ft)
            if note:
                st.caption(note)


def call_backend(prompt: str) -> dict:
    response = httpx.post(
        f"{FASTAPI_URL}/chat",
        json={"message": prompt, "session_id": st.session_state.session_id},
        headers=_auth_headers(), timeout=180.0,
    )
    response.raise_for_status()
    return response.json()


def scroll_to_bottom():
    n = len(st.session_state.messages)
    js = """
    <script>
      const doc = window.parent.document;
      function scrollChat() {
        doc.querySelectorAll('[data-testid="stVerticalBlockBorderWrapper"]').forEach(function (el) {
          if (el.scrollHeight > el.clientHeight + 5) { el.scrollTop = el.scrollHeight; }
        });
        const a = doc.getElementById('chat-bottom');
        if (a) { a.scrollIntoView({ block: 'end' }); }
      }
      scrollChat(); setTimeout(scrollChat,100); setTimeout(scrollChat,300); setTimeout(scrollChat,600);
    </script>
    """
    components.html(f"<div style='display:none'>{n}</div>" + js, height=0)


def load_conversation(sid: str):
    try:
        r = httpx.get(f"{FASTAPI_URL}/conversations/{sid}", headers=_auth_headers(), timeout=30)
        r.raise_for_status()
        msgs = r.json()["messages"]
    except Exception as e:
        st.error(f"Impossible de charger la conversation : {e}")
        return
    st.session_state.session_id = sid
    st.session_state.messages = [
        {"role": m["role"], "content": m["content"], "fallback_type": m.get("fallback_type")}
        for m in msgs
    ]
    st.session_state.last_trace = None
    st.session_state.pending = None


def conversations_sidebar():
    st.title("💬 Mes conversations")
    if st.button("➕ Nouvelle conversation", key="new_conv", use_container_width=True, type="primary"):
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.messages = []; st.session_state.last_trace = None
        st.session_state.pending = None; st.rerun()

    try:
        r = httpx.get(f"{FASTAPI_URL}/conversations", headers=_auth_headers(), timeout=30)
        r.raise_for_status()
        convs = r.json()
    except Exception as e:
        st.info(f"Aucune conversation disponible ({e}).")
        convs = []

    if not convs:
        st.caption("Vos conversations passées apparaîtront ici.")
        return

    st.markdown("<div style='height:.4rem'></div>", unsafe_allow_html=True)
    for c in convs:
        active = (c["session_id"] == st.session_state.session_id)
        label = ("🟢 " if active else "💬 ") + c["title"]
        if st.button(label, key=f"conv_{c['session_id']}", use_container_width=True):
            load_conversation(c["session_id"]); st.rerun()


def render_xai_panel():
    st.title("Traçabilité XAI")
    if st.session_state.last_trace is None:
        st.info("Envoyez un message pour voir la trace de décision.")
        return
    trace = st.session_state.last_trace

    st.subheader("📌 Routing")
    if trace.get("case_selected"):
        st.success(f"Cas détecté : **{trace['case_selected']}**")
        if trace.get("confidence"):
            st.metric("Score de confiance", f"{trace['confidence']:.2%}")
    elif trace.get("fallback_type") == "A":
        st.error("Hors périmètre métier")

    if trace.get("top2_scores"):
        st.subheader("📊 Scores du router")
        for item in trace["top2_scores"]:
            st.progress(max(0.0, min(1.0, float(item["score"]))),
                        text=f"{item['case']} — {float(item['score']):.2%}")

    st.subheader("🔎 Paramètres extraits")
    params = trace.get("extracted_params") or {}
    case = trace.get("case_selected")
    if params and case:
        allowed = RELEVANT_FIELDS.get(case, list(params.keys()))
        shown = {k: v for k, v in params.items() if k in allowed and _is_meaningful(v)}
        if shown:
            for k, v in shown.items():
                st.write(f"- **{PARAM_LABELS.get(k, k)}** : {v}")
        else:
            st.write("—")
    elif trace.get("params_collection_needed"):
        st.info("⏳ En attente des paramètres manquants…")
    else:
        st.write("—")

    st.subheader("⚖️ Décision ODM")
    if trace.get("odm_decision"):
        decision = trace["odm_decision"].get("decision", "")
        if decision in ("ELIGIBLE", "APPROUVE"):
            st.success(f"✅ {decision}")
        elif decision in ("REFUSE",):
            st.error(f"❌ {decision}")
        else:
            st.warning(f"⚠️ {decision}")
        if trace["odm_decision"].get("mock"):
            st.caption("*(Mock ODM — IBM ODM non connecté)*")
    elif trace.get("params_collection_needed"):
        st.info("⏳ En attente…")
    else:
        st.write("—")

    st.subheader("📋 Règles déclenchées")
    rules = (trace.get("rules_fired")
             or (trace.get("odm_decision") or {}).get("rulesTriggered")
             or (trace.get("odm_decision") or {}).get("rules_triggered") or [])
    if rules:
        chips = "".join(f"<span class='xai-rule'>{r}</span>" for r in rules)
        st.markdown(f"<div class='xai-rules'>{chips}</div>", unsafe_allow_html=True)
    else:
        st.write("—")

    st.subheader("⏱️ Latences par nœud")
    latencies = trace.get("latency_ms", {})
    if latencies:
        total = sum(latencies.values())
        for node, ms in latencies.items():
            st.write(f"- **{node}** : {ms:.0f} ms")
        st.metric("Total", f"{total:.0f} ms")

    if trace.get("fallback_type"):
        st.subheader("🚨 Fallback")
        st.error(f"{FALLBACK_TECH_LABELS.get(trace['fallback_type'], trace['fallback_type'])} — "
                 f"{trace.get('fallback_reason', '')}")


# ── Layout ────────────────────────────────────────────────────────────────────
role = st.session_state.get("role", "user")
col_chat, col_side = st.columns([3, 2.3])

with col_chat:
    st.title("Conseiller Bancaire IA")
    st.caption("Posez votre question : crédits, assurance vie, carte bancaire, virements.")

    if role == "admin":
        if st.button("🔄 Nouvelle session"):
            st.session_state.session_id = str(uuid.uuid4())
            st.session_state.messages = []; st.session_state.last_trace = None
            st.session_state.pending = None; st.rerun()

    chat_box = st.container(height=CHAT_HEIGHT)
    with chat_box:
        for msg in st.session_state.messages:
            render_message(msg)

        if st.session_state.pending:
            prompt = st.session_state.pending
            st.session_state.pending = None
            with st.chat_message("assistant"):
                with st.spinner("Traitement en cours…"):
                    ft = None
                    try:
                        data = call_backend(prompt)
                        bot_response = data.get("response", "Aucune réponse.")
                        ft = data.get("fallback_type")
                        st.session_state.last_trace = data
                    except httpx.ConnectError:
                        bot_response = "❌ Impossible de joindre le backend FastAPI (localhost:8000)"
                    except httpx.HTTPStatusError as e:
                        bot_response = ("🔒 Session expirée, veuillez vous reconnecter."
                                        if e.response.status_code == 401 else f"❌ Erreur : {e}")
                    except Exception as e:
                        bot_response = f"❌ Erreur : {e}"
                st.write(bot_response)
                if ft and ft != "B":
                    note = FALLBACK_USER_MESSAGES.get(ft)
                    if note:
                        st.caption(note)

            st.session_state.messages.append({"role": "assistant", "content": bot_response, "fallback_type": ft})
            st.rerun()

        st.markdown("<div id='chat-bottom'></div>", unsafe_allow_html=True)

with col_side:
    if role == "admin":
        render_xai_panel()
    else:
        conversations_sidebar()

scroll_to_bottom()

user_input = st.chat_input("Posez votre question bancaire...")
if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.pending = user_input
    st.rerun()