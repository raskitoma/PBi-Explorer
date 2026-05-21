"""Settings & .env IO. Karpathy: surface assumptions, fail loud on bad config."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AzureCloud = Literal["commercial", "gcc-high", "dod", "china"]


class Settings(BaseSettings):
    # Read .env first (compose-level + host-level config); then layer the
    # runtime overrides file written by the dashboard /config save handler.
    # Both files are optional; pydantic-settings silently skips missing ones.
    model_config = SettingsConfigDict(
        env_file=(".env", "/data/m365ai-overrides.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Cloud
    azure_cloud: AzureCloud = "commercial"
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: SecretStr = SecretStr("")
    m365_redirect_uri: str = "http://localhost:8080/m365/callback"

    # DB
    db_host: str = "db"
    db_port: int = 3306
    db_name: str = "m365_audit"
    db_user: str = "m365"
    db_pass: SecretStr = SecretStr("")

    # Dashboard
    dashboard_user: str = "admin"
    dashboard_pass_hash: str = ""
    app_secret_key: SecretStr = SecretStr("dev-insecure")
    web_port: int = 8080

    # OIDC
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: SecretStr = SecretStr("")
    oidc_redirect_uri: str = ""
    oidc_required_group: str = ""
    oidc_username_claim: str = "preferred_username"
    local_login_enabled: bool = True

    # Ingest
    # Default OFF so a fresh deploy never starts writing rows before the
    # operator has reviewed mappings via /discover + /mapping dry-run.
    # Flip via the /config GUI when ready.
    ingest_enabled: bool = False
    poll_interval_s: int = 300
    graph_lookback_hours: int = 24
    mgmt_content_types: str = (
        "Audit.SharePoint,Audit.Exchange,Audit.AzureActiveDirectory,Audit.General"
    )

    # TZ
    tz: str = "UTC"

    # Dev escape hatch
    force_insecure: bool = False

    @field_validator("azure_cloud")
    @classmethod
    def _check_cloud(cls, v: str) -> str:
        if v not in {"commercial", "gcc-high", "dod", "china"}:
            raise ValueError(f"unknown AZURE_CLOUD: {v}")
        return v

    @property
    def db_dsn(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_pass.get_secret_value()}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    @property
    def mgmt_content_type_list(self) -> list[str]:
        return [s.strip() for s in self.mgmt_content_types.split(",") if s.strip()]

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer_url)


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings()
    return _settings


def atomic_write_env(path: Path, kv: dict[str, str]) -> None:
    """Atomic .env writer used by /config edits.

    Values are single-quoted so docker compose does not try to interpolate
    any '$' characters inside them (e.g. inside a PBKDF2 password hash).
    python-dotenv strips the quotes on read, so consumers see the raw value.
    """
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write("# updated by app at runtime\n")
            for k in sorted(kv):
                v = str(kv[k])
                if "'" in v:
                    raise ValueError(
                        f"value for {k} contains a single quote — cannot safely "
                        "encode in .env"
                    )
                f.write(f"{k}='{v}'\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
