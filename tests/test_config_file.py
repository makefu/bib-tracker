"""Merged YAML config files and their precedence against env vars."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bib_tracker.config import SETTINGS_ENV_PREFIX, Settings, load_settings


@pytest.fixture(autouse=True)
def _clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No stray BIB_TRACKER_* or XDG config from the developer's shell may leak
    into a test."""
    for key in list(os.environ):
        if key.startswith(SETTINGS_ENV_PREFIX):
            monkeypatch.delenv(key)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-empty"))


def test_defaults_apply_without_any_file(tmp_path: Path) -> None:
    settings = load_settings()
    assert settings.port == 8099
    assert settings.log_level == "info"
    assert settings.accounts == []


def test_yaml_file_provides_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
db_path: /srv/bib.db
host: ::1
port: 9001
log_level: debug
poll_interval_minutes: 60
metadata_providers: [openlibrary, dnb]
default_prices:
  book: 20.0
""".strip(),
        encoding="utf-8",
    )
    settings = load_settings(config_files=[path])
    assert settings.db_path == Path("/srv/bib.db")
    assert settings.host == "::1"
    assert settings.port == 9001
    assert settings.log_level == "debug"
    assert settings.poll_interval_minutes == 60
    assert settings.metadata_providers == ["openlibrary", "dnb"]
    assert settings.default_prices["book"] == 20.0


def test_later_file_wins(tmp_path: Path) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("port: 9001\nlog_level: debug\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("port: 9002\n", encoding="utf-8")
    settings = load_settings(config_files=[first, second])
    assert settings.port == 9002
    assert settings.log_level == "debug"


def test_maps_merge_keywise(tmp_path: Path) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("default_prices:\n  book: 15.0\n  game: 35.0\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("default_prices:\n  book: 20.0\n", encoding="utf-8")
    prices = load_settings(config_files=[first, second]).default_prices
    assert prices["book"] == 20.0
    assert prices["game"] == 35.0


def test_accounts_merge_per_account_across_files(tmp_path: Path) -> None:
    """The secrets file completes the accounts the open file declared."""
    public = tmp_path / "config.yaml"
    public.write_text(
        """
accounts:
  stuttgart:
    library_type: stuttgart
    username: "5980610"
  remseck:
    library_type: remseck
    username: "103167"
""".strip(),
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text(
        """
accounts:
  stuttgart:
    password: "hunter2"
  remseck:
    password: "hunter3"
