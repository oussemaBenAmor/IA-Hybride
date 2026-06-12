from langgraph.graph import StateGraph, END
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


# ── Nœud : résolution d'une clarification de cas (Fallback B) ─────────────────
def clarification_resolver_node(state: GraphState) -> GraphState:
    print(f"✅ clarification_resolver : cas = {state.get('case_selected')}")
    return {
        **state,
        "fallback_type":          None,
        "fallback_reason":        None,
        "clarification_needed":   False,
        "clarification_question": None,
        "_skip_to":               None,
    }


# ── Nœud : collecte de paramètres manquants ───────────────────────────────────
def param_collector_node(state: GraphState) -> GraphState:
    """
    Reçoit les params fusionnés depuis main.py et les prépare pour validation.
    NE passe PAS par extraction — les params sont déjà dans extracted_params.
    """
    print(f"✅ param_collector : params prêts = {state.get('extracted_params')}")
    return {
        **state,
        "params_collection_needed": False,
        "params_question":          None,
        "missing_params":           [],
        "fallback_type":            None,
        "fallback_reason":          None,
        "retry_count":              0,
        "_skip_to":                 None,
    }


# ── Conditions ────────────────────────────────────────────────────────────────

def entry_point(state: GraphState) -> str:
    skip = state.get("_skip_to")
    if skip == "extraction":
        return "clarification_resolver"
    if skip == "param_collector":
        return "param_collector"
    return "security"

def should_stop_after_security(state: GraphState) -> str:
    return "audit" if state.get("is_blocked") else "correction"

def should_stop_after_router(state: GraphState) -> str:
    ft = state.get("fallback_type")
    if ft in ("A", "B"):
        return "audit"
    return "extraction"

def should_retry_or_continue(state: GraphState) -> str:
    if state.get("fallback_type") == "C":
        return "audit"
    # Paramètres manquants → on s'arrête pour demander à l'utilisateur
    if state.get("params_collection_needed"):
        return "audit"
    if state.get("validation_errors") and state.get("retry_count", 0) < 3:
        return "extraction"
    return "odm_call"

def should_stop_after_odm(state: GraphState) -> str:
    return "audit" if state.get("fallback_type") == "E" else "generation"


# ── Construction du graphe ────────────────────────────────────────────────────

def build_graph(checkpointer=None):
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

    builder.set_conditional_entry_point(
        entry_point,
        {
            "security":               "security",
            "clarification_resolver": "clarification_resolver",
            "param_collector":        "param_collector",
        }
    )

    builder.add_conditional_edges("security", should_stop_after_security, {
        "audit":      "audit",
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
    builder.add_conditional_edges("validation", should_retry_or_continue, {
        "audit":      "audit",
        "extraction": "extraction",
        "odm_call":   "odm_call",
    })
    builder.add_conditional_edges("odm_call", should_stop_after_odm, {
        "audit":      "audit",
        "generation": "generation",
    })
    builder.add_edge("generation", "audit")
    builder.add_edge("audit", END)

    return builder.compile(checkpointer=checkpointer)


# ── Initialisation du checkpointer ───────────────────────────────────────────

def _drop_checkpoint_tables(conn):
    with conn.cursor() as cur:
        cur.execute("""
                    DROP TABLE IF EXISTS
                        public.checkpoint_writes,
                        public.checkpoint_blobs,
                        public.checkpoints,
                        public.checkpoint_migrations
                        CASCADE;
                    """)
    print("🗑️  Anciennes tables checkpoint supprimées")


def _task_path_exists(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = 'checkpoint_writes'
                      AND column_name  = 'task_path'
                    """)
        return cur.fetchone() is not None


def _get_checkpointer():
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg

        conn = psycopg.connect(settings.postgres_url, autocommit=True)
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
        from langgraph.checkpoint.memory import MemorySaver
        return MemorySaver()


def get_graph():
    checkpointer = _get_checkpointer()
    return build_graph(checkpointer=checkpointer)