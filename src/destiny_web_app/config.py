"""Environment-backed application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


class ConfigurationError(RuntimeError):
    """Raised when a requested feature is not configured."""


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    host: str
    port: int
    ssl_certificate: Path
    ssl_key: Path
    database_path: Path
    bungie_client_id: str
    bungie_client_secret: str = field(repr=False)
    bungie_api_key: str = field(repr=False)
    bungie_redirect_uri: str
    session_cookie_name: str
    inventory_stale_seconds: int
    manifest_path: Path
    manifest_language: str

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            environment=os.getenv("DESTINY_ENV", "development"),
            host=os.getenv("DESTINY_HOST", "localhost"),
            port=_read_port(os.getenv("DESTINY_PORT", "42697")),
            ssl_certificate=_read_path(
                "DESTINY_SSL_CERT_PATH",
                "util/CERT.pem",
            ),
            ssl_key=_read_path(
                "DESTINY_SSL_KEY_PATH",
                "util/KEY.pem",
            ),
            database_path=_read_path(
                "DESTINY_DATABASE_PATH",
                "data/destiny.sqlite3",
            ),
            bungie_client_id=os.getenv("BUNGIE_CLIENT_ID", "").strip(),
            bungie_client_secret=os.getenv("BUNGIE_CLIENT_SECRET", "").strip(),
            bungie_api_key=os.getenv("BUNGIE_API_KEY", "").strip(),
            bungie_redirect_uri=os.getenv(
                "BUNGIE_REDIRECT_URI",
                "https://localhost:42697/redirect",
            ).strip(),
            session_cookie_name=os.getenv(
                "DESTINY_SESSION_COOKIE_NAME",
                "destiny_session",
            ).strip(),
            inventory_stale_seconds=_read_positive_int(
                "DESTINY_INVENTORY_STALE_SECONDS",
                os.getenv("DESTINY_INVENTORY_STALE_SECONDS", "300"),
            ),
            manifest_path=_read_path(
                "DESTINY_MANIFEST_PATH",
                "data/manifest.sqlite3",
            ),
            manifest_language=os.getenv(
                "DESTINY_MANIFEST_LANGUAGE",
                "en",
            ).strip()
            or "en",
        )

    @property
    def bungie_is_configured(self) -> bool:
        return not self.bungie_configuration_errors

    def require_bungie(self) -> None:
        errors = self.bungie_configuration_errors
        if errors:
            raise ConfigurationError(
                "Bungie OAuth configuration is invalid: " + "; ".join(errors)
            )

    @property
    def bungie_configuration_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.bungie_client_id.isdigit():
            errors.append("BUNGIE_CLIENT_ID must be the numeric client ID")
        if _is_missing_or_placeholder(self.bungie_client_secret):
            errors.append("BUNGIE_CLIENT_SECRET must contain the client secret")
        if _is_missing_or_placeholder(self.bungie_api_key):
            errors.append("BUNGIE_API_KEY must contain the API key")
        if not self.bungie_redirect_uri.startswith("https://"):
            errors.append("BUNGIE_REDIRECT_URI must be an HTTPS URL")
        return tuple(errors)


def _read_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("DESTINY_PORT must be an integer") from error

    if not 1 <= port <= 65535:
        raise ValueError("DESTINY_PORT must be between 1 and 65535")
    return port


def _read_path(variable_name: str, default: str) -> Path:
    configured = Path(os.getenv(variable_name, default)).expanduser()
    if not configured.is_absolute():
        configured = PROJECT_ROOT / configured
    return configured.resolve()


def _read_positive_int(variable_name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{variable_name} must be an integer") from error
    if parsed <= 0:
        raise ValueError(f"{variable_name} must be greater than zero")
    return parsed


def _is_missing_or_placeholder(value: str) -> bool:
    return not value or (value.startswith("<") and value.endswith(">"))