""".strip(),
        encoding="utf-8",
    )
    settings = load_settings(config_files=[public, secrets])
    by_name = {a.name: a for a in settings.accounts}
    assert sorted(by_name) == ["remseck", "stuttgart"]
    assert by_name["stuttgart"].username == "5980610"
    assert by_name["stuttgart"].resolve_password() == "hunter2"
    assert by_name["remseck"].resolve_password() == "hunter3"


def test_accounts_accept_a_list_and_a_mapping(tmp_path: Path) -> None:
    listed = tmp_path / "listed.yaml"
    listed.write_text(
        'accounts:\n  - name: remseck\n    library_type: remseck\n    username: "1"\n    password_file: pw.txt\n',
        encoding="utf-8",
    )
    (account,) = load_settings(config_files=[listed]).accounts
    assert account.password_file == Path("pw.txt")
    assert account.name == "remseck"


def test_a_non_mapping_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not hold a YAML mapping"):
        load_settings(config_files=[path])


def test_a_bare_password_is_required(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text('accounts:\n  broken:\n    library_type: remseck\n    username: "1"\n', encoding="utf-8")
    settings = load_settings(config_files=[path])
    with pytest.raises(RuntimeError, match="no password from any source"):
        settings.accounts[0].resolve_password()


def test_env_vars_beat_every_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("port: 9001\nmetadata_providers: [openlibrary, dnb]\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("port: 9002\n", encoding="utf-8")
    monkeypatch.setenv("BIB_TRACKER_PORT", "9999")
    monkeypatch.setenv("BIB_TRACKER_METADATA_PROVIDERS", "dnb,wikidata")
    settings = load_settings(config_files=[first, second])
    assert settings.port == 9999
    assert settings.metadata_providers == ["dnb", "wikidata"]


def test_explicit_files_win_over_xdg_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (xdg / "bib-tracker").mkdir(parents=True)
    (xdg / "bib-tracker" / "config.yaml").write_text("port: 9002\n", encoding="utf-8")
    explicit = tmp_path / "other.yaml"
    explicit.write_text("port: 9003\n", encoding="utf-8")
    assert load_settings().port == 9002
    assert load_settings(config_files=[explicit]).port == 9003


def test_config_files_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.yaml"
    first.write_text("port: 9004\nlog_level: debug\n", encoding="utf-8")
    second = tmp_path / "b.yaml"
    second.write_text("port: 9005\n", encoding="utf-8")
    monkeypatch.setenv("BIB_TRACKER_CONFIG_FILES", os.pathsep.join([str(first), str(second)]))
    settings = load_settings()
    assert settings.port == 9005
    assert settings.log_level == "debug"


def test_env_var_beats_files_and_files_beat_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    pointer = tmp_path / "pointer.yaml"
    pointer.write_text("port: 9005\nlog_level: debug\n", encoding="utf-8")
    monkeypatch.setenv("BIB_TRACKER_CONFIG_FILES", str(pointer))
    monkeypatch.setenv("BIB_TRACKER_PORT", "9006")
    settings = load_settings()
    assert settings.port == 9006
    assert settings.log_level == "debug"


def test_missing_explicit_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_settings(config_files=[tmp_path / "gone.yaml"])


def test_missing_pointer_file_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIB_TRACKER_CONFIG_FILES", str(tmp_path / "gone.yaml"))
    with pytest.raises(FileNotFoundError, match="does not exist"):
        load_settings()


def test_inline_api_keys_merge_like_other_maps(tmp_path: Path) -> None:
    public = tmp_path / "config.yaml"
    public.write_text('metadata_api_keys:\n  vlb: "vlb-token"\n', encoding="utf-8")
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text('metadata_api_keys:\n  bgg: "bgg-token"\n', encoding="utf-8")
    settings = load_settings(config_files=[public, secrets])
    assert settings.provider_api_key("bgg") == "bgg-token"
    assert settings.provider_api_key("vlb") == "vlb-token"
    assert settings.provider_api_key("openlibrary") is None


def test_cli_reports_missing_config_without_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import bib_tracker.cli as cli

    assert cli.main([str(tmp_path / "gone.yaml")]) == 2
    err = capsys.readouterr().err
    assert "does not exist" in err
    assert "Traceback" not in err


def test_direct_construction_ignores_default_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests build Settings(db_path=...); a config file picked up from the
    developer's machine would make those runs non-deterministic."""
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    (xdg / "bib-tracker").mkdir(parents=True)
    (xdg / "bib-tracker" / "config.yaml").write_text("port: 9010\n", encoding="utf-8")
    assert Settings(db_path=tmp_path / "x.db").port == 8099


def test_init_kwargs_beat_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Programmatic construction is the top source so the test fixtures in
    conftest stay deterministic no matter what the developer's shell exports."""
    monkeypatch.setenv("BIB_TRACKER_PORT", "9011")
    assert Settings(db_path=tmp_path / "x.db", port=8099).port == 8099


def test_cli_merges_positional_config_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import bib_tracker.cli as cli

    public = tmp_path / "config.yaml"
    public.write_text(
        'db_path: chosen.db\naccounts:\n  remseck:\n    library_type: remseck\n    username: "42"\n',
        encoding="utf-8",
    )
    secrets = tmp_path / "secrets.yaml"
    secrets.write_text('accounts:\n  remseck:\n    password: "hunter2"\n', encoding="utf-8")
    captured: list[Settings] = []
    monkeypatch.setattr(cli, "load_settings", lambda **kw: captured.append(load_settings(**kw)) or Settings())

    def fake_run(app: object, **kwargs: object) -> None:
        return None

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    argv = [str(public), str(secrets)]
    assert cli.main(argv) == 0
    (settings,) = captured
    assert settings.db_path == Path("chosen.db")
    (account,) = settings.accounts
    assert account.resolve_password() == "hunter2"
    assert account.username == "42"
