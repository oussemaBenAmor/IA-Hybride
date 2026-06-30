# === Destination : app/graph/nodes/correction.py (remplace l'existant) ===
"""
Nœud de correction orthographique à DEUX étages, rapide et 100% déterministe.

AUCUN FICHIER EXTERNE À FOURNIR : le dictionnaire français provient de la
bibliothèque `wordfreq` (données embarquées). Il suffit de `pip install wordfreq`.

Philosophie : corriger les fautes de frappe qui gênent le routing bancaire, SANS
sur-corriger ni les mots français valides ("veux" ne doit PAS devenir "taux"),
ni les mots hors-domaine ("users" ne doit pas devenir "sers").

  Étape 0 — Si le mot est un VRAI mot français (fréquence wordfreq suffisante),
            on n'y touche pas. C'est ce qui protège "veux", "maison", "voudrais".
  Étage 1 — SymSpell : correction lexicale rapide (Damerau-Levenshtein optimisé),
            dictionnaire construit EN MÉMOIRE depuis wordfreq + vocabulaire métier.
  Étage 2 — RapidFuzz métier : rattrape les fautes propres au domaine bancaire
            ("credot" → "crédit") avec un seuil de confiance élevé.

Garde-fou anti sur-correction : un mot inconnu qui ne ressemble fortement à rien
(ni correction SymSpell nette, ni terme métier >= 80%) est laissé TEL QUEL.
"""
import re
import time
import logging
import unicodedata

import wordfreq
from rapidfuzz import process, fuzz
from symspellpy import SymSpell, Verbosity

from app.graph.state import GraphState

logger = logging.getLogger("correction")

# ── Dictionnaire métier ───────────────────────────────────────────────────────
BUSINESS_DICTIONARY = [
    "crédit", "immobilier", "prêt", "banque", "virement",
    "assurance", "carte", "bancaire", "mensualité", "taux",
    "intérêt", "remboursement", "épargne", "placement",
    "revenus", "salaire", "apport", "durée", "montant",
    "bénéficiaire", "iban", "opposition", "plafond", "visa",
    "mastercard", "consommation", "personnel", "voiture",
    "euros", "mois", "annuel", "mensuel", "emprunt", "financement",
    "retraite", "investissement", "patrimoine", "logement",
    "souscription", "rachat", "versement", "déblocage", "renouvellement",
]

_BUSINESS_DICT_NORMALIZED = [
    ''.join(c for c in unicodedata.normalize('NFD', w) if unicodedata.category(c) != 'Mn')
    for w in BUSINESS_DICTIONARY
]

# ══════════════════════════════════════════════════════════════════════════════
# SEUILS — chacun justifié
# ══════════════════════════════════════════════════════════════════════════════
MIN_LEN           = 4    # mots < 4 lettres laissés tels quels (trop ambigus)

SYMSPELL_MAX_EDIT = 2    # 2 éditions = standard (~95% des fautes réelles sont à
# distance 1 ou 2). Au-delà → faux positifs.

BIZ_ACCEPT_CUTOFF = 80   # RapidFuzz : correction métier acceptée seulement si
# ressemblance >= 80/100 (à 65, "users" matchait par hasard).

BIZ_FLOOR         = 60   # en dessous, on ne retient même pas le candidat métier.

FR_MIN_ZIPF       = 2.5  # Seuil de "vrai mot français" via wordfreq.zipf_frequency.
# L'échelle Zipf va ~1 (très rare) à ~7 (très courant).
# 2.5 = mot raisonnablement courant. "veux"=5.6, "maison"=5.5
# → protégés. "users"=2.67 est juste au-dessus mais ne
# ressemble à aucun mot métier → laissé tel quel de toute façon.
# "credot"/"imobilier"=0 → inconnus → corrigés.

FR_VOCAB_SIZE     = 40000  # nombre de mots FR les plus fréquents injectés dans SymSpell
BIZ_FREQ          = 5000   # fréquence MODÉRÉE du métier (ne pas écraser le français)
BIZ_FREQ_NOACCENT = 4000

_WORD_RE = re.compile(r"[a-zàâäéèêëïîôöùûüÿçœæ'-]+")


