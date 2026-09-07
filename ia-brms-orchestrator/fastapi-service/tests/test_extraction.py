"""
Tests des fonctions PURES d'extraction.py — en particulier l'anti-hallucination
des nombres (couche 1), qui est la protection la plus critique du système.

On teste ici uniquement les fonctions qui n'appellent PAS le LLM :
  - _clean_params          : nettoyage des valeurs parasites ("null", "none"...)
  - _numbers_in_text       : extraction des nombres réels d'un texte
  - _is_number_grounded    : un nombre est-il présent dans le texte ?
  - _drop_hallucinated_numbers : la couche 1 complète (annule inventions/troncatures)

"""
import pytest

from app.graph.nodes.extraction import (
    _clean_params,
    _numbers_in_text,
    _is_number_grounded,
    _drop_hallucinated_numbers,
)


# ══════════════════════════════════════════════════════════════════════════════
# _clean_params : convertit les valeurs parasites du LLM en None
# ══════════════════════════════════════════════════════════════════════════════

class TestCleanParams:
    """Le LLM renvoie parfois la CHAÎNE 'null' au lieu d'un vrai vide."""

    def test_chaine_null_devient_none(self):
        # Arrange : le LLM a mis la chaîne "null" au lieu de rien
        params = {"montant": "null"}
        # Act
        resultat = _clean_params(params)
        # Assert : "null" doit être converti en None (vrai vide)
        assert resultat["montant"] is None

    def test_valeurs_valides_conservees(self):
        # Une vraie valeur ne doit pas être touchée
        params = {"montant": 5000, "operation": "souscription"}
        resultat = _clean_params(params)
        assert resultat["montant"] == 5000
        assert resultat["operation"] == "souscription"

    def test_dict_vide(self):
        # Cas limite : dictionnaire vide → dictionnaire vide (pas d'erreur)
        assert _clean_params({}) == {}

    def test_none_en_entree(self):
        # Cas limite : None en entrée → dict vide (robustesse)
        assert _clean_params(None) == {}

    @pytest.mark.parametrize("valeur_parasite", [
        "null", "none", "nan", "n/a", "aucun", "inconnu", "",
        "NULL", "None", "Non spécifié",   # insensible à la casse
    ])
    def test_toutes_les_valeurs_parasites(self, valeur_parasite):
        # Un seul test, exécuté pour CHAQUE valeur parasite de la liste.
        # C'est la puissance de parametrize : 9 tests écrits en un.
        params = {"champ": valeur_parasite}
        resultat = _clean_params(params)
        assert resultat["champ"] is None, f"'{valeur_parasite}' aurait dû devenir None"


# ══════════════════════════════════════════════════════════════════════════════
# _numbers_in_text : extraire les nombres réellement présents dans un texte
# ══════════════════════════════════════════════════════════════════════════════

class TestNumbersInText:
    """Détection des nombres d'un texte, avec gestion des séparateurs de milliers."""

    def test_nombre_simple(self):
        # "200000" doit être détecté
        assert 200000 in _numbers_in_text("montant 200000 euros")

    def test_plusieurs_nombres(self):
        # tous les nombres du texte doivent être là
        nums = _numbers_in_text("credit 200000 sur 240 mois revenu 5000")
        assert 200000 in nums
        assert 240 in nums
        assert 5000 in nums

    def test_texte_sans_nombre(self):
        # aucun nombre → ensemble vide
        assert _numbers_in_text("virement à mon frère") == set()

    def test_texte_vide(self):
        # cas limite : chaîne vide → ensemble vide (pas d'erreur)
        assert _numbers_in_text("") == set()

    @pytest.mark.parametrize("texte,nombre_attendu", [
        ("200 000 euros", 200000),      # séparateur espace
        ("200.000 euros", 200000),      # séparateur point
        ("8 000 par mois", 8000),       # séparateur espace, autre nombre
        ("age 35 ans", 35),             # petit nombre sans séparateur
        ("montant 1000", 1000),         # nombre rond
    ])
    def test_separateurs_de_milliers(self, texte, nombre_attendu):
        # Vérifie que "8 000" et "8.000" sont bien lus comme 8000 (et non 8 et 000)
        assert nombre_attendu in _numbers_in_text(texte)


# ══════════════════════════════════════════════════════════════════════════════
# _is_number_grounded : un nombre est-il "ancré" (présent) dans le texte ?
# ══════════════════════════════════════════════════════════════════════════════

