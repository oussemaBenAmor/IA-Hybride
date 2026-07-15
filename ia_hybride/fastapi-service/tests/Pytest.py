[pytest]
# Dossier où pytest cherche les tests
testpaths = tests
# Motifs de découverte (par défaut, mais explicites ici pour l'apprentissage)
python_files = test_*.py
python_classes = Test*
python_functions = test_*
# Options par défaut : -v = verbeux (montre chaque test)
addopts = -v
    

