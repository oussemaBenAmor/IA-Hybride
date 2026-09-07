import os
import io
import json
import base64

# Génère des identifiants uniques pour les conversations
import uuid
import logging
import traceback

# Permet de définir du code exécuté au démarrage et à l'arrêt de FastAPI
from contextlib import asynccontextmanager

# Crée le serveur FastAPI et permet de lever des erreurs HTTP
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File

# Autorise les appels HTTP provenant d'autres applications (frontend)
from fastapi.middleware.cors import CORSMiddleware

# Permet de renvoyer un fichier binaire (modèle Excel) en téléchargement
from fastapi.responses import StreamingResponse

# pour définir les formats d'entrée/sortie
from pydantic import BaseModel

# pour tolérer les fautes
from rapidfuzz import fuzz

from app.config import settings
from app.core.logging import setup_logging
from app.core.llm import generate_text, LLMError
from app.db.postgres import init_db
from app.db.vector_store import seed_cases
from app.graph.graph_builder import get_graph
from app.graph.nodes.extraction import extract_params_with_llm
from app.graph.nodes.security import detect_attack  # réutilisé ici pour screener TOUS les tours
from app.graph.nodes.router import VALID_CASES

# ── Authentification (JWT) ────────────────────────────────────────────────────
from app.db.auth import (
    authenticate, create_user, create_access_token, ensure_default_admin,
)
from app.core.auth import get_current_user, require_admin

# ── Conversations et messages persistés ───────────────────────────────────────
from app.db.conversations import (
    ensure_conversation, save_message,
    list_conversations, get_conversation_messages,
)

# ── Exécution de scénarios de test (upload Excel) ─────────────────────────────
from app.services.scenario_runner import run_scenarios, build_template_xlsx

logger = logging.getLogger("api")

# Données reçues dans une requête POST /chat
class ChatRequest(BaseModel):
    message:    str                 # Message envoyé par l'utilisateur
    session_id: str | None = None   # Identifiant de conversation

# Structure de la réponse renvoyée par l'API après traitement de la demande
class ChatResponse(BaseModel):
    session_id:               str
    response:                 str    # la réponse à afficher
    case_selected:            str | None
    confidence:               float | None
    fallback_type:            str | None
    rules_fired:              list | None
    odm_decision:             dict | None
    extracted_params:         dict | None = None
    latency_ms:               dict
    clarification_needed:     bool
    clarification_question:   str | None
    params_collection_needed: bool
    params_question:          str | None


# ── Modèles pour l'authentification ───────────────────────────────────────────
class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    token:    str
    username: str
    role:     str


# Instance globale du workflow LangGraph, créée une seule fois au démarrage
graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graph
    setup_logging()      # configure les logs

    # Active le tracing LangSmith pour visualiser les exécutions LangGraph
    if settings.langsmith_tracing and settings.langsmith_api_key:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGCHAIN_API_KEY"]    = settings.langsmith_api_key
        os.environ["LANGCHAIN_PROJECT"]    = settings.langsmith_project
        os.environ["LANGCHAIN_ENDPOINT"]   = settings.langsmith_endpoint
        os.environ["LANGSMITH_API_KEY"]    = settings.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"]    = settings.langsmith_project
        os.environ["LANGSMITH_ENDPOINT"]   = settings.langsmith_endpoint
        logger.info(
            "LangSmith tracing activé (projet=%s, endpoint=%s)",
            settings.langsmith_project,
            settings.langsmith_endpoint,
        )



    # Crée les tables PostgreSQL nécessaires si elles n'existent pas
    init_db()

    # Crée un compte administrateur par défaut au premier lancement (admin / admin123)
    ensure_default_admin()

    try:

        # Met à jour les cas métier dans pgvector (embeddings sémantiques)
        seed_cases()
    except Exception as e:
        logger.warning("seed_cases ignoré (Ollama injoignable ?) : %s", e)

    # Construit le workflow LangGraph une seule fois
    graph = get_graph()
    logger.info("FastAPI démarré — LangGraph prêt (Ollama=%s)", settings.ollama_base_url)
    yield



