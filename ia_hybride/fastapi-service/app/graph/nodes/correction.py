import time
import unicodedata
from rapidfuzz import process, fuzz
from spellchecker import SpellChecker
from app.graph.state import GraphState

# ── Dictionnaire métier étendu ────────────────────────────────────────────────
BUSINESS_DICTIONARY = [
    "crédit", "immobilier", "prêt", "banque", "virement",
    "assurance", "carte", "bancaire", "mensualité", "taux",
    "intérêt", "remboursement", "épargne", "placement",
    "revenus", "salaire", "apport", "durée", "montant",
    "bénéficiaire", "iban", "opposition", "plafond", "visa",
    "mastercard", "consommation", "personnel", "voiture",
    "euros", "mois", "annuel", "mensuel", "emprunt", "financement",
    "retraite", "investissement", "patrimoine", "logement",
]

# Version sans accents du dictionnaire métier (pour comparaison normalisée)
_BUSINESS_DICT_NORMALIZED = [
    ''.join(
        c for c in unicodedata.normalize('NFD', w)
        if unicodedata.category(c) != 'Mn'
    )
    for w in BUSINESS_DICTIONARY
]

# Seuils de correction
SPELLCHECK_MIN_LEN  = 4     # mots trop courts ignorés
FUZZY_SCORE_CUTOFF  = 65    # score minimum RapidFuzz (sur 100)

spell = SpellChecker(language='fr')
spell.word_frequency.load_words(BUSINESS_DICTIONARY)


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _strip_accents(s: str) -> str:
    """Supprime les accents pour comparaison normalisée."""
    return ''.join(
        c for c in unicodedata.normalize('NFD', s)
        if unicodedata.category(c) != 'Mn'
    )


def _levenshtein_ratio(a: str, b: str) -> float:
    return fuzz.ratio(a, b) / 100.0


def correct_word(word):
    clean = word.lower().strip(".,;:!?\"'()")
    if len(clean) < SPELLCHECK_MIN_LEN:
        return word, False

    # ── Passe 1 : SpellChecker — candidat possible, pas encore retourné ──────
    sc_candidate = None
    sc_ratio = 0.0
    if clean not in spell:
        sc = spell.correction(clean)
        if sc and sc != clean:
            sc_ratio = fuzz.ratio(clean, sc) / 100
            if sc_ratio >= 0.55:
                sc_candidate = sc

    # ── Passe 2 : RapidFuzz métier sur version SANS ACCENTS ──────────────────
    # Exemple : "credoit" → normalisé "credoit"
    #           "crédit"  → normalisé "credit"  → ratio("credoit","credit") = 92%
    #           "redoit"  → normalisé "redoit"  → ratio("credoit","redoit") = 92%
    # On compare les formes sans accents, puis on retourne le mot AVEC accents.
    if clean not in spell:
        clean_norm = _strip_accents(clean)
        match = process.extractOne(
            clean_norm,
            _BUSINESS_DICT_NORMALIZED,
            scorer=fuzz.WRatio,
            score_cutoff=FUZZY_SCORE_CUTOFF
        )
        if match:
            _, score, idx = match
            biz_ratio = score / 100
            candidate_original = BUSINESS_DICTIONARY[idx]   # avec accents
            candidate_norm     = _BUSINESS_DICT_NORMALIZED[idx]

            # Ne retourner que si le candidat est différent du mot d'origine
            if candidate_norm != clean_norm:
                # Priorité au dictionnaire métier si son score >= SpellChecker
                if biz_ratio >= sc_ratio:
                    return candidate_original, True

    # ── Fallback : candidat SpellChecker si aucune correction métier ─────────
    if sc_candidate:
        return sc_candidate, True

    return word, False


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
        print(f"  [correction] '{text}' → '{corrected_text}'")

    return {
        **state,
        "input_corrected": corrected_text if correction_applied else text,
        "latency_ms":      {**state.get("latency_ms", {}), "correction": elapsed}
    }