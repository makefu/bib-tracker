"""Credentials for the live tests.

Read from .secrets.yml, which is git-ignored. A test that needs a credential
it cannot find skips rather than fails: not everyone running the suite has a
VLB contract or a BoardGameGeek token.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SECRETS = [
    Path(__file__).resolve().parents[2] / ".secrets.yml",
    Path.home() / "r/ha_stadtbibliothek/.secrets.yml",
]


def _load() -> dict[str, str]:
    for path in SECRETS:
        if not path.exists():
            continue
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if ":" not in line or line.strip().startswith("#"):
                continue
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values
    return {}


@pytest.fixture(scope="session")
def secrets() -> dict[str, str]:
    return _load()


@pytest.fixture
def bgg_token(secrets: dict[str, str]) -> str:
    token = secrets.get("bgg_api_key")
    if not token:
        pytest.skip("no bgg_api_key in .secrets.yml")
    return token


@pytest.fixture
def vlb_token(secrets: dict[str, str]) -> str:
    token = secrets.get("vlb_api_key")
    if not token:
        pytest.skip("no vlb_api_key in .secrets.yml (a VLB contract is needed)")
    return token