# Crée l'application FastAPI et exécute lifespan() au démarrage
app = FastAPI(title="Orchestration IA + BRMS", version="4.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,    # autorise le frontend (Streamlit) à appeler l'API
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mots-clés utilisés pour résoudre rapidement une ambiguïté de routage.
LEXICAL_HINTS = {
    "credit_immobilier":   ("immobilier", "immo", "maison", "appartement", "logement", "résidence", "residence"),
    "credit_consommation": ("consommation", "conso", "personnel", "voiture", "auto", "travaux", "loisir"),
    "carte_bancaire":      ("carte", "visa", "mastercard", "opposition", "plafond"),
    "virement":            ("virement", "transfert", "iban", "bénéficiaire", "beneficiaire"),
    "assurance_vie":       ("assurance", "épargne", "epargne", "placement", "retraite"),
}


GREETING_WORDS = (
    "bonjour", "salut", "bonsoir", "hello", "hi", "hey", "coucou",
    "bonne journée", "bonne journee", "ça va", "ca va",
)
THANKS_WORDS  = ("merci", "thanks", "thank you")
GOODBYE_WORDS = ("au revoir", "à bientôt", "a bientot", "bye", "adieu", "bonne soirée", "bonne soiree")

GREETING_REPLY = (
    "Bonjour ! 👋 Je suis votre conseiller bancaire virtuel. "
    "Je peux vous aider sur : le crédit immobilier, le crédit à la consommation, "
    "l'assurance vie, la carte bancaire et les virements. Que puis-je faire pour vous ?"
)
THANKS_REPLY  = "Avec plaisir ! N'hésitez pas si vous avez d'autres questions bancaires. 😊"
GOODBYE_REPLY = "Au revoir, et merci de votre visite ! À bientôt. 👋"


# Expressions permettant de détecter une question sur les services disponibles
CAPABILITY_TRIGGERS = (
    "quels services", "quels sont les services", "que proposez", "que proposes",
    "que faites-vous", "que faites vous", "que fais-tu", "que fais tu",
    "qu'est-ce que tu peux faire", "qu'est ce que tu peux faire",
    "quelles fonctionnalités", "quelles fonctionnalites", "tes services",
    "vos services", "comment peux-tu m'aider", "comment peux tu m'aider",
    "tu sers à quoi", "tu sers a quoi", "à quoi tu sers", "a quoi tu sers",
    "quelles sont vos offres", "qu'offrez-vous", "qu'offrez vous",
    "que peux-tu faire", "que peux tu faire", "tu fais quoi", "tu gères quoi",
    "tu geres quoi",
)

# Réponse détaillée présentant tous les services gérés par le chatbot
CAPABILITIES_REPLY = (
    "Je suis votre conseiller bancaire virtuel. Voici tout ce que je peux traiter :\n\n"
    "🏠 **Crédit immobilier** — financer l'achat d'une maison, d'un appartement, "
    "un investissement locatif ou une construction, et estimer votre capacité d'emprunt.\n\n"
    "🚗 **Crédit à la consommation** — prêt personnel pour une voiture, des travaux, "
    "des loisirs ou un besoin ponctuel.\n\n"
    "📈 **Assurance vie** — souscription d'un contrat, versement libre ou rachat "
    "(partiel ou total), pour épargner ou préparer votre retraite.\n\n"
    "💳 **Carte bancaire** — faire opposition après une perte ou un vol, modifier "
    "votre plafond, renouveler ou débloquer votre carte.\n\n"
    "💸 **Virement** — envoyer de l'argent (SEPA, instantané ou international) "
    "vers un bénéficiaire.\n\n"
    "Que souhaitez-vous faire ?"
)

#Détecte une question sur les services offerts (méta, pas un cas métier).
def _is_capability_question(text: str) -> bool:
    #transforme tout en minuscules et enlève les espaces et signes de ponctuation inutiles
    t = text.lower().strip(" !.?,")
    if len(t) > 80:
        return False

    # Vérifie si un des mots-clés de CAPABILITY_TRIGGERS  apparaît dans le message utilisateur.
    return any(trigger in t for trigger in CAPABILITY_TRIGGERS)

_FUZZY_THRESHOLD = 75    # seuil de ressemblance pour tolérer les fautes de frappe

# Renvoie la réponse de small talk appropriée, ou None si ce n'en est pas.
def _match_smalltalk(text: str) -> str | None:

    t = text.lower().strip(" !.?,")
    if not t or len(t) > 40:          # un vrai message métier est plus long
        return None

    # On récupère le premier mot du message.
    first = t.split()[0]

    def hits(words: tuple) -> bool:
        for w in words:

            # correspondance exacte ou début de phrase
            if t == w or t.startswith(w + " ") or t.startswith(w + "!"):
                return True
            # tolérance typo sur le premier mot ("bonjouur" → "bonjour")
            if fuzz.ratio(first, w) >= _FUZZY_THRESHOLD:
                return True
        return False

    if hits(GOODBYE_WORDS):
        return GOODBYE_REPLY
    if hits(THANKS_WORDS):
        return THANKS_REPLY
    if hits(GREETING_WORDS):
        return GREETING_REPLY
    return None



# Crée un nouvel état de conversation avec des valeurs par défaut.
# Les anciennes données du checkpointer LangGraph sont réinitialisées
# pour éviter de réutiliser des informations d'une précédente demande.
def _fresh_state(session_id: str, user_reply: str) -> dict:
    return {
        "session_id":               session_id,
        "input_raw":                user_reply,
        "input_corrected":          None,
        "is_blocked":               False,
        "security_pattern":         None,
        "attack_type":              None,
        "fallback_type":            None,
        "fallback_reason":          None,
        "case_selected":            None,
        "confidence":               None,
        "top2_scores":              None,
        "extracted_params":         None,
        "validation_errors":        None,
        "retry_count":              0,
        "clarification_needed":     False,
        "clarification_question":   None,
        "missing_params":           [],
        "params_question":          None,
        "params_collection_needed": False,
        "odm_payload":              None,
        "odm_decision":             None,
        "rules_fired":              None,
        "response_text":            None,
        "latency_ms":               {},
        "_skip_to":                 None,
    }


# Nettoie les anciens résultats avant de poursuivre une conversation existante.
_RESET_OUTPUTS = {
    "is_blocked":       False,
    "security_pattern": None,
    "attack_type":      None,
    "fallback_type":    None,
    "fallback_reason":  None,
    "odm_decision":     None,
    "odm_payload":      None,
    "rules_fired":      None,
    "response_text":    None,
    "validation_errors": None,
}



# Vérifie si la réponse utilisateur complète la demande en cours
# ou si elle correspond à une nouvelle demande indépendante.
def _is_new_request(user_reply: str, question_asked: str, missing_fields: list) -> bool:

    # Création du prompt envoyé au LLM pour classifier la réponse.
    # Le modèle doit choisir entre :
    # A -> l'utilisateur répond à la question posée
    # B -> l'utilisateur démarre une nouvelle demande


    prompt = f"""Le chatbot a demandé : "{question_asked}"
Champs attendus : {missing_fields}
L'utilisateur a répondu : "{user_reply}"

La réponse est-elle :
A) Une réponse directe à la question (il fournit la valeur demandée)
B) Une nouvelle demande différente (il change de sujet)

Réponds UNIQUEMENT par A ou B."""
    try:

        # Appel du LLM utilisé comme classifieur
        ans = generate_text(prompt, task="classifier", temperature=0.0, timeout=30.0).upper()
        return ans.startswith("B") # l'utilisateur a changé de sujet
    except LLMError as e:
        logger.warning("intent_check KO (%s) → on suppose réponse directe", e)
        return False

# Vérifie si le nouveau message continue la demande actuelle
# ou correspond à un nouveau sujet.
def _is_followup(user_reply: str, case_selected: str, known_params: dict) -> bool:
    prompt = f"""Demande bancaire en cours : cas = "{case_selected}", paramètres connus = {json.dumps(known_params, ensure_ascii=False)}.
Nouveau message de l'utilisateur : "{user_reply}"

Ce message est-il :
A) La suite de cette demande (précise/modifie un paramètre — durée, montant, revenu… — ou pose une question sur ce même dossier)
B) Une nouvelle demande sans rapport (autre produit bancaire, autre sujet)

Réponds UNIQUEMENT par A ou B."""
    try:

        # Utilisation du LLM comme classifieur de contexte
        ans = generate_text(prompt, task="classifier", temperature=0.0, timeout=30.0).upper()
        return ans.startswith("A")
    except LLMError as e:
        logger.warning("followup_check KO (%s) → on suppose nouvelle demande", e)
        return False


# ── Helper de finalisation ────────────────────────────────────────────────────
# Persiste la réponse de l'assistant AVANT de renvoyer la ChatResponse.
# Centralise l'écriture pour que tous les chemins (small talk, capacités,
# clarification, pipeline complet) sauvegardent bien le message assistant.
def _finalize(session_id: str, response_text: str, fallback_type: str | None,
              **kwargs) -> ChatResponse:
    try:
        save_message(session_id, "assistant", response_text or "", fallback_type)
    except Exception:
        logger.exception("échec de la persistance du message assistant")
    return ChatResponse(
        session_id=session_id,
        response=response_text or "",
        fallback_type=fallback_type,
        **kwargs,
    )


# ══════════════════════════════════════════════════════════════════════════════
# AUTHENTIFICATION
# ══════════════════════════════════════════════════════════════════════════════
@app.post("/auth/register", response_model=TokenResponse)
def register(req: RegisterRequest):
    if len(req.username) < 3 or len(req.password) < 6:
        raise HTTPException(400, "Identifiant (≥3 caractères) ou mot de passe (≥6) trop court")
    try:
        user = create_user(req.username, req.password, role="user")
    except ValueError as e:
        raise HTTPException(409, str(e))
    token = create_access_token(user["id"], user["username"], user["role"])
    return TokenResponse(token=token, username=user["username"], role=user["role"])


@app.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest):
    user = authenticate(req.username, req.password)
    if not user:
        raise HTTPException(401, "Identifiants incorrects")
    token = create_access_token(user["id"], user["username"], user["role"])
    return TokenResponse(token=token, username=user["username"], role=user["role"])


