import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bind_host: str = "0.0.0.0"
    bind_port: int = 8443
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3"
    ollama_num_ctx: int = 0  # 0 = use model default
    mesh_url: str = "http://mesh:8444"
    log_level: str = "INFO"


def load_settings() -> Settings:
    return Settings(
        bind_host=os.getenv("JOI_BIND_HOST", "0.0.0.0"),
        bind_port=int(os.getenv("JOI_BIND_PORT", "8443")),
        ollama_url=os.getenv("JOI_OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("JOI_OLLAMA_MODEL", "llama3"),
        ollama_num_ctx=int(os.getenv("JOI_OLLAMA_NUM_CTX", "0")),
        mesh_url=os.getenv("JOI_MESH_URL", "http://mesh:8444"),
        log_level=os.getenv("JOI_LOG_LEVEL", "INFO"),
    )
