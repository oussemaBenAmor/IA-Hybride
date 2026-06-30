# === Destination : app/main.py (remplace l'existant) ===
import os
import json
import uuid
import logging
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from rapidfuzz import fuzz

from app.config import settings
from app.core.logging import setup_logging
from app.core.llm import generate_text, LLMError
from app.db.postgres import init_db
from app.db.vector_store import seed_cases
from app.graph.graph_builder import get_graph
from app.graph.nodes.extraction import extract_params_with_llm
from app.graph.nodes.security import detect_attack
from app.graph.nodes.router import VALID_CASES

logger = logging.getLogger("api")


class ChatRequest(BaseModel):
    message:    str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id:               str
    response:                 str
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


graph = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global graph
    setup_logging()

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

    if settings.hf_token:
        os.environ["HF_TOKEN"] = settings.hf_token

    init_db()
    try:
        seed_cases()
    except Exception as e:
        logger.warning("seed_cases ignoré (Ollama injoignable ?) : %s", e)

    graph = get_graph()
    logger.info("FastAPI démarré — LangGraph prêt (Ollama=%s)", settings.ollama_base_url)
    yield


app = FastAPI(title="Orchestration IA + BRMS", version="3.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


LEXICAL_HINTS = {
    "credit_immobilier":   ("immobilier", "immo", "maison", "appartement", "logement", "résidence", "residence"),
    "credit_consommation": ("consommation", "conso", "personnel", "voiture", "auto", "travaux", "loisir"),
    "carte_bancaire":      ("carte", "visa", "mastercard", "opposition", "plafond"),
    "virement":            ("virement", "transfert", "iban", "bénéficiaire", "beneficiaire"),
    "assurance_vie":       ("assurance", "épargne", "epargne", "placement", "retraite"),
}


# ══════════════════════════════════════════════════════════════════════════════
# GREETINGS — petite conversation traitée directement, sans passer par le graphe.
# Matching lexical + fuzzy (rapidfuzz) pour tolérer les fautes de frappe
# ("bonjouur", "helo"...). Volontairement simple et rapide (~0 ms, pas de LLM).
# ══════════════════════════════════════════════════════════════════════════════
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


# ── CAPABILITIES — "que faites-vous ?", "quels services ?" → réponse détaillée ──
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


def _is_capability_question(text: str) -> bool:
    """Détecte une question sur les services offerts (méta, pas un cas métier)."""
    t = text.lower().strip(" !.?,")
    if len(t) > 80:
        return False
    return any(trigger in t for trigger in CAPABILITY_TRIGGERS)

_FUZZY_THRESHOLD = 75   # tolérance fautes de frappe sur le premier mot


def _match_smalltalk(text: str) -> str | None:
    """Renvoie la réponse de small talk appropriée, ou None si ce n'en est pas."""
    t = text.lower().strip(" !.?,")
    if not t or len(t) > 40:          # un vrai message métier est plus long
        return None
    first = t.split()[0]

    def hits(words: tuple) -> bool:
        for w in words:
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


# ── État vierge : remet TOUS les champs de sortie à None/défaut ───────────────
# Indispensable avec le checkpointer : graph.invoke FUSIONNE l'input avec le
# dernier checkpoint du thread. Tout champ non réinitialisé conserve la valeur
# du tour précédent. On force donc ici un reset complet pour une nouvelle demande.
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


# Champs de sortie à effacer sur les chemins de SUITE (clarification, params)
# qui réutilisent **prev_values mais ne doivent pas traîner d'anciens résultats.
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


def _is_new_request(user_reply: str, question_asked: str, missing_fields: list) -> bool:
    prompt = f"""Le chatbot a demandé : "{question_asked}"
Champs attendus : {missing_fields}
L'utilisateur a répondu : "{user_reply}"

La réponse est-elle :
A) Une réponse directe à la question (il fournit la valeur demandée)
B) Une nouvelle demande différente (il change de sujet)

Réponds UNIQUEMENT par A ou B."""
    try:
        ans = generate_text(prompt, task="classifier", temperature=0.0, timeout=30.0).upper()
        return ans.startswith("B")
    except LLMError as e:
        logger.warning("intent_check KO (%s) → on suppose réponse directe", e)
        return False


def _is_followup(user_reply: str, case_selected: str, known_params: dict) -> bool:
    prompt = f"""Demande bancaire en cours : cas = "{case_selected}", paramètres connus = {json.dumps(known_params, ensure_ascii=False)}.
Nouveau message de l'utilisateur : "{user_reply}"

Ce message est-il :
A) La suite de cette demande (précise/modifie un paramètre — durée, montant, revenu… — ou pose une question sur ce même dossier)
B) Une nouvelle demande sans rapport (autre produit bancaire, autre sujet)

Réponds UNIQUEMENT par A ou B."""
    try:
        ans = generate_text(prompt, task="classifier", temperature=0.0, timeout=30.0).upper()
        return ans.startswith("A")
    except LLMError as e:
        logger.warning("followup_check KO (%s) → on suppose nouvelle demande", e)
        return False


@app.get("/health")
def health():
    return {"status": "ok", "env": settings.app_env}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    session_id = request.session_id or str(uuid.uuid4())
    config     = {"configurable": {"thread_id": session_id}}
    user_reply = request.message

    try:
        current_state = graph.get_state(config)
        prev_values   = current_state.values if current_state else {}
    except Exception:
        prev_values = {}

    # ── SÉCURITÉ — screening sur TOUS les tours ─────────────────────────────
    if detect_attack(user_reply):
        logger.warning("attaque détectée sur le tour courant → blocage")
        initial_state = _fresh_state(session_id, user_reply)

    # ── GREETINGS — bonjour / merci / au revoir, réponse directe sans graphe ─
    # On ne court-circuite PAS une collecte en cours : si le bot attend une
    # clarification ou des paramètres, un simple "merci" suit le chemin normal.
    elif (smalltalk := _match_smalltalk(user_reply)) is not None \
            and not prev_values.get("clarification_needed") \
            and not prev_values.get("params_collection_needed"):
        logger.info("small talk détecté → réponse directe")
        return ChatResponse(
            session_id=session_id,
            response=smalltalk,
            case_selected=None, confidence=None, fallback_type=None,
            rules_fired=None, odm_decision=None, latency_ms={},
            clarification_needed=False, clarification_question=None,
            params_collection_needed=False, params_question=None,
        )

    # ── CAPABILITIES — "quels services ?" → réponse détaillée, sans graphe ──
    # On ne court-circuite PAS une collecte en cours (clarification / params).
    elif _is_capability_question(user_reply) \
            and not prev_values.get("clarification_needed") \
            and not prev_values.get("params_collection_needed"):
        logger.info("question capacités détectée → réponse directe")
        return ChatResponse(
            session_id=session_id,
            response=CAPABILITIES_REPLY,
            case_selected=None, confidence=None, fallback_type=None,
            rules_fired=None, odm_decision=None, latency_ms={},
            clarification_needed=False, clarification_question=None,
            params_collection_needed=False, params_question=None,
        )

    # ── CAS 1 — réponse à une clarification de cas (résolution lexicale) ─────
    elif prev_values.get("clarification_needed"):
        top2      = prev_values.get("top2_scores", []) or []
        presented = [item["case"] for item in top2 if item.get("case") in VALID_CASES]
        reply_low = user_reply.lower()
        hits = [c for c in presented if any(kw in reply_low for kw in LEXICAL_HINTS.get(c, ()))]
        forced_case = hits[0] if len(hits) == 1 else None

        if forced_case:
            logger.info("clarification résolue → %s", forced_case)
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
                "_skip_to":                 "extraction",
            }
        else:
            logger.info("clarification non résolue : %r", user_reply)
            return ChatResponse(
                session_id=session_id,
                response=(
                        "Je n'ai pas bien saisi votre choix. "
                        + prev_values.get("clarification_question", "Pourriez-vous préciser votre demande ?")
                ),
                case_selected=None, confidence=None, fallback_type="B",
                rules_fired=None, odm_decision=None, latency_ms={},
                clarification_needed=True,
                clarification_question=prev_values.get("clarification_question"),
                params_collection_needed=False, params_question=None,
            )

    # ── CAS 2 — fourniture des paramètres manquants ─────────────────────────
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
                "_skip_to":                 "param_collector",
            }

    # ── CAS 3 — nouvelle demande ou suivi contextuel ────────────────────────
    else:
        carried_params = prev_values.get("extracted_params") or {}
        active_case    = prev_values.get("case_selected")
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
                "_skip_to":                 "extraction",
            }
        else:
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

    return ChatResponse(
        session_id=session_id,
        response=display_response,
        case_selected=final_state.get("case_selected"),
        confidence=final_state.get("confidence"),
        fallback_type=final_state.get("fallback_type"),
        rules_fired=final_state.get("rules_fired"),
        odm_decision=final_state.get("odm_decision"),
        extracted_params=final_state.get("extracted_params"),
        latency_ms=final_state.get("latency_ms") or {},
        clarification_needed=final_state.get("clarification_needed", False),
        clarification_question=final_state.get("clarification_question"),
        params_collection_needed=final_state.get("params_collection_needed", False),
        params_question=final_state.get("params_question"),
    )


@app.get("/cases")
def list_cases():
    from app.db.postgres import get_connection
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT case_name, description FROM semantic_cases")
            return [{"case_name": r[0], "description": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


@app.get("/audit/{session_id}")
def get_audit(session_id: str):
    import psycopg2.extras
    from app.db.postgres import get_connection
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM audit_trail WHERE session_id = %s::uuid ORDER BY created_at DESC",
                (session_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()