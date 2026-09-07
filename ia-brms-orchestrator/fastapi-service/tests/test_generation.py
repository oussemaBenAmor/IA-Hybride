"""
Tests de build_generation_prompt (generation.py) — fonction PURE.

On vérifie le correctif clé (bug du jargon crédit) : le vocabulaire crédit
(mensualité, taux d'endettement, capacité d'emprunt) ne doit apparaître dans le
prompt QUE pour les crédits. Pour l'assurance / carte / virement, il doit être
ABSENT, et une interdiction explicite doit figurer.

C'est un test important car il verrouille un bug réel : le LLM parlait de
mensualité/taux pour une assurance vie.
"""
import pytest

from app.graph.nodes.generation import build_generation_prompt, CREDIT_CASES



# Construit un state minimal pour les tests de génération,
# avec des valeurs par défaut modifiables selon le scénario testé.
def _state(case, **extra):
    """Construit un state minimal pour la génération."""
    base = {
        "case_selected": case,
        "input_raw": "ma demande",
        "input_corrected": None,
        "odm_decision": {"decision": "ELIGIBLE", "details": {}},
        "rules_fired": [],
        "extracted_params": {},
    }
    base.update(extra)
    return base


class TestBlocCreditConditionnel:
    """Le vocabulaire crédit doit être conditionnel au cas métier."""

    @pytest.mark.parametrize("case", ["credit_immobilier", "credit_consommation"])
    def test_credit_contient_vocabulaire(self, case):
        # Pour un crédit, le prompt PEUT contenir le vocabulaire crédit
        prompt = build_generation_prompt(_state(case))
        # au moins une notion de crédit présente (table des clés / mensualité)
        assert "mensualite" in prompt.lower() or "endettement" in prompt.lower()

    @pytest.mark.parametrize("case", ["assurance_vie", "carte_bancaire", "virement"])
    def test_non_credit_interdit_vocabulaire(self, case):
        # Pour un NON-crédit, l'interdiction absolue doit être présente
        prompt = build_generation_prompt(_state(case))
        assert "INTERDICTION ABSOLUE" in prompt

    @pytest.mark.parametrize("case", ["assurance_vie", "carte_bancaire", "virement"])
    def test_non_credit_pas_de_table_cles_credit(self, case):
        # La table des clés crédit (montant_max_empruntable...) ne doit PAS être là
        prompt = build_generation_prompt(_state(case))
        assert "montant_max_empruntable" not in prompt

    def test_credit_cases_contient_les_deux(self):
        # Vérifie la constante elle-même
        assert CREDIT_CASES == {"credit_immobilier", "credit_consommation"}


class TestContenuPrompt:
    """Le prompt doit contenir les infos essentielles de la décision."""

    def test_prompt_contient_la_decision(self):
        prompt = build_generation_prompt(_state("virement",
                                                odm_decision={"decision": "APPROUVE", "details": {}}))
        assert "APPROUVE" in prompt

    def test_prompt_contient_le_message_client(self):
        prompt = build_generation_prompt(_state("virement", input_raw="je veux virer 500 euros"))
        assert "500 euros" in prompt
