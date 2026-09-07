"""
conftest.py : fichier spécial pytest, chargé automatiquement AVANT les tests.
Ici il sert à ajouter la racine du projet au PYTHONPATH pour que les imports
`from app.graph.nodes...` fonctionnent quand on lance pytest.
"""
import sys
from pathlib import Path

# Racine du projet = dossier parent de tests/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
