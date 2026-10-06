"""Runtime configuration.

Settings arrive as YAML config files (repeatable, later files override
earlier ones) plus BIB_TRACKER_* environment variables, which always win.
Accounts are declared inline under the ``accounts`` key. Passwords never
appear inline under systemd: an account names a credential, which the unit
exposes in $CREDENTIALS_DIRECTORY as a file readable only by that unit.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

#: Prefix for every environment variable that overrides the config files.
SETTINGS_ENV_PREFIX = "BIB_TRACKER_"
#: Environment variable naming the config files, os.pathsep-separated, for
#: setups that cannot pass command-line flags (systemd units, containers).
CONFIG_FILES_ENV_VAR = SETTINGS_ENV_PREFIX + "CONFIG_FILES"

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


#: Set by load_settings() around the Settings() call so settings_customise_sources
#: — a classmethod with no access to the init kwargs — knows which files to read.
_active_config_files: ContextVar[list[Path] | None] = ContextVar("bib_tracker_config_files", default=None)


class _LenientEnvSettingsSource(EnvSettingsSource):
    """Env source that hands a non-JSON string on to the field validators.

    The base source aborts the whole load when a complex field holds a value
    that is not JSON, but a bare comma list is a documented shorthand for
    list settings; the validators accept it.
    """

    def decode_complex_value(self, field_name: str, field: FieldInfo, value: Any) -> Any:
        try:
            return super().decode_complex_value(field_name, field, value)
        except ValueError:
            return value


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
    #: Inline secret, for a YAML file that is itself the secrets file.
    password: str | None = None
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
        if self.password is not None:
            return self.password
        if self.password_file:
            return self.password_file.read_text(encoding="utf-8").strip()
        raise RuntimeError(f"Account {self.name!r} has no password from any source")

    def loan_period(self, media_class: str) -> int:
        return self.loan_period_days.get(media_class, DEFAULT_LOAN_PERIOD_DAYS.get(media_class, 28))


def _credentials_directory() -> Path | None:
    value = os.environ.get("CREDENTIALS_DIRECTORY")
    return Path(value) if value else None


def default_config_file() -> Path:
    """$XDG_CONFIG_HOME/bib-tracker/config.yaml, the XDG location."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "bib-tracker" / "config.yaml"


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"config file {path} does not hold a YAML mapping")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _accounts_by_name(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalise the accounts key to a mapping keyed by account name.

    Both shapes are accepted: a list of objects each carrying a name, or a
    mapping already keyed by name, which is how the NixOS module declares
    them. Accounts from separate files merge by name so that one file can
    carry the open configuration and another the credentials for the very
    same accounts.
    """
    raw = data.get("accounts")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        entries: list[Any] = [{**entry, "name": entry.get("name", name)} for name, entry in raw.items()]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise ValueError("the accounts key must be a list or a mapping")
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValueError("every account needs a name")
        name = str(entry["name"])
        by_name[name] = _deep_merge(by_name.get(name, {}), entry)
    return by_name


def _merged_config_data(paths: Sequence[Path]) -> dict[str, Any]:
    """Read every file and overlay them, later files winning.

    Scalars are replaced, mappings merged key-wise, and accounts merged per
    account name — a file that only carries passwords completes the accounts
    another file declared instead of dropping them.
    """
    data: dict[str, Any] = {}
    accounts: dict[str, dict[str, Any]] = {}
    for path in paths:
        current = _read_yaml_mapping(path)
        current_accounts = _accounts_by_name(current)
        current = {key: value for key, value in current.items() if key != "accounts"}
        data = _deep_merge(data, current)
        accounts = {
            name: _deep_merge(accounts.get(name, {}), entry) for name, entry in {**accounts, **current_accounts}.items()
        }
    if accounts:
        data["accounts"] = list(accounts.values())
    return data


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix=SETTINGS_ENV_PREFIX, extra="ignore")

    #: The YAML files this configuration was loaded from, if any. Set by
    #: load_settings(); a bare Settings(...) never reads any, so tests and
    #: embedding code stay deterministic.
    config_files: list[Path] = Field(default_factory=list)

    db_path: Path = Path("bib-tracker.db")
    host: str = "127.0.0.1"
    port: int = 8099
    log_level: str = "info"
    #: Library accounts, declared inline. Files merge their account lists per
    #: account name, so an open config and a secrets file can each carry part
    #: of the same account.
    accounts: list[AccountConfig] = Field(default_factory=list)

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
    #: The catalogue sources first: the VLB is the reference database for the
    #: gebundener Ladenpreis but needs a contract, the DNB has the price as
    #: catalogued, free, and Google Books rarely knows German titles. The
    #: shops then answer for everything they stock; they are polite by
    #: default, riding metadata_rate_limits / default_rate_limit_per_minute.
    price_providers: list[str] = Field(
        default_factory=lambda: [
            "vlb",
            "dnb",
            "googlebooks",
            "buchkatalog",
            "thalia",
            "amazon",
            "buch7",
            "lehmanns",
            "ebookde",
        ]
    )
    #: Prioritised cover-image sources, walked only when the OPAC and the
    #: enrichment merge gave none. `library` means the OPAC's own cover URLs.
    image_providers: list[str] = Field(
        default_factory=lambda: [
            "library",
            "openlibrary",
            "thalia",
            "googlebooks",
            "buchkatalog",
            "buch7",
            "ebookde",
            "lehmanns",
            "amazon",
        ]
    )
    #: Re-probe a provider whose last answer was 'blocked' or 'error' after
    #: this many days; 'found' and 'not_found' are never retried on their own.
    price_retry_days: int = 14
    metadata_base_urls: dict[str, str] = Field(default_factory=dict)
    #: Per-provider credential files, keyed by provider name.
    metadata_api_key_files: dict[str, Path] = Field(default_factory=dict)
    #: Inline tokens, for a YAML file that is itself the secrets file.
    metadata_api_keys: dict[str, str] = Field(default_factory=dict)
    #: Per-provider `Cookie:` header values (e.g. a Thalia session that gets
    #: past Cloudflare), inline or via files, same secrets handling as keys.
    metadata_cookies: dict[str, str] = Field(default_factory=dict)
    metadata_cookie_files: dict[str, Path] = Field(default_factory=dict)
    metadata_rate_limits: dict[str, int] = Field(default_factory=dict)
    user_agent_contact: str = "https://github.com/makefu/bib-tracker"
    #: Requests per minute for a provider that does not name its own limit.
    default_rate_limit_per_minute: int = 30

    default_prices: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_PRICES_EUR))
    media_class_map: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Earlier source wins: explicit init, then env vars, then the merged
        # YAML files.
        files = _active_config_files.get()
        env = _LenientEnvSettingsSource(settings_cls)
        if not files:
            return (init_settings, env)
        merged = _merged_config_data(files)
        return (init_settings, env, InitSettingsSource(settings_cls, merged))

    @field_validator(
        "metadata_providers",
        "price_providers",
        "image_providers",
        "metadata_base_urls",
        "metadata_api_key_files",
        "metadata_api_keys",
        "metadata_cookies",
        "metadata_cookie_files",
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

    def _read_secret(self, name: str, files: dict[str, Path], inline: dict[str, str]) -> str | None:
        """Read one secret from wherever systemd or the user put it.

        Missing is not an error: a provider that needs one reports itself as
        unavailable, which the interface can explain, rather than failing.
        """
        path = files.get(name)
        if path is not None:
            candidate = Path(path)
            if not candidate.is_absolute():
                base = _credentials_directory()
                if base is not None:
                    candidate = base / candidate
            try:
                secret = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                secret = ""
            if secret:
                return secret
        return inline.get(name) or None

    def provider_api_key(self, provider: str) -> str | None:
        return self._read_secret(provider, self.metadata_api_key_files, self.metadata_api_keys)

    def provider_cookie(self, provider: str) -> str | None:
        return self._read_secret(provider, self.metadata_cookie_files, self.metadata_cookies)

    def provider_rate_limit(self, provider: str) -> int:
        return self.metadata_rate_limits.get(provider, self.default_rate_limit_per_minute)

    def default_price_cents(self, media_class: str) -> int:
        euros = self.default_prices.get(media_class, DEFAULT_PRICES_EUR.get(media_class, 10.0))
        return round(euros * 100)


def load_settings(config_files: Sequence[Path] | None = None) -> Settings:
    """Build settings from YAML files, overridden by BIB_TRACKER_* env vars.

    Several files are merged, later ones winning key by key, which lets the
    open configuration and the account credentials live apart. The files are
    the given ones, else $BIB_TRACKER_CONFIG_FILES (os.pathsep-separated),
    else the XDG default; a named file must exist, the default need not.
    """
    if config_files is not None:
        paths = [Path(p).expanduser() for p in config_files]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"config file {path} does not exist")
    else:
        pointer = os.environ.get(CONFIG_FILES_ENV_VAR)
        if pointer:
            paths = [Path(part).expanduser() for part in pointer.split(os.pathsep) if part]
            for path in paths:
                if not path.is_file():
                    raise FileNotFoundError(f"config file {path} does not exist")
        else:
            default = default_config_file()
            paths = [default] if default.is_file() else []
    token = _active_config_files.set(paths)
    try:
        return Settings(config_files=paths)
    finally:
        _active_config_files.reset(token)
