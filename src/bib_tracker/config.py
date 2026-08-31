"""Runtime configuration.

Everything non-secret arrives as BIB_TRACKER_* environment variables plus an
accounts JSON file written by the NixOS module. Passwords never appear in
either: an account names a systemd credential, which the unit exposes in
$CREDENTIALS_DIRECTORY as a file readable only by that unit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: What a borrowed item is worth when no list price could be found. Only ever
#: used to produce an explicitly-labelled estimate.
DEFAULT_PRICES_EUR: dict[str, float] = {
    "book": 15.0,
    "audiobook": 12.0,
    "music": 10.0,
    "movie": 10.0,
    "game": 35.0,
    "magazine": 5.0,
    "other": 10.0,
}

DEFAULT_LOAN_PERIOD_DAYS: dict[str, int] = {
    "book": 28,
    "audiobook": 28,
    "music": 28,
    "movie": 14,
    "game": 28,
    "magazine": 14,
    "other": 28,
}


class AccountConfig(BaseModel):
    """One library account, as declared in the NixOS module."""

    name: str
    library_type: str
    username: str
    base_url: str | None = None
    enabled: bool = True
    display_name: str | None = None
    colour: str | None = None
    #: Name of a systemd credential holding the password.
    password_credential: str | None = None
    #: Plain file fallback, for development and for the one-shot CLIs.
    password_file: Path | None = None
    loan_period_days: dict[str, int] = Field(default_factory=dict)

    def resolve_password(self, credentials_dir: Path | None = None) -> str:
        """Read the password from wherever systemd or the user put it."""
        if self.password_credential:
            base = credentials_dir or _credentials_directory()
            if base is None:
                raise RuntimeError(
                    f"Account {self.name!r} names credential {self.password_credential!r}, "
                    "but CREDENTIALS_DIRECTORY is not set"
                )
            return (base / self.password_credential).read_text(encoding="utf-8").strip()
        if self.password_file:
            return self.password_file.read_text(encoding="utf-8").strip()
        raise RuntimeError(f"Account {self.name!r} has neither password_credential nor password_file")

    def loan_period(self, media_class: str) -> int:
        return self.loan_period_days.get(media_class, DEFAULT_LOAN_PERIOD_DAYS.get(media_class, 28))


def _credentials_directory() -> Path | None:
    value = os.environ.get("CREDENTIALS_DIRECTORY")
    return Path(value) if value else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIB_TRACKER_", extra="ignore")

    db_path: Path = Path("bib-tracker.db")
    host: str = "127.0.0.1"
    port: int = 8099
    log_level: str = "info"

    accounts_file: Path | None = None

    poll_interval_minutes: int = 360
    poll_jitter_seconds: int = 300
    poll_max_concurrent: int = 2
    poll_on_startup: bool = True
    #: How many consecutive empty results are needed before believing that an
    #: account really was emptied, rather than that the parser broke.
    zero_result_confirmations: int = 2
    #: Fraction of open loans that vanishing at once makes a poll suspect.
    suspect_drop_ratio: float = 0.8
    #: A loan reappearing within this window is the same loan, not a new one.
    reopen_window_hours: int = 48

    renew_threshold_days: int = 3

    metadata_enabled: bool = True
    #: Open Library and the DNB need no credentials. Google Books works
    #: anonymously only until its per-address quota runs out, and BoardGameGeek
    #: refuses anonymous requests outright, so both are off unless configured.
    metadata_providers: list[str] = Field(default_factory=lambda: ["openlibrary", "dnb", "wikidata"])
    #: Which providers may supply a purchase price, in order of preference.
    #: The VLB is the reference database for the gebundener Ladenpreis but
    #: needs a contract; the DNB has the price as catalogued, free; Google
    #: Books is the fallback and rarely knows German titles.
    price_providers: list[str] = Field(default_factory=lambda: ["vlb", "dnb", "googlebooks"])
    metadata_base_urls: dict[str, str] = Field(default_factory=dict)
    #: Per-provider credential files, keyed by provider name.
    metadata_api_key_files: dict[str, Path] = Field(default_factory=dict)
    metadata_rate_limits: dict[str, int] = Field(default_factory=dict)
    user_agent_contact: str = "https://github.com/makefu/bib-tracker"
    #: Requests per minute for a provider that does not name its own limit.
    default_rate_limit_per_minute: int = 30

    default_prices: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_PRICES_EUR))
    media_class_map: dict[str, str] = Field(default_factory=dict)

    @field_validator(
        "metadata_providers",
        "price_providers",
        "metadata_base_urls",
        "metadata_api_key_files",
        "metadata_rate_limits",
        "default_prices",
        "media_class_map",
        mode="before",
    )
    @classmethod
    def _parse_json(cls, value: object) -> object:
        """Env vars carry these as JSON; a bare comma list is accepted too."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("[", "{")):
                return json.loads(text)
            return [part.strip() for part in text.split(",") if part.strip()]
        return value

    def load_accounts(self) -> list[AccountConfig]:
        if self.accounts_file is None:
            return []
        raw = json.loads(self.accounts_file.read_text(encoding="utf-8"))
        return [AccountConfig.model_validate(item) for item in raw]

    def provider_api_key(self, provider: str) -> str | None:
        """Read a provider credential from wherever systemd or the user put it.

        Missing is not an error: a provider that needs one reports itself as
        unavailable, which the interface can explain, rather than failing.
        """
        path = self.metadata_api_key_files.get(provider)
        if path is None:
            return None
        candidate = Path(path)
        if not candidate.is_absolute():
            base = _credentials_directory()
            if base is not None:
                candidate = base / candidate
        try:
            return candidate.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def provider_rate_limit(self, provider: str) -> int:
        return self.metadata_rate_limits.get(provider, self.default_rate_limit_per_minute)

    def default_price_cents(self, media_class: str) -> int:
        euros = self.default_prices.get(media_class, DEFAULT_PRICES_EUR.get(media_class, 10.0))
        return round(euros * 100)


def load_settings() -> Settings:
    return Settings()