@app.get("/auth/me")
def me(user: dict = Depends(get_current_user)):
    return user


# ══════════════════════════════════════════════════════════════════════════════
# CONVERSATIONS (barre latérale utilisateur)
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/conversations")
def get_conversations(user: dict = Depends(get_current_user)):
    return list_conversations(user["id"])


@app.get("/conversations/{sid}")
def get_conversation(sid: str, user: dict = Depends(get_current_user)):
    msgs = get_conversation_messages(sid, user["id"])
    if msgs is None:
        # None = conversation inexistante OU n'appartenant pas à l'utilisateur
        raise HTTPException(404, "Conversation introuvable")
    return {"session_id": sid, "messages": msgs}


# Endpoint de vérification de disponibilité de l'API.
@app.get("/health")
def health():
    return {"status": "ok", "env": settings.app_env}



# Point d'entrée principal du chatbot.
# Analyse la demande utilisateur, gère le contexte et exécute le workflow LangGraph.
@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, user: dict = Depends(get_current_user)):

    # Récupère ou crée un identifiant de conversation
    # utilisé par LangGraph pour conserver le contexte.
    session_id = request.session_id or str(uuid.uuid4())

    #permet au checkpointer LangGraph de restaurer l'état
    config     = {"configurable": {"thread_id": session_id}}
    user_reply = request.message

    # Rattache la conversation à l'utilisateur (idempotent : titre + user_id posés
    # une seule fois, au premier message) et persiste le message utilisateur.
    ensure_conversation(session_id, user["id"], user_reply)
    save_message(session_id, "user", user_reply)

    try:

        # Charge l'état précédent de la conversation depuis le checkpointer LangGraph
        # grâce au thread_id afin de conserver le contexte utilisateur entre les requêtes.
        current_state = graph.get_state(config)
        prev_values   = current_state.values if current_state else {}
    except Exception:
        prev_values = {}

    # Vérifie si le message contient une tentative d'attaque sur tous les tours .
    # Les messages suspects sont bloqués avant le traitement métier.
    if detect_attack(user_reply):
        logger.warning("attaque détectée sur le tour courant → blocage")
        initial_state = _fresh_state(session_id, user_reply)

    # Traite les messages sociaux directement sans lancer le workflow.
    # Une collecte de données en cours reste prioritaire.
    elif (smalltalk := _match_smalltalk(user_reply)) is not None \
            and not prev_values.get("clarification_needed") \
            and not prev_values.get("params_collection_needed"):
        logger.info("small talk détecté → réponse directe")
        return _finalize(
            session_id, smalltalk, None,
            case_selected=None, confidence=None,
            rules_fired=None, odm_decision=None, latency_ms={},
            clarification_needed=False, clarification_question=None,
            params_collection_needed=False, params_question=None,
        )

    # Répond directement aux questions sur les fonctionnalités du chatbot.
    # On ne court-circuite PAS une collecte en cours (clarification / params).
    elif _is_capability_question(user_reply) \
            and not prev_values.get("clarification_needed") \
            and not prev_values.get("params_collection_needed"):
        logger.info("question capacités détectée → réponse directe")
        return _finalize(
            session_id, CAPABILITIES_REPLY, None,
            case_selected=None, confidence=None,
            rules_fired=None, odm_decision=None, latency_ms={},
            clarification_needed=False, clarification_question=None,
            params_collection_needed=False, params_question=None,
        )

    # ── CAS 1 — réponse à une clarification de cas (résolution lexicale) ─────
    # Le chatbot avait plusieurs cas possibles et attendait une précision.
    elif prev_values.get("clarification_needed"):
        # Récupère les deux meilleurs cas proposés précédemment par le routeur.
        top2      = prev_values.get("top2_scores", []) or []

        # Garde uniquement les cas qui existent réellement dans l'application.
        # Cela évite de traiter un cas inconnu.
        presented = [item["case"] for item in top2 if item.get("case") in VALID_CASES]

        # Convertit la réponse utilisateur en minuscules pour faciliter la comparaison.
        reply_low = user_reply.lower()

        # Recherche des mots-clés associés aux cas possibles.
        hits = [c for c in presented if any(kw in reply_low for kw in LEXICAL_HINTS.get(c, ()))]

        # Si un seul cas correspond, on le sélectionne Sinon on garde None car la réponse est ambiguë.
        forced_case = hits[0] if len(hits) == 1 else None

        if forced_case:
            logger.info("clarification résolue → %s", forced_case)

            # Combine la première demande utilisateur avec la précision apportée.
            combined = f'{prev_values.get("input_raw", "")} {user_reply}'.strip()
            initial_state = {
                **prev_values,
                **_RESET_OUTPUTS,
                "input_raw":                combined,
                "input_corrected":          None,
                "case_selected":            forced_case,
                "confidence":               1.0,
                "clarification_needed":     False,
                "clarification_question":   None,
                "params_collection_needed": False,
                "missing_params":           [],
                "params_question":          None,
                "retry_count":              0,
                "latency_ms":               {},
                "_skip_to":                 "extraction",   # Demande à LangGraph de commencer directement depuis le nœud extraction
            }
        else:
            logger.info("clarification non résolue : %r", user_reply)
            return _finalize(
                session_id,
                (
                        "Je n'ai pas bien saisi votre choix. "
                        + prev_values.get("clarification_question", "Pourriez-vous préciser votre demande ?")
                ),
                "B",
                case_selected=None, confidence=None,
                rules_fired=None, odm_decision=None, latency_ms={},
                clarification_needed=True,
                clarification_question=prev_values.get("clarification_question"),
                params_collection_needed=False, params_question=None,
            )

    # ── CAS 2 — fourniture des paramètres manquants pour compléter un dossier déjà commencé. ─────────────────────────
    elif prev_values.get("params_collection_needed"):
        missing_fields  = prev_values.get("missing_params", [])
        existing_params = prev_values.get("extracted_params", {}) or {}
        question_asked  = prev_values.get("params_question", "")

        if _is_new_request(user_reply, question_asked, missing_fields):
            logger.info("nouvelle demande détectée pendant la collecte de params")
            initial_state = _fresh_state(session_id, user_reply)
        else:
            new_params    = extract_params_with_llm(
                case_name       = prev_values.get("case_selected", ""),
                text            = user_reply,
                existing_params = existing_params,
                missing_fields  = missing_fields,
                question_asked  = question_asked,
            )
            merged_params = {**existing_params, **{k: v for k, v in new_params.items() if v is not None}}
            logger.info("params reçus %s → fusionnés %s", new_params, merged_params)
            initial_state = {
                **prev_values,
                **_RESET_OUTPUTS,
                "extracted_params":         merged_params,
                "missing_params":           [],
                "params_collection_needed": False,
                "params_question":          None,
                "retry_count":              0,
                "latency_ms":               {},
                "_skip_to":                 "param_collector",  # Pour vérifier les paramètres, sans refaire tout le routage
            }

    # CAS 3 : aucune clarification ni collecte de paramètres n'est en cours.
    # Vérifie si le message poursuit la demande actuelle ou correspond à une nouvelle demande.

    else:

        # Récupère les paramètres déjà connus.
        carried_params = prev_values.get("extracted_params") or {}
        active_case    = prev_values.get("case_selected")

        # Vérifie si la dernière exécution était un fallback.
        last_fallback  = prev_values.get("fallback_type")

        if active_case and not last_fallback and _is_followup(user_reply, active_case, carried_params):
            logger.info("suivi de '%s' → on saute le router", active_case)
            initial_state = {
                **prev_values,
                **_RESET_OUTPUTS,
                "input_raw":                user_reply,
                "input_corrected":          None,
                "case_selected":            active_case,
                "extracted_params":         carried_params,
                "clarification_needed":     False,
                "params_collection_needed": False,
                "missing_params":           [],
                "params_question":          None,
                "retry_count":              0,
                "latency_ms":               {},
                "_skip_to":                 "extraction",  # Demande à LangGraph de reprendre directement au nœud d'extraction sans repasser par le routeur parce que le cas métier est déjà connu
            }
        else:

            # Démarre une nouvelle conversation en réinitialisant complètement l'état.
            initial_state = _fresh_state(session_id, user_reply)

    # ── Exécution du graphe ──────────────────────────────────────────────────
    try:
        final_state = graph.invoke(initial_state, config=config)
    except Exception:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=traceback.format_exc())

    logger.info(
        "résultat session=%s case=%s fallback=%s",
        session_id[:8], final_state.get("case_selected"), final_state.get("fallback_type"),
    )

    if final_state.get("params_collection_needed"):
        display_response = final_state.get("params_question") or "Pourriez-vous préciser ?"
    else:
        display_response = final_state.get("response_text") or ""

    return _finalize(
        session_id, display_response, final_state.get("fallback_type"),
        case_selected=final_state.get("case_selected"),
        confidence=final_state.get("confidence"),
        rules_fired=final_state.get("rules_fired"),
        odm_decision=final_state.get("odm_decision"),
        extracted_params=final_state.get("extracted_params"),
        latency_ms=final_state.get("latency_ms") or {},
        clarification_needed=final_state.get("clarification_needed", False),
        clarification_question=final_state.get("clarification_question"),
        params_collection_needed=final_state.get("params_collection_needed", False),
        params_question=final_state.get("params_question"),
    )