# ── Initialisation SymSpell depuis wordfreq (en mémoire) ──────────────────────
def _build_symspell() -> SymSpell:
    sym = SymSpell(max_dictionary_edit_distance=SYMSPELL_MAX_EDIT, prefix_length=7)

    # Dictionnaire français : les N mots les plus fréquents, fréquence = rang inversé.
    top = wordfreq.top_n_list("fr", FR_VOCAB_SIZE)
    added = 0
    for i, w in enumerate(top):
        if len(w) >= 2 and _WORD_RE.fullmatch(w):
            sym.create_dictionary_entry(w, FR_VOCAB_SIZE - i)
            added += 1
    logger.info("dictionnaire FR construit depuis wordfreq (%d mots, aucun fichier)", added)

    # Vocabulaire métier ajouté par-dessus, à fréquence MODÉRÉE
    for word in BUSINESS_DICTIONARY:
        sym.create_dictionary_entry(word.lower(), BIZ_FREQ)
    for word in _BUSINESS_DICT_NORMALIZED:
        sym.create_dictionary_entry(word.lower(), BIZ_FREQ_NOACCENT)

    return sym


_sym = _build_symspell()
_BIZ_SET = {w.lower() for w in BUSINESS_DICTIONARY} | {w.lower() for w in _BUSINESS_DICT_NORMALIZED}


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


def _is_real_french_word(clean: str, clean_norm: str) -> bool:
    """Vrai mot français ? On teste la forme exacte ET la forme sans accents
    (l'utilisateur tape souvent sans accents : "credit" → "crédit" existe)."""
    if wordfreq.zipf_frequency(clean, "fr") >= FR_MIN_ZIPF:
        return True
    if clean_norm != clean and wordfreq.zipf_frequency(clean_norm, "fr") >= FR_MIN_ZIPF:
        return True
    return False


def _biz_best(clean_norm: str):
    match = process.extractOne(clean_norm, _BUSINESS_DICT_NORMALIZED,
                               scorer=fuzz.WRatio, score_cutoff=BIZ_FLOOR)
    if not match:
        return None, 0
    _, score, idx = match
    return BUSINESS_DICTIONARY[idx], score


# ══════════════════════════════════════════════════════════════════════════════
# Correction d'UN mot
# ══════════════════════════════════════════════════════════════════════════════

def correct_word(word: str) -> tuple[str, bool]:
    clean = word.lower().strip(".,;:!?\"'()")
    if len(clean) < MIN_LEN:
        return word, False

    # Déjà un terme métier connu → ne pas toucher
    if clean in _BIZ_SET:
        return word, False

    clean_norm = _strip_accents(clean)

    # ── Étape 0 : vrai mot français → on n'y touche PAS (protège "veux") ──────
    if _is_real_french_word(clean, clean_norm):
        return word, False

    # ── Étage 1 : SymSpell (mot inconnu) ──────────────────────────────────────
    suggestions = _sym.lookup(clean_norm, Verbosity.CLOSEST,
                              max_edit_distance=SYMSPELL_MAX_EDIT)
    sym_candidate = None
    if suggestions:
        best_term = suggestions[0].term
        if best_term != clean_norm:
            if best_term in _BUSINESS_DICT_NORMALIZED:
                sym_candidate = BUSINESS_DICTIONARY[_BUSINESS_DICT_NORMALIZED.index(best_term)]
            else:
                sym_candidate = best_term

    # ── Étage 2 : RapidFuzz métier (haute confiance prioritaire) ──────────────
    biz_word, biz_score = _biz_best(clean_norm)
    if biz_word and biz_score >= BIZ_ACCEPT_CUTOFF and _strip_accents(biz_word.lower()) != clean_norm:
        return biz_word, True

    if sym_candidate and sym_candidate.lower() != clean:
        return sym_candidate, True

    # ── Rien de sûr → mot laissé tel quel ─────────────────────────────────────
    return word, False


# ══════════════════════════════════════════════════════════════════════════════
# Nœud
# ══════════════════════════════════════════════════════════════════════════════

def correction_node(state: GraphState) -> GraphState:
    start = time.time()
    text  = normalize(state["input_raw"])
    words = text.split()

    corrected_words    = []
    correction_applied = False

    for word in words:
        corrected, changed = correct_word(word)
        corrected_words.append(corrected)
        if changed:
            correction_applied = True

    corrected_text = " ".join(corrected_words)
    elapsed = (time.time() - start) * 1000

    if correction_applied:
        logger.info("[correction] '%s' → '%s'", text, corrected_text)

    return {
        **state,
        "input_corrected": corrected_text if correction_applied else text,
        "latency_ms":      {**state.get("latency_ms", {}), "correction": elapsed},
    }