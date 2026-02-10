from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    bind_host: str = "0.0.0.0"
    bind_port: int = 8444
    signal_cli_socket: str = "/var/run/signal-cli/socket"
    log_dir: str = "/var/log/mesh-proxy"


def load_settings() -> Settings:
    return Settings(
        bind_host=os.getenv("MESH_BIND_HOST", "0.0.0.0"),
        bind_port=int(os.getenv("MESH_BIND_PORT", "8444")),
        signal_cli_socket=os.getenv("SIGNAL_CLI_SOCKET", "/var/run/signal-cli/socket"),
        log_dir=os.getenv("MESH_LOG_DIR", "/var/log/mesh-proxy"),
    )


def ensure_log_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)
