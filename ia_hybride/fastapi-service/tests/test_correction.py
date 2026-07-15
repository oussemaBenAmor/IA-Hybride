"""
Tests des fonctions PURES de correction.py — correction orthographique métier.

On teste surtout correct_word(word) -> (mot_corrigé, a_été_corrigé) :
  - les vrais mots français ne sont PAS touchés
  - les termes métier connus ne sont PAS touchés
  - les fautes proches d'un terme métier SONT corrigées
  - les mots trop courts sont ignorés

Plus les petits utilitaires _strip_accents et normalize.
"""
import pytest

from app.graph.nodes.correction import (
    correct_word,
    _strip_accents,
    normalize,
    _is_real_french_word,
)


# ══════════════════════════════════════════════════════════════════════════════
# _strip_accents : retirer les accents (utilitaire)
# ══════════════════════════════════════════════════════════════════════════════

class TestStripAccents:

    @pytest.mark.parametrize("entree,attendu", [
        ("crédit", "credit"),
        ("intérêt", "interet"),
        ("durée", "duree"),
        ("virement", "virement"),   # sans accent → inchangé
    ])
    def test_strip(self, entree, attendu):
        assert _strip_accents(entree) == attendu


# ══════════════════════════════════════════════════════════════════════════════
# correct_word : le cœur de la correction
# ══════════════════════════════════════════════════════════════════════════════

class TestCorrectWordNeTouchePas:
    """Cas où le mot NE DOIT PAS être corrigé (2e élément = False)."""

    @pytest.mark.parametrize("mot", [
        "crédit", "virement", "assurance", "carte", "montant",
    ])
    def test_terme_metier_connu_intact(self, mot):
        # Un terme métier du dictionnaire ne doit jamais être modifié
        corrige, a_change = correct_word(mot)
        assert a_change is False
        assert corrige == mot

    @pytest.mark.parametrize("mot", [
        "veux", "avec", "pour", "dans", "mais",
    ])
    def test_vrai_mot_francais_intact(self, mot):
        # Un vrai mot français courant ne doit pas être "corrigé" par erreur
        _, a_change = correct_word(mot)
        assert a_change is False

    def test_mot_trop_court_ignore(self):
        # Les mots < 4 lettres sont laissés tels quels (trop ambigus)
        corrige, a_change = correct_word("de")
        assert a_change is False
        assert corrige == "de"


class TestCorrectWordCorrige:
    """Cas où le mot DOIT être corrigé vers un terme métier."""

    @pytest.mark.parametrize("faute,attendu", [
        ("assurence", "assurance"),  # faute de frappe
        ("virment", "virement"),     # lettre manquante
        ("imobilier", "immobilier"), # lettre manquante
        ("créditt", "crédit"),       # lettre en trop
        ("crédi", "crédit"),         # lettre manquante (avec accent)
    ])
    def test_faute_metier_corrigee(self, faute, attendu):
        # NOTE : une faute doit être VRAIMENT fautive. "credit" (sans accent) est
        # un mot français valide dans le corpus → le code le PROTÈGE volontairement
        # et ne le corrige pas. On teste donc des fautes non ambiguës.
        corrige, a_change = correct_word(faute)
        assert corrige.lower() == attendu, f"{faute!r} aurait dû devenir {attendu!r}, obtenu {corrige!r}"
        assert a_change is True

    def test_mot_francais_sans_accent_protege(self):
        # "credit" sans accent EXISTE comme mot français → le code ne le corrige PAS.
        # Ce test documente ce choix de conception (éviter la sur-correction).
        corrige, a_change = correct_word("credit")
        assert a_change is False


# ══════════════════════════════════════════════════════════════════════════════
# _is_real_french_word : détection d'un vrai mot français
# ══════════════════════════════════════════════════════════════════════════════

class TestIsRealFrenchWord:

    def test_mot_courant_reconnu(self):
        # "maison" est un mot français fréquent
        assert _is_real_french_word("maison", "maison") is True

    def test_charabia_non_reconnu(self):
        # une suite de lettres sans sens n'est pas un mot français
        assert _is_real_french_word("xqzptr", "xqzptr") is False
