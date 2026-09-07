# Conseiller Bancaire Hybride — LLM + BRMS (IBM ODM)

Assistant conversationnel bancaire couplant un **modèle de langage (LLM)** à un
**moteur de règles métier (IBM ODM)**. Le LLM comprend la demande et en extrait
les paramètres ; **la décision finale est produite exclusivement par le moteur
de règles**, ce qui garantit des décisions **déterministes, traçables et
auditables** et réduit les hallucinations.

Projet réalisé dans le cadre d'un stage de fin d'études (M2 Systèmes Intelligents
et Applications — Université Gustave Eiffel).

---

## Architecture

Le système suit une architecture à trois niveaux :

- **Frontend (Streamlit)** — interface de chat, historique des conversations
  (utilisateur), panneau d'explicabilité XAI et dashboard (administrateur).
- **Orchestration (FastAPI + LangGraph)** — pipeline de traitement : sécurité →
  correction orthographique → routage → extraction → validation → appel ODM →
  génération → audit. Gère l'authentification (JWT) et la conversation multi-tours.
- **Passerelle règles (Spring Boot → IBM ODM)** — expose un point de décision
  unique, appelle le moteur de règles via HTDS et renvoie la décision et les
  règles déclenchées.

Base de données : **PostgreSQL + pgvector** (routage sémantique, trace d'audit,
état des conversations). LLM et embeddings servis localement via **Ollama**.

---

## Prérequis

- Python 3.11
- PostgreSQL avec l'extension **pgvector**
- **Ollama** avec les modèles requis (LLM + embeddings)
- La passerelle **Spring Boot** et **IBM ODM** en fonctionnement (voir le dépôt ODM)

---

## Installation

```bash
# 1. Créer et activer un environnement Python
conda create -n orchestration-ia python=3.11
conda activate orchestration-ia

# 2. Installer les dépendances
pip install -r requirements.txt
```

Créer un fichier `.env` dans `fastapi-service/` (voir la section Configuration).

---

## Configuration (.env)

Le fichier `.env` (dans `fastapi-service/`) n'est **pas** versionné. Il contient :

```env
# PostgreSQL
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_USER=postgres
POSTGRES_PASSWORD=votre_mot_de_passe
POSTGRES_DB=ia_brms

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
LLM_MODEL=qwen3:8b
EMBEDDING_MODEL=nomic-embed-text

# Authentification JWT
JWT_SECRET=une_longue_chaine_aleatoire

# Identifiants du script d'évaluation
EVAL_USERNAME=admin
EVAL_PASSWORD=admin123
```

---

## Lancement

```bash
# 1. Backend (depuis fastapi-service/)
cd fastapi-service
uvicorn app.main:app --reload

# 2. Frontend (depuis frontend/)
cd frontend
streamlit run app.py
```

Compte administrateur par défaut : `admin` / `admin123`.

---

## Tests

```bash
# Depuis fastapi-service/
pytest                                  # exécuter les tests unitaires
pytest --cov=app --cov-report=term-missing   # avec la couverture de code
```

---

## Évaluation

Le dossier `evaluation/` contient un script comparant le système hybride à un
LLM seul, et générant des rapports Excel.

```bash
cd evaluation
python evaluation.py all
```

Le taux de réussite des scénarios de test peut aussi être mesuré directement
depuis le dashboard administrateur (téléversement d'un fichier CSV/Excel).

---

## Structure du projet

```
ia-brms-orchestrator/
├── requirements.txt
├── README.md
├── fastapi-service/       # backend (FastAPI + LangGraph)
│   ├── app/
│   └── tests/
├── frontend/              # interface Streamlit
└── evaluation/            # scripts d'évaluation et de comparaison
```

---

## Auteur

Oussema Ben Amor — Stage de fin d'études, Pacte Novation (2025–2026).
