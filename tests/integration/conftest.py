"""Credentials for the live tests.

Read from .secrets.yml, which is git-ignored. A test that needs a credential
it cannot find skips rather than fails: not everyone running the suite has a
VLB contract or a BoardGameGeek token.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

SECRETS = [
    Path(__file__).resolve().parents[2] / ".secrets.yml",
    Path.home() / "r/ha_stadtbibliothek/.secrets.yml",
]


def _load() -> dict[str, object]:
    for path in SECRETS:
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    return {}


def _api_key(secrets: dict[str, object], provider: str) -> str | None:
    """A token given inline or as the path of a file holding it."""
    flat = secrets.get(f"{provider}_api_key")
    if isinstance(flat, str) and flat:
        return flat
    inline = secrets.get("metadata_api_keys")
    if isinstance(inline, dict):
        token = inline.get(provider)
        if isinstance(token, str) and token:
            return token
    files = secrets.get("metadata_api_key_files")
    if isinstance(files, dict):
        path = files.get(provider)
        if isinstance(path, str) and path:
            candidate = Path(__file__).resolve().parents[2] / path
            try:
                return candidate.read_text(encoding="utf-8").strip() or None
            except OSError:
                return None
    return None


@pytest.fixture(scope="session")
def secrets() -> dict[str, object]:
    return _load()


@pytest.fixture
def bgg_token(secrets: dict[str, object]) -> str:
    token = _api_key(secrets, "bgg")
    if not token:
        pytest.skip("no bgg_api_key (inline or under metadata_api_key_files) in .secrets.yml")
    return token


@pytest.fixture
def vlb_token(secrets: dict[str, object]) -> str:
    token = _api_key(secrets, "vlb")
    if not token:
        pytest.skip("no vlb_api_key in .secrets.yml (a VLB contract is needed)")
    return token
