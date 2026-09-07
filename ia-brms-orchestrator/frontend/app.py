"""
Point d'entrée du frontend Streamlit.

Le thème (Light / Dark / System) est celui choisi par l'utilisateur dans le menu
Streamlit : ce fichier n'impose AUCUNE couleur en dur. Tout le CSS s'appuie sur
les variables de thème de Streamlit (--background-color, --secondary-background-color,
--text-color, --primary-color), donc l'interface suit automatiquement le mode choisi.

app.py, chat.py et dashboard.py sont TOUS à la racine de frontend/ (pas de pages/).

Lancement :  streamlit run app.py   —   Streamlit ≥ 1.39
À placer dans : frontend/app.py
"""
import streamlit as st
import httpx

FASTAPI_URL = "http://localhost:8000"

st.set_page_config(page_title="Conseiller Bancaire IA", page_icon="🏦", layout="wide")

_authed = bool(st.session_state.get("token"))

# Style « colonne = carte » appliqué uniquement une fois connecté (page chat).
_card_cols_css = """
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]{
        background:var(--secondary-background-color);
        border:1px solid color-mix(in srgb, var(--text-color) 14%, transparent);
        border-radius:20px; padding:1.3rem 1.6rem 1.2rem;
        box-shadow:0 24px 48px -34px color-mix(in srgb, var(--text-color) 55%, transparent);
    }
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child{
        border-top:3px solid var(--primary-color);
    }
""" if _authed else ""

