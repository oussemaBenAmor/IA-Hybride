import streamlit as st
import streamlit.components.v1 as components
import httpx
import uuid

FASTAPI_URL = "http://localhost:8000"

st.set_page_config(
    page_title="Orchestration IA + BRMS",
    page_icon="🏦",
    layout="wide"
)

# ── Barre de saisie plus étroite (ajuste max-width à ta convenance) ───────────
st.markdown(
    """
    <style>
    [data-testid="stChatInput"] {
        max-width: 650px;
        margin-left: auto;
        margin-right: auto;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ── Messages CONVIVIAUX affichés à l'utilisateur (plus de codes techniques) ───
# Le détail technique (Type A/B/C/D/E + raison) reste visible dans le panneau XAI.
FALLBACK_USER_MESSAGES = {
    "A": "ℹ️ Je suis spécialisé dans les services bancaires : crédits, assurance vie, carte bancaire et virements.",
    "C": "ℹ️ Je n'ai pas réussi à valider les informations fournies. Pourriez-vous reformuler votre demande ?",
    "D": "🔒 Par mesure de sécurité, ce message n'a pas pu être traité.",
    "E": "⏳ Notre service de décision est momentanément indisponible. Merci de réessayer dans quelques instants.",
}

# Labels techniques — utilisés UNIQUEMENT dans le panneau XAI
FALLBACK_TECH_LABELS = {
    "A": "Type A — Hors périmètre",
    "B": "Type B — Ambiguïté (clarification)",
    "C": "Type C — Paramètres invalides",
    "D": "Type D — Sécurité",
    "E": "Type E — ODM indisponible",
}

# ── Champs PERTINENTS par cas (utilisés par les règles ODM ou fournis par l'user) ──
# On n'affiche dans le panneau XAI que les paramètres qui ont un sens pour le cas :
# soit utilisés dans les règles, soit fournis explicitement. Les champs optionnels
# jamais renseignés ni utilisés (ex: motif en conso, versement_mensuel en assurance)
# ne sont PAS affichés.
RELEVANT_FIELDS = {
    "credit_immobilier":   ["montant", "duree_mois", "revenu_mensuel", "age", "apport"],
    "credit_consommation": ["montant", "duree_mois", "revenu_mensuel"],
    "assurance_vie":       ["operation", "age", "montant_initial"],
    "carte_bancaire":      ["operation", "plafond_souhaite", "type_carte"],
    "virement":            ["type_virement", "montant", "iban", "beneficiaire"],
}

# Libellés lisibles pour le panneau (optionnel : sinon on affiche la clé brute)
PARAM_LABELS = {
    "montant":           "Montant",
    "duree_mois":        "Durée (mois)",
    "revenu_mensuel":    "Revenu mensuel",
    "age":               "Âge",
    "apport":            "Apport",
    "operation":         "Opération",
    "montant_initial":   "Versement initial",
    "plafond_souhaite":  "Plafond souhaité",
    "type_carte":        "Type de carte",
    "type_virement":     "Type de virement",
    "iban":              "IBAN",
    "beneficiaire":      "Bénéficiaire",
}


def _is_meaningful(v) -> bool:
    """Une valeur est affichable si elle a été réellement renseignée."""
    if v is None:
        return False
    if isinstance(v, str) and v.strip() == "":
        return False
    return True


# Hauteur (px) de la zone de conversation défilante
CHAT_HEIGHT = 480

# ── Session state ──
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())
if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_trace" not in st.session_state:
    st.session_state.last_trace = None
if "pending" not in st.session_state:
    st.session_state.pending = None


def render_message(msg: dict):
    """Affiche un message + une note conviviale si le tour avait un fallback."""
    with st.chat_message(msg["role"]):
        st.write(msg["content"])
        ft = msg.get("fallback_type")
        if ft and ft != "B":   # B = clarification, déjà naturelle dans le texte
            note = FALLBACK_USER_MESSAGES.get(ft)
            if note:
                st.caption(note)


def call_backend(prompt: str) -> dict:
    response = httpx.post(
        f"{FASTAPI_URL}/chat",
        json={"message": prompt, "session_id": st.session_state.session_id},
        timeout=180.0,
    )
    response.raise_for_status()
    return response.json()


def scroll_to_bottom():
    """Fait défiler la zone de chat jusqu'au dernier message."""
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
      scrollChat();
      setTimeout(scrollChat, 100);
      setTimeout(scrollChat, 300);
      setTimeout(scrollChat, 600);
    </script>
    """
    components.html(f"<div style='display:none'>{n}</div>" + js, height=0)


# ── Layout ──
col_chat, col_xai = st.columns([3, 2])

# ════════════════════════════════
# COLONNE GAUCHE — Chat
# ════════════════════════════════
with col_chat:
    st.title("Conseiller Bancaire IA")
    st.caption(f"Session : `{st.session_state.session_id[:8]}...`")

    if st.button("🔄 Nouvelle session"):
        st.session_state.session_id = str(uuid.uuid4())
        st.session_state.messages   = []
        st.session_state.last_trace = None
        st.session_state.pending    = None
        st.rerun()

    # Zone de conversation défilante (hauteur fixe → l'input reste juste dessous)
    chat_box = st.container(height=CHAT_HEIGHT)
    with chat_box:
        for msg in st.session_state.messages:
            render_message(msg)

        # Traitement du message en attente (affichage progressif)
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
                    except Exception as e:
                        bot_response = f"❌ Erreur : {e}"
                st.write(bot_response)
                if ft and ft != "B":
                    note = FALLBACK_USER_MESSAGES.get(ft)
                    if note:
                        st.caption(note)

            st.session_state.messages.append({
                "role": "assistant",
                "content": bot_response,
                "fallback_type": ft,
            })

        # Ancre de défilement (cible du scroll auto)
        st.markdown("<div id='chat-bottom'></div>", unsafe_allow_html=True)

# ════════════════════════════════
# COLONNE DROITE — Panneau XAI (détails TECHNIQUES, y compris fallbacks)
# ════════════════════════════════
with col_xai:
    st.title("Traçabilité XAI")

    if st.session_state.last_trace is None:
        st.info("Envoyez un message pour voir la trace de décision.")
    else:
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
                st.progress(
                    max(0.0, min(1.0, float(item["score"]))),
                    text=f"{item['case']} — {float(item['score']):.2%}"
                )

        # ── Paramètres extraits : on lit extracted_params (vraie source) et on
        #    n'affiche que les champs PERTINENTS au cas ET réellement renseignés. ──
        st.subheader("🔎 Paramètres extraits")
        params = trace.get("extracted_params") or {}
        case = trace.get("case_selected")

        if params and case:
            allowed = RELEVANT_FIELDS.get(case, list(params.keys()))
            shown = {
                k: v for k, v in params.items()
                if k in allowed and _is_meaningful(v)
            }
            if shown:
                for k, v in shown.items():
                    label = PARAM_LABELS.get(k, k)
                    st.write(f"- **{label}** : {v}")
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
        rules = (
                trace.get("rules_fired")
                or (trace.get("odm_decision") or {}).get("rulesTriggered")
                or (trace.get("odm_decision") or {}).get("rules_triggered")
                or []
        )
        if rules:
            for rule in rules:
                st.write(f"✓ `{rule}`")
        else:
            st.write("—")

        st.subheader("⏱️ Latences par nœud")
        latencies = trace.get("latency_ms", {})
        if latencies:
            total = sum(latencies.values())
            for node, ms in latencies.items():
                st.write(f"- **{node}** : {ms:.0f} ms")
            st.metric("Total", f"{total:.0f} ms")

        # Fallback TECHNIQUE — seul endroit où les types A/B/C/D/E apparaissent
        if trace.get("fallback_type"):
            st.subheader("🚨 Fallback")
            st.error(
                f"{FALLBACK_TECH_LABELS.get(trace['fallback_type'], trace['fallback_type'])} — "
                f"{trace.get('fallback_reason', '')}"
            )

# Défilement automatique vers le dernier message
scroll_to_bottom()

# ════════════════════════════════
# INPUT — niveau racine → épinglé en bas (rétréci par le CSS plus haut)
# ════════════════════════════════
user_input = st.chat_input("Posez votre question bancaire...")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    st.session_state.pending = user_input
    st.rerun()