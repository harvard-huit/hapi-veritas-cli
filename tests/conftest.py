import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own VERITAS_HOME, CODEX_HOME and the file store: never the real keychain
    or the real ~/.codex/config.toml."""
    monkeypatch.setenv("VERITAS_HOME", str(tmp_path / "veritas-home"))
    monkeypatch.setenv("VERITAS_TOKEN_STORE", "file")
    # Never the developer's real Codex config: --install-config writes $CODEX_HOME/config.toml.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.delenv("VERITAS_APP", raising=False)
    monkeypatch.delenv("VERITAS_ENV", raising=False)
    return tmp_path / "veritas-home"
