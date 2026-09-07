"""
Tests du odm_call_node (odm_call.py) — NIVEAU 2 : avec MOCKING de httpx.

Le nœud ODM appelle Spring Boot via httpx.post(). On ne veut PAS que Spring
tourne pendant les tests. On mocke donc _call_odm (la fonction qui fait l'appel
HTTP) pour contrôler ce que "Spring" renvoie — succès OU erreur.

Ce qu'on vérifie :
  - décision correctement lue depuis la réponse (rulesTriggered en camelCase)
  - gestion des erreurs → Fallback E (ODM indisponible)
  - le payload envoyé est bien formé {caseName, params}
"""
import pytest
import httpx
from tenacity import RetryError

from app.graph.nodes.odm_call import odm_call_node, _extract_rules


# ══════════════════════════════════════════════════════════════════════════════
# _extract_rules : lecture des règles (fonction pure, pas besoin de mock)
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractRules:
#Vérifie que le système extrait correctement la liste des règles déclenchées depuis la réponse renvoyée
# par ODM/Spring Boot.

# Vérifie que le format utilisé par Java (camelCase) est correctement interprété

    def test_lit_camelcase(self):
        # Java renvoie "rulesTriggered" (camelCase, à cause de Lombok)
        result = {"decision": "ELIGIBLE", "rulesTriggered": ["regle_criteres_ok"]}
        assert _extract_rules(result) == ["regle_criteres_ok"]

# Vérifie que le système accepte aussi un format Python classique (snake_case)
# afin d'être robuste si la réponse change de format

    def test_tolere_snakecase(self):
        # Filet de sécurité : on tolère aussi snake_case
        result = {"rules_triggered": ["r1", "r2"]}
        assert _extract_rules(result) == ["r1", "r2"]


# Vérifie que l'absence de règles dans la réponse ne provoque pas d'erreur
# et retourne simplement une liste vide

    def test_liste_vide_si_absent(self):
        # Pas de clé de règles → liste vide (pas d'erreur)
        assert _extract_rules({"decision": "APPROUVE"}) == []



"""
    Tests du fonctionnement normal du nœud ODM.
    On vérifie que lorsque le moteur ODM répond correctement :
    - la décision est bien récupérée,
    - les règles déclenchées sont bien extraites,
    - aucun mécanisme de fallback n'est activé.
"""
class TestOdmSucces:

    def test_decision_lue_correctement(self, mocker):
        # On mocke _call_odm pour simuler une réponse Spring réussie
        mocker.patch(
            "app.graph.nodes.odm_call._call_odm",
            return_value={"decision": "ELIGIBLE", "rulesTriggered": ["regle_criteres_ok"]},
        )
        state = {"case_selected": "credit_consommation",
                 "extracted_params": {"montant": 20000, "duree_mois": 48, "revenu_mensuel": 3000}}

        result = odm_call_node(state)

        assert result["odm_decision"]["decision"] == "ELIGIBLE"
        assert result["rules_fired"] == ["regle_criteres_ok"]
        # pas de fallback en cas de succès
        assert result.get("fallback_type") is None

    def test_payload_bien_forme(self, mocker):
        # On vérifie que le nœud construit le BON payload pour Spring
        mock_call = mocker.patch(
            "app.graph.nodes.odm_call._call_odm",
            return_value={"decision": "APPROUVE", "rulesTriggered": []},
        )
        state = {"case_selected": "virement",
                 "extracted_params": {"type_virement": "standard", "montant": 500}}

        odm_call_node(state)

        # _call_odm doit avoir été appelé avec {caseName, params}
        payload_envoye = mock_call.call_args[0][0]  # 1er argument positionnel
        assert payload_envoye["caseName"] == "virement"
        assert payload_envoye["params"]["montant"] == 500


# ══════════════════════════════════════════════════════════════════════════════
# odm_call_node : cas d'ÉCHEC (Spring/ODM indisponible → Fallback E)
# ══════════════════════════════════════════════════════════════════════════════

class TestOdmIndisponible:


    # Vérifie que lorsque ODM ne répond plus après plusieurs tentatives,
    # le système bascule automatiquement vers le fallback E
    def test_retry_error_donne_fallback_e(self, mocker):

        # Simulation d'un échec définitif après l'épuisement des tentatives
        # de reconnexion effectuées par le mécanisme de retry (tenacity)
        fake_retry_error = RetryError(last_attempt=None)

        # Remplacement de l'appel réel à ODM par une exception simulée
        # pour reproduire un serveur ODM indisponible
        mocker.patch(
            "app.graph.nodes.odm_call._call_odm",
            side_effect=fake_retry_error,
        )
        state = {"case_selected": "credit_immobilier", "extracted_params": {}}

        result = odm_call_node(state)

        # Vérifie que le fallback E est déclenché lorsque ODM est inaccessible
        assert result["fallback_type"] == "E"
        # un message utilisateur doit être présent
        assert "response_text" in result



    # Vérifie que le système gère aussi les erreurs inattendues
    # provenant de l'appel ODM (exception non prévue)

    def test_erreur_inattendue_donne_fallback_e(self, mocker):

        # Simulation d'une erreur quelconque pendant l'appel ODM
        # Exemple : erreur réseau, erreur de parsing, bug serveur...

        mocker.patch(
            "app.graph.nodes.odm_call._call_odm",
            side_effect=ValueError("boom inattendu"),
        )
        state = {"case_selected": "virement", "extracted_params": {}}

        result = odm_call_node(state)

        assert result["fallback_type"] == "E"
