# === Destination : app/config.py (remplace l'existant) ===
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── PostgreSQL ────────────────────────────────────────────────────────
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # Pool de connexions (production : évite d'ouvrir une connexion par appel)
    db_pool_min: int = 1
    db_pool_max: int = 10

    # ── Hugging Face / modèles ────────────────────────────────────────────
    hf_token: str | None = None

    # ── Ollama ────────────────────────────────────────────────────────────
    ollama_base_url: str
    llm_model: str                      # modèle par défaut
    embedding_model: str
    embedding_dimensions: int = 768

    # Modèles dédiés par tâche (optionnels). Si None → fallback sur llm_model.
    # Idée : un petit modèle rapide pour classer/router, un plus gros pour la
    # reformulation client. Ex (.env) : CLASSIFIER_MODEL=qwen3:4b
    classifier_model: str | None = None   # routing / intent-check
    extraction_model: str | None = None   # extraction de paramètres
    generation_model: str | None = None   # reformulation finale

    # Timeouts (secondes)
    ollama_timeout_sec: float = 60.0
    ollama_keep_alive: str = "30m"


# ── Spring Boot / ODM ─────────────────────────────────────────────────
    spring_boot_url: str = "http://localhost:8080"
    odm_request_timeout_sec: float = 8.0    # timeout d'une requête ODM
    odm_retry_window_sec: float = 30.0      # fenêtre totale de retry
    odm_wait_min_sec: float = 1.0
    odm_wait_max_sec: float = 10.0



    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "ia-brms-orchestrator"
    langsmith_endpoint: str = "https://eu.api.smith.langchain.com"   # <-- nouveau




    # ── Divers ────────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    class Config:
        env_file = ".env"


settings = Settings()