# ══════════════════════════════════════════════════════════════════════════════
# ADMINISTRATION (dashboard + exécution de scénarios de test)
# ══════════════════════════════════════════════════════════════════════════════

# Régénère un jeton pour l'admin courant, utilisé par le harnais d'exécution
# des scénarios de test pour s'authentifier auprès de /chat (protégé).
def _service_token(user: dict) -> str:
    return create_access_token(user["id"], user["username"], user["role"])





@app.get("/admin/test-template")
def download_template(user: dict = Depends(require_admin)):
    """Renvoie un modèle Excel vierge que l'admin peut remplir avec ses scénarios."""
    data = build_template_xlsx()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=modele_scenarios.xlsx"},
    )






@app.get("/admin/dashboard")
def admin_dashboard(user: dict = Depends(require_admin)):
    """Agrégats du dashboard, calculés depuis audit_trail / chat_messages / users."""
    import psycopg2.extras
    from app.db.postgres import get_connection

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # ── Totaux ────────────────────────────────────────────────────────
            cur.execute("SELECT COUNT(*) AS n FROM audit_trail")
            requests = int(cur.fetchone()["n"])

            cur.execute("SELECT COUNT(DISTINCT session_id) AS n FROM chat_messages")
            sessions = int(cur.fetchone()["n"])

            cur.execute("SELECT COUNT(*) AS n FROM users")
            users_count = int(cur.fetchone()["n"])

            # "Attaques bloquées" = fallback Sécurité (D) → identique à la barre du graphe.
            cur.execute("SELECT COUNT(*) AS n FROM audit_trail WHERE fallback_type = 'D'")
            attacks = int(cur.fetchone()["n"])

            # ── Répartitions ──────────────────────────────────────────────────
            cur.execute("""
                        SELECT business_case, COUNT(*) AS n FROM audit_trail
                        WHERE business_case IS NOT NULL
                        GROUP BY business_case ORDER BY n DESC
                        """)
            by_case = [{"business_case": r["business_case"], "n": int(r["n"])} for r in cur.fetchall()]

            cur.execute("""
                        SELECT fallback_type, COUNT(*) AS n FROM audit_trail
                        WHERE fallback_type IS NOT NULL
                        GROUP BY fallback_type ORDER BY fallback_type
                        """)
            by_fallback = [{"fallback_type": r["fallback_type"], "n": int(r["n"])} for r in cur.fetchall()]

            cur.execute("""
                        SELECT odm_decision, COUNT(*) AS n FROM audit_trail
                        WHERE odm_decision IS NOT NULL AND odm_decision <> ''
                        GROUP BY odm_decision ORDER BY n DESC
                        """)
            by_decision = [{"odm_decision": r["odm_decision"], "n": int(r["n"])} for r in cur.fetchall()]

            # ── Latences moyennes par nœud ────────────────────────────────────
            cur.execute("""
                        SELECT
                            AVG(latency_security)   AS s, AVG(latency_correction) AS c,
                            AVG(latency_router)     AS r, AVG(latency_extraction) AS e,
                            AVG(latency_validation) AS v, AVG(latency_odm)        AS o,
                            AVG(latency_generation) AS g, AVG(latency_total)      AS t
                        FROM audit_trail
                        """)
            lat = {k: (float(val) if val is not None else None)
                   for k, val in dict(cur.fetchone()).items()}

            # ── Percentiles de latence totale (temps de réponse de bout en bout) ─
            cur.execute("""
                        SELECT
                            PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY latency_total) AS p50,
                    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY latency_total) AS p95
                        FROM audit_trail
                        WHERE latency_total IS NOT NULL
                        """)
            pc = cur.fetchone()
            latency_pct = {
                "p50": float(pc["p50"]) if pc["p50"] is not None else None,
                "p95": float(pc["p95"]) if pc["p95"] is not None else None,
            }

        return {
            "totals": {
                "requests": requests, "sessions": sessions,
                "users": users_count, "attacks": attacks,
            },
            "by_case":         by_case,
            "by_fallback":     by_fallback,
            "by_decision":     by_decision,
            "latency_by_node": lat,
            "latency_pct":     latency_pct,
        }
    finally:
        conn.close()


