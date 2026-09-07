"""
Construit et configure le workflow LangGraph de l'application.
"""

# Classe LangGraph permettant de construire le workflow (ajout des nœuds et des transitions)
# END représente la fin du workflow
from langgraph.graph import StateGraph, END


# Structure de l'état partagé : contient toutes les données qui circulent entre les nœuds
from app.graph.state import GraphState
from app.graph.nodes.security   import security_node
from app.graph.nodes.correction import correction_node
from app.graph.nodes.router     import router_node
from app.graph.nodes.extraction import extraction_node
from app.graph.nodes.validation import validation_node
from app.graph.nodes.odm_call   import odm_call_node
from app.graph.nodes.generation import generation_node
from app.graph.nodes.audit      import audit_node
from app.config import settings


# Nœud : résolution d'une clarification de cas :Traite la réponse du client après une demande de clarification (Fallback B)
def clarification_resolver_node(state: GraphState) -> GraphState:
    print(f"✅ clarification_resolver : cas = {state.get('case_selected')}")
    return {
        **state,

        # Réinitialise le fallback B
        "fallback_type":          None,
        "fallback_reason":        None,

        # Indique que la clarification est terminée
        "clarification_needed":   False,
        "clarification_question": None,

        # Supprime le saut vers un autre nœud
        "_skip_to":               None,
    }


# Nœud : collecte de paramètres manquants : # Prépare les paramètres complétés par l'utilisateur avant la validation
def param_collector_node(state: GraphState) -> GraphState:
    """
    Reçoit les params fusionnés depuis main.py et les prépare pour validation.
    NE passe PAS par extraction — les params sont déjà dans extracted_params.
    """
    print(f"✅ param_collector : params prêts = {state.get('extracted_params')}")
    return {
        **state,

        # Réinitialise l'état de collecte des paramètres
        "params_collection_needed": False,
        "params_question":          None,
        "missing_params":           [],
        "fallback_type":            None,
        "fallback_reason":          None,
        "retry_count":              0,
        "_skip_to":                 None,
    }


# Détermine le premier nœud du workflow selon le contexte.
# Permet de reprendre un traitement interrompu sans recommencer tout le pipeline.
def entry_point(state: GraphState) -> str:
    skip = state.get("_skip_to")

    # Après une clarification utilisateur, on reprend par la résolution du cas pour nettoyer les flags
    if skip == "extraction":
        return "clarification_resolver"


    # Après réception des paramètres manquants, on reprend directement leur collecte
    # puis on continue vers la validation.
    if skip == "param_collector":
        return "param_collector"

    # Démarrage normal du pipeline
    return "security"


# Arrête le pipeline si une attaque est détectée
def should_stop_after_security(state: GraphState) -> str:
    return "audit" if state.get("is_blocked") else "correction"


# Arrête le pipeline en cas de Fallback A ou B
def should_stop_after_router(state: GraphState) -> str:
    ft = state.get("fallback_type")
    if ft in ("A", "B"):
        return "audit"
    return "extraction"


# Décide de relancer l'extraction ou de poursuivre le traitement
def should_retry_or_continue(state: GraphState) -> str:
    if state.get("fallback_type") == "C":
        return "audit"
    # Paramètres manquants → on s'arrête pour demander à l'utilisateur
    if state.get("params_collection_needed"):
        return "audit"
    if state.get("validation_errors") and state.get("retry_count", 0) < 3:
        return "extraction"
    return "odm_call"


# Arrête le pipeline si ODM retourne un fallback E
def should_stop_after_odm(state: GraphState) -> str:
    return "audit" if state.get("fallback_type") == "E" else "generation"


# ── Construction du graphe ────────────────────────────────────────────────────

