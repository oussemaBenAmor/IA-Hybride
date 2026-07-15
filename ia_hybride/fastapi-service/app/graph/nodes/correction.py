"""
Nœud de correction orthographique hybride :
- protège les vrais mots français
- corrige les fautes avec SymSpell
- vérifie le vocabulaire bancaire avec RapidFuzz

Flux de correction :
1. wordfreq vérifie si le mot est un vrai mot français → protection contre les fausses corrections.
2. SymSpell cherche une correction rapide basée sur la distance d'édition.
3. RapidFuzz vérifie les correspondances métier (banque) avec un score de confiance élevé.

Les mots incertains sont conservés pour éviter la sur-correction.
"""
import re
import time
import logging
import unicodedata

import wordfreq
from rapidfuzz import process, fuzz
from symspellpy import SymSpell, Verbosity

from app.graph.state import GraphState

# Logger du nœud de correction
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

# Version sans accents pour reconnaître "credit" comme "crédit"

_BUSINESS_DICT_NORMALIZED = [
    ''.join(c for c in unicodedata.normalize('NFD', w) if unicodedata.category(c) != 'Mn')
    for w in BUSINESS_DICTIONARY
]


MIN_LEN           = 4    # Ignore les mots trop courts (trop ambigus)

SYMSPELL_MAX_EDIT = 2    # Nombre maximum de modifications autorisées par SymSpell

BIZ_ACCEPT_CUTOFF = 80  # Score minimum RapidFuzz pour accepter une correction bancaire


BIZ_FLOOR         = 60   # Score minimum pour considérer un candidat métier

FR_MIN_ZIPF       = 2.5  # Fréquence minimale pour considérer un mot comme français valide


FR_VOCAB_SIZE     = 40000  # nombre de mots FR les plus fréquents injectés dans SymSpell
BIZ_FREQ          = 5000   # Poids des mots métier dans SymSpell
BIZ_FREQ_NOACCENT = 4000   # Poids des mots métier sans accents

# Reconnaît les mots contenant lettres, accents, apostrophes et tirets
_WORD_RE = re.compile(r"[a-zàâäéèêëïîôöùûüÿçœæ'-]+")


# ── Initialisation SymSpell depuis wordfreq (en mémoire) ──────────────────────
def _build_symspell() -> SymSpell:
    #utilise les 7 premiers caractères du mot pour accélérer la recherche.
    sym = SymSpell(max_dictionary_edit_distance=SYMSPELL_MAX_EDIT, prefix_length=7)

    # Chargement des mots français fréquents depuis wordfreq

    top = wordfreq.top_n_list("fr", FR_VOCAB_SIZE)
    added = 0
    for i, w in enumerate(top):
        if len(w) >= 2 and _WORD_RE.fullmatch(w):
            sym.create_dictionary_entry(w, FR_VOCAB_SIZE - i)
            added += 1
    logger.info("dictionnaire FR construit depuis wordfreq (%d mots, aucun fichier)", added)

    # Vocabulaire métier ajouté par-dessus, à fréquence modérée
    for word in BUSINESS_DICTIONARY:
        sym.create_dictionary_entry(word.lower(), BIZ_FREQ)
    for word in _BUSINESS_DICT_NORMALIZED:
        sym.create_dictionary_entry(word.lower(), BIZ_FREQ_NOACCENT)

    return sym

# Dictionnaire SymSpell chargé une seule fois au démarrage
_sym = _build_symspell()
_BIZ_SET = {w.lower() for w in BUSINESS_DICTIONARY} | {w.lower() for w in _BUSINESS_DICT_NORMALIZED}

# Uniformise les caractères Unicode
def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)

# Supprime les accents
def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')


# Vérifie si le mot existe en français
def _is_real_french_word(clean: str, clean_norm: str) -> bool:

    if wordfreq.zipf_frequency(clean, "fr") >= FR_MIN_ZIPF:
        return True
    if clean_norm != clean and wordfreq.zipf_frequency(clean_norm, "fr") >= FR_MIN_ZIPF:
        return True
    return False


# Cherche le mot métier le plus ressemblant
def _biz_best(clean_norm: str):
    match = process.extractOne(clean_norm, _BUSINESS_DICT_NORMALIZED,
                               scorer=fuzz.WRatio, score_cutoff=BIZ_FLOOR)
    if not match:
        return None, 0
    _, score, idx = match
    return BUSINESS_DICTIONARY[idx], score


# Correction d'un seul mot

def correct_word(word: str) -> tuple[str, bool]:
    # Nettoyage du mot
    clean = word.lower().strip(".,;:!?\"'()")

    # Ignore les mots courts
    if len(clean) < MIN_LEN:
        return word, False

    # Les mots métier connus restent inchangés
    if clean in _BIZ_SET:
        return word, False

    # Création de la version sans accents
    clean_norm = _strip_accents(clean)

    # ── Étape 0 : vrai mot français → on n'y touche PAS ──────
    if _is_real_french_word(clean, clean_norm):
        return word, False

    # ── Étage 1 : SymSpell (mot inconnu) ──────────────────────────────────────
    suggestions = _sym.lookup(clean_norm, Verbosity.CLOSEST,
                              max_edit_distance=SYMSPELL_MAX_EDIT)
    sym_candidate = None
    if suggestions:
        # Meilleure proposition SymSpell
        best_term = suggestions[0].term
        # Si c'est un mot bancaire, garder la forme avec accent
        if best_term != clean_norm:
            if best_term in _BUSINESS_DICT_NORMALIZED:
                sym_candidate = BUSINESS_DICTIONARY[_BUSINESS_DICT_NORMALIZED.index(best_term)]
            else:
                sym_candidate = best_term

    # Vérification métier avec RapidFuzz
    biz_word, biz_score = _biz_best(clean_norm)

    # Correction uniquement si confiance élevée
    if biz_word and biz_score >= BIZ_ACCEPT_CUTOFF and _strip_accents(biz_word.lower()) != clean_norm:
        return biz_word, True

    # Sinon utiliser la correction SymSpell
    if sym_candidate and sym_candidate.lower() != clean:
        return sym_candidate, True

    # ── Rien de sûr → mot laissé tel quel ─────────────────────────────────────
    return word, False


# ── Nœud LangGraph ───────────────────────────────────────────────────────────


def correction_node(state: GraphState) -> GraphState:
    start = time.time()

    # Récupération et normalisation du texte utilisateur
    text  = normalize(state["input_raw"])
    # Séparation en mots
    words = text.split()

    corrected_words    = []
    correction_applied = False

    # Correction mot par mot
    for word in words:
        corrected, changed = correct_word(word)
        corrected_words.append(corrected)
        if changed:
            correction_applied = True

    # Reconstruction de la phrase
    corrected_text = " ".join(corrected_words)
    elapsed = (time.time() - start) * 1000

    if correction_applied:
        logger.info("[correction] '%s' → '%s'", text, corrected_text)

    return {
        **state,
        "input_corrected": corrected_text if correction_applied else text,
        "latency_ms":      {**state.get("latency_ms", {}), "correction": elapsed},
    }