@app.post("/admin/run-tests")
def run_tests(file: UploadFile = File(...), user: dict = Depends(require_admin)):
    """Exécute un fichier de scénarios (CSV ou Excel) contre le système hybride.
    Endpoint synchrone : FastAPI l'exécute dans un threadpool, ce qui évite de
    bloquer la boucle d'événements pendant les appels /chat internes."""
    content = file.file.read()
    try:
        result = run_scenarios(
            content,
            settings.self_api_url,
            token=_service_token(user),
            filename=file.filename or "",   # ← permet de distinguer CSV / Excel
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "summary":    result["summary"],
        "results":    result["results"],
        "report_b64": base64.b64encode(result["report_xlsx"]).decode(),
    }


# Endpoint permettant de récupérer la liste des cas métiers enregistrés.
@app.get("/cases")
def list_cases():

    # Importe la fonction permettant d'ouvrir une connexion PostgreSQL.
    from app.db.postgres import get_connection

    # Ouvre une connexion à la base de données.
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT case_name, description FROM semantic_cases")
            return [{"case_name": r[0], "description": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()

# Endpoint permettant de récupérer l'historique (audit) d'une conversation à partir de son session_id.
@app.get("/audit/{session_id}")
def get_audit(session_id: str, user: dict = Depends(require_admin)):

    # Permet de récupérer directement les résultats SQL sous forme de dictionnaires.
    import psycopg2.extras
    from app.db.postgres import get_connection
    conn = get_connection()
    try:

        # Utilise RealDictCursor pour obtenir des dictionnaires au lieu de tuples.
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Recherche toutes les traces de la session,triées de la plus récente à la plus ancienne.
            cur.execute(
                "SELECT * FROM audit_trail WHERE session_id = %s::uuid ORDER BY created_at DESC",
                (session_id,),
            )

            # Convertit les lignes SQL en liste de dictionnaires.
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()