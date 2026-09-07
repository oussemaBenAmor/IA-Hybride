# indique que les paramètres qui suivent appartiennent à la configuration du framework pytest
[pytest]



# Dossier où pytest cherche les tests
testpaths = tests



# Motifs de découverte
python_files = test_*.py
python_classes = Test*
python_functions = test_*



# Options par défaut : -v = verbeux (montre chaque test)
addopts = -v
    