class TestIsNumberGrounded:
    """Cœur de l'anti-hallucination : un nombre est valide s'il est dans le texte."""

    def test_nombre_present_est_ancre(self):
        # 5000 est dans l'ensemble → ancré (True)
        text_numbers = {5000, 240}
        assert _is_number_grounded(5000, text_numbers) is True

    def test_nombre_absent_non_ancre(self):
        # 10000 n'est PAS dans l'ensemble → non ancré (False)
        text_numbers = {5000, 240}
        assert _is_number_grounded(10000, text_numbers) is False

    def test_troncature_detectee(self):
        # LE CAS T02 : le texte contient 8000, le LLM a mis 800.
        # 800 n'est PAS dans {8000} → non ancré → sera annulé. C'est le but.
        text_numbers = {8000}
        assert _is_number_grounded(800, text_numbers) is False


# Même si le texte contient 5000, une valeur absente (None) doit être acceptée.
    def test_none_toujours_ancre(self):
        # None = absence légitime, toujours "ancré" (on ne l'annule pas)
        assert _is_number_grounded(None, {5000}) is True

    def test_valeur_texte_non_concernee(self):
        # Un IBAN (chaîne non numérique) n'est pas soumis à l'ancrage → True
        assert _is_number_grounded("FR7612345", {5000}) is True


# Tests de bout en bout de la protection anti-hallucination numérique :
# les valeurs numériques non présentes dans le texte sont annulées,
# tandis que les valeurs correctement extraites sont conservées.

class TestDropHallucinatedNumbers:
    """Tests de bout en bout de la couche anti-hallucination.
    """

    def test_montant_invente_annule(self):
        # BUG T85 : "virement à mon frère" (aucun montant) mais le LLM invente 10000.
        # La couche 1 doit l'annuler → None → la validation le réclamera.
        params = {"montant": 10000}
        texte = "je veux faire un virement à mon frère"
        resultat = _drop_hallucinated_numbers(params, texte)
        assert resultat["montant"] is None

    def test_revenu_tronque_annule(self):
        # BUG T02 : le texte dit "revenu 8000" mais le LLM a mis 800 (zéro perdu).
        # 800 absent du texte → annulé.
        params = {"revenu_mensuel": 800}
        texte = "credit immobilier 300000 sur 300 mois revenu 8000 euros"
        resultat = _drop_hallucinated_numbers(params, texte)
        assert resultat["revenu_mensuel"] is None

    def test_valeur_correcte_conservee(self):
        # Une valeur réellement présente ne doit PAS être annulée
        params = {"montant": 200000}
        texte = "credit de 200000 euros"
        resultat = _drop_hallucinated_numbers(params, texte)
        assert resultat["montant"] == 200000

    def test_plusieurs_champs_mixtes(self):
        # Mélange réaliste : certains ancrés (conservés), un inventé (annulé)
        params = {
            "montant": 200000,      # présent → conservé
            "duree_mois": 240,      # présent → conservé
            "revenu_mensuel": 999,  # ABSENT → annulé
        }
        texte = "credit 200000 sur 240 mois"
        resultat = _drop_hallucinated_numbers(params, texte)
        assert resultat["montant"] == 200000
        assert resultat["duree_mois"] == 240
        assert resultat["revenu_mensuel"] is None

# vérifier que cette fonction protège uniquement contre les hallucinations de type nombre métier.
    def test_champ_texte_jamais_annule(self):
        # Un IBAN n'est pas un champ numérique → jamais annulé, même absent
        params = {"iban": "FR7612345678901234567890123"}
        texte = "virement à mon frère"
        resultat = _drop_hallucinated_numbers(params, texte)
        assert resultat["iban"] == "FR7612345678901234567890123"

    def test_separateur_milliers_conserve(self):
        # "8 000" dans le texte, le LLM extrait 8000 → doit être conservé
        params = {"revenu_mensuel": 8000}
        texte = "je gagne 8 000 euros par mois"
        resultat = _drop_hallucinated_numbers(params, texte)
        assert resultat["revenu_mensuel"] == 8000

    def test_ne_modifie_pas_original(self):
        # Bonne pratique : la fonction renvoie un NOUVEAU dict, sans muter l'entrée
        params = {"montant": 10000}
        texte = "virement à mon frère"
        _drop_hallucinated_numbers(params, texte)
        # l'original ne doit pas avoir changé
        assert params["montant"] == 10000
