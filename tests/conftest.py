import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own VERITAS_HOME and the file store: never the real keychain."""
    monkeypatch.setenv("VERITAS_HOME", str(tmp_path / "veritas-home"))
    monkeypatch.setenv("VERITAS_TOKEN_STORE", "file")
    monkeypatch.delenv("VERITAS_APP", raising=False)
    monkeypatch.delenv("VERITAS_ENV", raising=False)
    return tmp_path / "veritas-home"
