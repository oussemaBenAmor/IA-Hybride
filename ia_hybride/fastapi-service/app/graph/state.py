from typing import Optional, Any
from langgraph.graph import MessagesState
from typing import TypedDict, Optional

class GraphState(TypedDict, total=False):
    # Entrée
    session_id:             str
    input_raw:              str
    input_corrected:        Optional[str]

    # Sécurité
    is_blocked:             bool
    security_pattern:       Optional[str]

    # Fallback
    fallback_type:          Optional[str]
    fallback_reason:        Optional[str]

    # Router
    case_selected:          Optional[str]
    confidence:             Optional[float]
    top2_scores:            Optional[list]

    # Extraction / Validation
    extracted_params:       Optional[dict]
    validation_errors:      Optional[list]
    retry_count:            int

    # Clarification cas (Fallback B)
    clarification_needed:   bool
    clarification_question: Optional[str]

    # Collecte de paramètres manquants
    missing_params:         Optional[list]   # liste des champs manquants
    params_question:        Optional[str]    # question posée à l'user pour les params
    params_collection_needed: bool           # True si on attend des params de l'user

    # ODM
    odm_payload:            Optional[dict]
    odm_decision:           Optional[dict]
    rules_fired:            Optional[list]

    # Réponse finale
    response_text:          Optional[str]

    # Latences
    latency_ms:             dict

    # Navigation interne (non exposé à l'API)
    _skip_to:               Optional[str]