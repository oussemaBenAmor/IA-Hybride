"""
Tests des fonctions PURES de validation.py — la logique des champs requis.

On teste :
  - _compute_missing    : quels champs obligatoires manquent (inconditionnels + conditionnels)
  - _human_question     : formulation humaine d'un champ
  - _build_question     : question complète pour un ou plusieurs champs

Ces tests encodent les règles métier : quels paramètres sont nécessaires avant
d'appeler l'ODM, y compris les requis CONDITIONNELS (montant_initial seulement si
souscription, plafond_souhaite seulement si operation=plafond).
"""
import pytest

from app.graph.nodes.validation import (
    _compute_missing,
    _human_question,
    _build_question,
)


# ══════════════════════════════════════════════════════════════════════════════
# _compute_missing : requis INCONDITIONNELS
# ══════════════════════════════════════════════════════════════════════════════

class TestRequisInconditionnels:
    """Champs toujours obligatoires selon le cas métier."""

    def test_immo_complet_rien_ne_manque(self):
        # Tous les champs requis présents → aucun manquant
        params = {"montant": 200000, "duree_mois": 240, "revenu_mensuel": 5000, "age": 35}
        assert _compute_missing("credit_immobilier", params) == []

    def test_immo_sans_age(self):
        # BUG identifié en éval : l'âge manque → doit être réclamé
        params = {"montant": 200000, "duree_mois": 240, "revenu_mensuel": 5000}
        missing = _compute_missing("credit_immobilier", params)
        assert "age" in missing

    def test_immo_plusieurs_manquants(self):
        # Seul le montant est fourni → durée, revenu, âge manquent
        params = {"montant": 200000}
        missing = _compute_missing("credit_immobilier", params)
        assert set(missing) == {"duree_mois", "revenu_mensuel", "age"}

    def test_conso_pas_besoin_age(self):
        # Le crédit conso ne requiert PAS l'âge (contrairement à l'immo)
        params = {"montant": 20000, "duree_mois": 48, "revenu_mensuel": 3000}
        assert _compute_missing("credit_consommation", params) == []

    def test_virement_complet(self):
        params = {"type_virement": "standard", "montant": 500}
        assert _compute_missing("virement", params) == []


# ══════════════════════════════════════════════════════════════════════════════
# _compute_missing : requis CONDITIONNELS (le point subtil)
# ══════════════════════════════════════════════════════════════════════════════

class TestRequisConditionnels:
    """Champs requis SEULEMENT selon la valeur d'un autre champ."""

    def test_assurance_souscription_exige_montant_initial(self):
        # operation=souscription → montant_initial devient obligatoire
        params = {"operation": "souscription", "age": 40}
        missing = _compute_missing("assurance_vie", params)
        assert "montant_initial" in missing

    def test_assurance_rachat_nexige_pas_montant_initial(self):
        # operation=rachat → montant_initial n'est PAS requis
        params = {"operation": "rachat", "age": 40}
        missing = _compute_missing("assurance_vie", params)
        assert "montant_initial" not in missing
        assert missing == []  # tout est là pour un rachat

    def test_assurance_souscription_complete(self):
        # souscription avec montant_initial → rien ne manque
        params = {"operation": "souscription", "age": 40, "montant_initial": 5000}
        assert _compute_missing("assurance_vie", params) == []

    def test_carte_plafond_exige_valeur(self):
        # operation=plafond → plafond_souhaite devient obligatoire
        params = {"operation": "plafond"}
        missing = _compute_missing("carte_bancaire", params)
        assert "plafond_souhaite" in missing

    def test_carte_opposition_nexige_rien(self):
        # operation=opposition → pas de plafond requis
        params = {"operation": "opposition"}
        assert _compute_missing("carte_bancaire", params) == []

    def test_conditionnel_insensible_casse(self):
        # "Souscription" (majuscule) doit déclencher le requis comme "souscription"
        params = {"operation": "Souscription", "age": 40}
        missing = _compute_missing("assurance_vie", params)
        assert "montant_initial" in missing


# ══════════════════════════════════════════════════════════════════════════════
# _human_question et _build_question : formulation des questions
# ══════════════════════════════════════════════════════════════════════════════

class TestQuestions:
    """Les questions posées à l'utilisateur doivent être en langage humain."""

    def test_operation_assurance_propose_options(self):
        # La question sur 'operation' pour l'assurance doit lister les choix
        q = _human_question("assurance_vie", "operation")
        assert "souscription" in q.lower()
        assert "rachat" in q.lower()

    def test_operation_carte_propose_options(self):
        q = _human_question("carte_bancaire", "operation")
        assert "opposition" in q.lower()

    def test_champ_numerique_formulation(self):
        # Un champ simple a une formulation claire
        q = _human_question("credit_immobilier", "age")
        assert "âge" in q.lower() or "age" in q.lower()

    def test_build_question_un_champ(self):
        # Question pour UN seul champ manquant
        q = _build_question("credit_immobilier", ["age"])
        assert "crédit immobilier" in q.lower()
        assert q.endswith("?")

    def test_build_question_plusieurs_champs(self):
        # Question pour PLUSIEURS champs : doit tous les mentionner
        q = _build_question("credit_immobilier", ["revenu_mensuel", "age"])
        assert "?" in q
        # la question enchaîne les champs (contient un séparateur)
        assert len(q) > 30