def build_graph(checkpointer=None):

    # Création du graphe basé sur GraphState
    builder = StateGraph(GraphState)

    builder.add_node("security",               security_node)
    builder.add_node("correction",             correction_node)
    builder.add_node("router",                 router_node)
    builder.add_node("clarification_resolver", clarification_resolver_node)
    builder.add_node("param_collector",        param_collector_node)
    builder.add_node("extraction",             extraction_node)
    builder.add_node("validation",             validation_node)
    builder.add_node("odm_call",               odm_call_node)
    builder.add_node("generation",             generation_node)
    builder.add_node("audit",                  audit_node)


    # Choisit dynamiquement le point d'entrée du workflow
    builder.set_conditional_entry_point(
        entry_point,
        {
            "security":               "security",
            "clarification_resolver": "clarification_resolver",
            "param_collector":        "param_collector",
        }
    )


    # Définit les transitions conditionnelles après le nœud Security.
    # La fonction should_stop_after_security() décide si la requête doit être bloquée
    # ou si elle peut continuer dans le pipeline normal.

    builder.add_conditional_edges("security", should_stop_after_security, {

        # Si une attaque est détectée → arrêt du traitement et sauvegarde dans l'audit
        "audit":      "audit",

        # Sinon → correction orthographique du message utilisateur
        "correction": "correction",
    })


    builder.add_edge("correction", "router")
    builder.add_conditional_edges("router", should_stop_after_router, {
        "audit":      "audit",
        "extraction": "extraction",
    })

    # clarification_resolver → extraction (le LLM doit extraire depuis input_raw)
    builder.add_edge("clarification_resolver", "extraction")

    # param_collector → validation directement (params déjà extraits et fusionnés)
    builder.add_edge("param_collector", "validation")

    builder.add_edge("extraction", "validation")


    # Définit les transitions après la validation des paramètres.
    # La fonction décide s'il faut recommencer l'extraction, arrêter ou continuer vers ODM.
    builder.add_conditional_edges("validation", should_retry_or_continue, {

        # Paramètres invalides ou fallback C → arrêt du pipeline
        "audit":      "audit",

        # Erreur d'extraction mais nombre de tentatives non dépassé
        # → nouvelle tentative d'extraction
        "extraction": "extraction",

        # Paramètres valides → appel du moteur de règles ODM
        "odm_call":   "odm_call",
    })
    builder.add_conditional_edges("odm_call", should_stop_after_odm, {
        "audit":      "audit",
        "generation": "generation",
    })
    builder.add_edge("generation", "audit")
    builder.add_edge("audit", END)


    # Compile le graphe pour obtenir un workflow exécutable.
    # Le checkpointer permet de sauvegarder/restaurer l'état du workflow.

    return builder.compile(checkpointer=checkpointer)


# ── Initialisation du checkpointer ───────────────────────────────────────────
# Supprime les anciennes tables du checkpointer
# Utilisé lorsque le schéma PostgreSQL n'est plus compatible avec LangGraph
def _drop_checkpoint_tables(conn):

    # Création d'un curseur SQL
    with conn.cursor() as cur:

        # Suppression des tables de sauvegarde LangGraph
        cur.execute("""
                    DROP TABLE IF EXISTS
                        public.checkpoint_writes,
                        public.checkpoint_blobs,
                        public.checkpoints,
                        public.checkpoint_migrations
                        CASCADE;
                    """)
    print("🗑️  Anciennes tables checkpoint supprimées")

# Vérifie si la structure actuelle du checkpointer correspond à la version attendue et contient la colonne task_path
def _task_path_exists(conn) -> bool:
    with conn.cursor() as cur:

        # Recherche l'existence de la colonne task_path
        cur.execute("""
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = 'checkpoint_writes'
                      AND column_name  = 'task_path'
                    """)

        # True si la colonne existe, False sinon
        return cur.fetchone() is not None


# Initialise le checkpointer PostgreSQL.
# Si PostgreSQL échoue, utilise un stockage mémoire temporaire.

def _get_checkpointer():
    try:
        from langgraph.checkpoint.postgres import PostgresSaver #Importe le sauvegardeur PostgreSQL de LangGraph
        import psycopg   #Bibliothèque Python pour communiquer avec PostgreSQL.


        # Connexion à PostgreSQ
        conn = psycopg.connect(settings.postgres_url, autocommit=True)

        # Création du gestionnaire de sauvegarde LangGraph
        saver = PostgresSaver(conn)

        with conn.cursor() as cur:
            cur.execute("""
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name   = 'checkpoint_writes'
                        """)
            table_exists = cur.fetchone() is not None

        if table_exists and not _task_path_exists(conn):
            print("⚠️  Schéma checkpoint obsolète (task_path manquant) → recréation")
            _drop_checkpoint_tables(conn)

        saver.setup()
        print("✅ PostgresSaver prêt")
        return saver

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ PostgresSaver échoué : {type(e).__name__}: {e}")
        print("⚠️  Fallback → MemorySaver")

        # En cas d'erreur, utilisation d'un stockage en mémoire
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()

# Créer le graphe complet.
def get_graph():
    checkpointer = _get_checkpointer()

    #On construit ton workflow LangGraph avec ce sauvegardeur.
    return build_graph(checkpointer=checkpointer)