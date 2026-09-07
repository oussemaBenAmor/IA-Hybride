"""
Tests du router_node (router.py) — NIVEAU 2 : avec MOCKING.

Le routeur a DEUX dépendances externes qu'on ne veut pas appeler réellement :
  - search_similar_cases()  : le bi-encoder pgvector (base + embeddings)
  - _llm_classify()         : l'appel au LLM

On les REMPLACE par des mocks (faux objets contrôlés) grâce au plugin pytest-mock,
qui fournit la fixture `mocker`. Ainsi :
  - pas besoin d'Ollama ni de PostgreSQL,
  - résultats déterministes (on décide ce que renvoie chaque dépendance),
  - on teste UNIQUEMENT la logique de décision du routeur.

CONCEPT CLÉ — mocker.patch("chemin.vers.fonction", return_value=...) :
  remplace la fonction par un faux qui renvoie toujours return_value.
  Le chemin doit être celui où la fonction est UTILISÉE (dans router.py),
  pas où elle est définie. C'est le piège classique du mocking.
"""
import pytest

from app.graph.nodes.router import router_node



def _candidates(*pairs):

    return [{"case_name": c, "score": s} for c, s in pairs]


# ══════════════════════════════════════════════════════════════════════════════
# Chemin 1 : hors-périmètre bon marché (score trop faible)
# ══════════════════════════════════════════════════════════════════════════════

class TestHorsPerimetreScoreFaible:

    def test_score_sous_le_plancher_donne_fallback_a(self, mocker):
        # Arrange : le bi-encoder renvoie un score très faible (< 0.45)
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=_candidates(("credit_immobilier", 0.30), ("virement", 0.20)),
        )
        # Act
        result = router_node({"input_raw": "quelle est la météo"})
        # Assert : score trop faible → Fallback A (hors-périmètre)
        assert result["fallback_type"] == "A"

    def test_aucun_candidat_donne_fallback_a(self, mocker):
        # Si le bi-encoder ne renvoie RIEN → hors-périmètre
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=[],
        )
        result = router_node({"input_raw": "xyz"})
        assert result["fallback_type"] == "A"


# ══════════════════════════════════════════════════════════════════════════════
# Chemin 2 : chemin rapide (score élevé + gap net → pas de LLM)
# ══════════════════════════════════════════════════════════════════════════════

class TestCheminRapide:

    def test_score_eleve_gap_net_accepte_sans_llm(self, mocker):
        # Score >= 0.70 ET gap >= 0.15 → acceptation directe, LLM PAS appelé
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=_candidates(("credit_immobilier", 0.90), ("virement", 0.50)),
        )
        # On espionne _llm_classify pour VÉRIFIER qu'il n'est pas appelé
        llm_spy = mocker.patch("app.graph.nodes.router._llm_classify")

        result = router_node({"input_raw": "je veux un crédit immobilier"})

        assert result["case_selected"] == "credit_immobilier"
        # Le LLM ne doit PAS avoir été appelé (chemin rapide)
        llm_spy.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# Chemin 3 : zone incertaine → le LLM tranche
# ══════════════════════════════════════════════════════════════════════════════

class TestZoneIncertaineLLM:

    def test_llm_choisit_un_cas_valide(self, mocker):
        # Score moyen → on passe par le LLM. Le LLM (mocké) choisit credit_consommation.
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=_candidates(("credit_consommation", 0.65), ("credit_immobilier", 0.60)),
        )
        mocker.patch(
            "app.graph.nodes.router._llm_classify",
            return_value={"case": "credit_consommation", "confidence": 0.8,
                          "ambiguous": False, "alternatives": []},
        )
        result = router_node({"input_raw": "je veux financer une voiture"})
        assert result["case_selected"] == "credit_consommation"

    def test_llm_signale_ambiguite_donne_fallback_b(self, mocker):
        # Le LLM déclare une ambiguïté avec 2 alternatives → Fallback B (clarification)
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=_candidates(("credit_immobilier", 0.65), ("credit_consommation", 0.62)),
        )
        mocker.patch(
            "app.graph.nodes.router._llm_classify",
            return_value={"case": "credit_immobilier", "confidence": 0.5,
                          "ambiguous": True,
                          "alternatives": ["credit_immobilier", "credit_consommation"]},
        )
        result = router_node({"input_raw": "je veux un crédit"})
        assert result["fallback_type"] == "B"

    def test_llm_dit_hors_perimetre(self, mocker):
        # Le LLM tranche hors_perimetre (sans ambiguïté) → Fallback A
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=_candidates(("virement", 0.55), ("carte_bancaire", 0.50)),
        )
        mocker.patch(
            "app.graph.nodes.router._llm_classify",
            return_value={"case": "hors_perimetre", "confidence": 0.6,
                          "ambiguous": False, "alternatives": []},
        )
        result = router_node({"input_raw": "raconte-moi une blague"})
        assert result["fallback_type"] == "A"

    def test_llm_echoue_fallback_sur_biencoder(self, mocker):
        # Si le LLM échoue (renvoie None), on retombe sur le top1 du bi-encoder
        mocker.patch(
            "app.graph.nodes.router.search_similar_cases",
            return_value=_candidates(("virement", 0.65), ("carte_bancaire", 0.60)),
        )
        mocker.patch("app.graph.nodes.router._llm_classify", return_value=None)
        result = router_node({"input_raw": "faire un virement"})
        # pas de crash, on accepte le top1 du bi-encoder
        assert result["case_selected"] == "virement"
