from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

class Settings(BaseSettings):
    # ── PostgreSQL ────────────────────────────────────────────────────────
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # Pool de connexions (pour éviter d'ouvrir une connexion par appel)
    db_pool_min: int = 1
    db_pool_max: int = 10


    # ── Ollama ────────────────────────────────────────────────────────────
    ollama_base_url: str
    llm_model: str                      # modèle par défaut
    embedding_model: str
    embedding_dimensions: int = 768

    # Modèles dédiés par tâche (optionnels). Si None → fallback sur llm_model.
    classifier_model: str | None = None   # routing / intent-check
    extraction_model: str | None = None   # extraction de paramètres
    generation_model: str | None = None   # reformulation finale

    # Timeouts (secondes)
    ollama_timeout_sec: float = 60.0
    ollama_keep_alive: str = "30m"


    # ── Spring Boot / ODM ─────────────────────────────────────────────────
    spring_boot_url: str = "http://localhost:8081"
    odm_request_timeout_sec: float = 8.0    # timeout d'une requête ODM
    odm_retry_window_sec: float = 30.0      # fenêtre totale de retry
    odm_wait_min_sec: float = 1.0    #on attend au minimum 1 seconde avant de réessayer.
    odm_wait_max_sec: float = 10.0   #on n'attend pas plus de 10 secondes entre deux tentatives surtout lorsequ'on utilise le backoff exponentiel.



    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "ia-brms-orchestrator"
    langsmith_endpoint: str = "https://eu.api.smith.langchain.com"


    # ── Authentification JWT ──────────────────────────────────────────────
    # jwt_secret : DOIT être défini dans le .env en production (chaîne longue et aléatoire).
    jwt_secret: str = "CHANGE-ME-en-prod-mettre-une-longue-chaine-aleatoire-de-64-caracteres"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480   # durée de validité du jeton (8 heures)

    # URL interne utilisée par le harnais d'exécution des scénarios de test
    # (le backend s'appelle lui-même sur /chat pendant l'exécution).
    self_api_url: str = "http://localhost:8000"



    eval_username: str = "admin"
    eval_password: str | None = None

# ── Divers ────────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent.parent / ".env"
    )


settings = Settings()