st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Sora:wght@600;700;800&display=swap');

    html, body, [class*="css"]{ font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }

    /* Léger halo teinté avec la couleur primaire — s'adapte au thème. */
    .stApp{
        background:
            radial-gradient(1100px 520px at 10% -12%, color-mix(in srgb, var(--primary-color) 10%, transparent) 0%, transparent 55%),
            var(--background-color);
    }
    .block-container{ padding-top:1.4rem; padding-bottom:6rem; max-width:1460px; }
    [data-testid="stDecoration"]{ display:none; }
    [data-testid="stHeader"]{ background:transparent; }
    footer{ display:none; }
    h1{ font-family:'Sora',sans-serif; font-weight:800 !important; letter-spacing:-.03em; margin-bottom:.15rem !important; }

    /* ---- Badge nom d'utilisateur + bouton déconnexion (haut droite, espacés) ---- */
    .user-badge{
        position:fixed; top:.78rem; right:12rem; z-index:1000000;
        font-weight:700; font-size:.86rem; color:var(--text-color);
        background:var(--secondary-background-color);
        border:1px solid color-mix(in srgb, var(--text-color) 16%, transparent);
        border-radius:999px; padding:.3rem .9rem;
        box-shadow:0 6px 18px -10px color-mix(in srgb, var(--text-color) 55%, transparent);
        white-space:nowrap;
    }
    .st-key-logout_btn{ position:fixed; top:.6rem; right:8.2rem; z-index:1000000; }
    .st-key-logout_btn button{
        width:44px !important; height:38px !important; padding:0 !important;
        border-radius:999px !important;
        border:1px solid color-mix(in srgb, var(--text-color) 16%, transparent) !important;
        background-color:var(--secondary-background-color) !important;
        background-image:url("data:image/png;base64,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") !important;
        background-repeat:no-repeat !important; background-position:center !important;
        background-size:20px 20px !important;
        color:transparent !important; font-size:0 !important;
        box-shadow:0 6px 18px -10px color-mix(in srgb, var(--text-color) 55%, transparent) !important;
    }
    .st-key-logout_btn button:hover{ border-color:#ff5a6a !important; }

    /* ---- Barre latérale (admin) : en-tête ---- */
    [data-testid="stSidebarNav"]::before{
        content:"🏦  BRMS Console";
        display:block; font-family:'Sora',sans-serif; font-weight:800;
        font-size:1.02rem; color:var(--text-color);
        padding:1.1rem 1rem .6rem 1.2rem; letter-spacing:-.02em;
    }
    [data-testid="stSidebarNav"] a{ border-radius:10px; margin:.15rem .6rem; padding:.5rem .7rem !important; }
    [data-testid="stSidebarNav"] a:hover{ background:color-mix(in srgb, var(--primary-color) 16%, transparent); }
    [data-testid="stSidebarNav"] a[aria-current="page"]{ background:color-mix(in srgb, var(--primary-color) 24%, transparent) !important; }
    [data-testid="stSidebarNav"] a span{ font-weight:600; font-size:.92rem; }

    /* ---- Page de login (formulaire centré) ---- */
    .login-hero{ text-align:center; margin:6vh 0 .2rem; }
    .login-hero h1{ font-size:2.15rem !important; }
    .login-hero p{ color:color-mix(in srgb, var(--text-color) 65%, transparent); font-size:.95rem; margin-top:.1rem; }
    [data-testid="stForm"]{
        max-width:440px; margin:0 auto;
        background:var(--secondary-background-color);
        border:1px solid color-mix(in srgb, var(--text-color) 14%, transparent);
        border-radius:20px; padding:1.6rem 1.7rem !important;
        box-shadow:0 30px 60px -34px color-mix(in srgb, var(--text-color) 60%, transparent);
    }
    .stTabs{ max-width:440px; margin:0 auto; }
    .stTabs [data-baseweb="tab-list"]{ justify-content:center; gap:1.4rem; }
    .stTabs [data-baseweb="tab"]{ font-weight:600; }

    """
    + _card_cols_css +
    """

    /* ---- Chat + bulles ---- */
    [data-testid="stVerticalBlockBorderWrapper"]{ border-radius:16px !important; }
    [data-testid="stChatMessage"]{ background:transparent; padding:.15rem 0; }
    [data-testid="stChatMessageContent"]{ border-radius:14px; padding:.8rem 1.05rem !important; line-height:1.55; }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) [data-testid="stChatMessageContent"]{
        background:color-mix(in srgb, var(--primary-color) 16%, var(--secondary-background-color));
        border:1px solid color-mix(in srgb, var(--primary-color) 30%, transparent);
    }
    [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) [data-testid="stChatMessageContent"]{
        background:var(--secondary-background-color);
        border:1px solid color-mix(in srgb, var(--text-color) 12%, transparent);
    }

    /* ---- Boutons + saisie ---- */
    .stButton > button{ border-radius:12px; font-weight:600; transition:all .15s ease; }
    .stButton > button:hover{ transform:translateY(-1px); }
    [data-testid="stChatInput"]{ max-width:720px; margin-left:auto; margin-right:auto; border-radius:16px; }

    /* ---- Puces des règles (XAI) ---- */
    .xai-rules{ display:flex; flex-wrap:wrap; gap:.32rem; margin:.15rem 0 .1rem; }
    .xai-rule{
        display:inline-flex; align-items:center;
        background:color-mix(in srgb, var(--primary-color) 18%, transparent);
        color:var(--text-color);
        border:1px solid color-mix(in srgb, var(--primary-color) 34%, transparent);
        border-radius:999px; padding:.13rem .58rem; font-size:.74rem; font-weight:600;
        font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; line-height:1.45; white-space:nowrap;
    }
    .xai-rule::before{ content:"✓"; color:var(--primary-color); font-weight:800; margin-right:.32rem; }

    /* ---- Titres de section (XAI) ---- */
    [data-testid="stColumn"]:last-child h3{
        font-family:'Sora',sans-serif; font-size:.72rem !important; font-weight:700 !important;
        text-transform:uppercase; letter-spacing:.05em; color:var(--primary-color) !important;
        margin-top:1.2rem !important; margin-bottom:.4rem !important;
        padding-bottom:.28rem; border-bottom:1px solid color-mix(in srgb, var(--text-color) 12%, transparent);
    }
    </style>
    """,
    unsafe_allow_html=True,
    )


for k, v in {"token": None, "role": None, "username": None}.items():
    st.session_state.setdefault(k, v)


def do_logout():
    for k in ("token", "role", "username", "messages", "last_trace", "pending", "session_id"):
        st.session_state.pop(k, None)
    st.rerun()


def login_page():
    st.markdown(
        "<div class='login-hero'><h1>🏦 Conseiller Bancaire IA</h1>"
        "<p>Connectez-vous pour accéder à votre assistant bancaire intelligent.</p></div>",
        unsafe_allow_html=True,
    )

    tab_login, tab_register = st.tabs(["🔐  Se connecter", "✨  Créer un compte"])

    with tab_login:
        with st.form("form_login", clear_on_submit=False):
            u = st.text_input("Identifiant", placeholder="votre identifiant")
            p = st.text_input("Mot de passe", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Se connecter", use_container_width=True, type="primary")
        if submitted:
            try:
                r = httpx.post(f"{FASTAPI_URL}/auth/login",
                               json={"username": u, "password": p}, timeout=30)
                r.raise_for_status()
                data = r.json()
                st.session_state.token = data["token"]; st.session_state.role = data["role"]
                st.session_state.username = data["username"]; st.rerun()
            except httpx.HTTPStatusError:
                st.error("Identifiants incorrects.")
            except Exception as e:
                st.error(f"Impossible de joindre le backend : {e}")

    with tab_register:
        with st.form("form_register", clear_on_submit=False):
            u2 = st.text_input("Choisissez un identifiant", placeholder="min. 3 caractères")
            p2 = st.text_input("Choisissez un mot de passe", type="password", placeholder="min. 6 caractères")
            submitted2 = st.form_submit_button("Créer le compte", use_container_width=True, type="primary")
        if submitted2:
            try:
                r = httpx.post(f"{FASTAPI_URL}/auth/register",
                               json={"username": u2, "password": p2}, timeout=30)
                r.raise_for_status()
                data = r.json()
                st.session_state.token = data["token"]; st.session_state.role = data["role"]
                st.session_state.username = data["username"]; st.rerun()
            except httpx.HTTPStatusError as e:
                try:
                    st.error(e.response.json().get("detail", "Erreur lors de la création."))
                except Exception:
                    st.error("Erreur lors de la création du compte.")
            except Exception as e:
                st.error(f"Impossible de joindre le backend : {e}")


if not st.session_state.get("token"):
    nav = st.navigation([st.Page(login_page, title="Connexion")], position="hidden")
    nav.run()
else:
    username = st.session_state.get("username", "")
    st.markdown(f"<div class='user-badge'>👤 {username}</div>", unsafe_allow_html=True)
    if st.button(" ", key="logout_btn", help="Se déconnecter"):
        do_logout()

    role = st.session_state.get("role")
    pages = [st.Page("chat.py", title="Assistant", icon="💬", default=True)]
    if role == "admin":
        pages.append(st.Page("dashboard.py", title="Dashboard", icon="📊"))
        nav = st.navigation(pages)
    else:
        nav = st.navigation(pages, position="hidden")
    nav.run()