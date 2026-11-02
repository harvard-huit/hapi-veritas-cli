import os
import stat

import pytest

from veritas import pkce, stacks
from veritas.store import StoreError, TokenStore, default_backend


def test_convention_hosts():
    assert stacks.resolve("dev").auth_base == "https://go.dev.apis.huit.harvard.edu/oauth2/v1"
    assert stacks.resolve("prod").auth_base == "https://go.apis.huit.harvard.edu/oauth2/v1"
    assert stacks.resolve("prod").gateway_base == "https://prod.apis.huit.harvard.edu"
    assert stacks.resolve("stage").gateway_base == "https://stage.apis.huit.harvard.edu"


def test_unlisted_stack_follows_the_convention():
    future = stacks.resolve("qa2")
    assert future.auth_base == "https://go.qa2.apis.huit.harvard.edu/oauth2/v1"
    assert future.gateway_base == "https://qa2.apis.huit.harvard.edu"


def test_config_overrides_convention():
    stack_config = {"lab": {"auth_base": "https://auth.example.edu/oauth2/v1/",
                            "gateway_base": "https://api.example.edu"}}
    lab = stacks.resolve("lab", stack_config)
    assert lab.auth_base == "https://auth.example.edu/oauth2/v1"  # trailing slash trimmed
    assert lab.gateway_base == "https://api.example.edu"
    assert stacks.resolve("dev", stack_config).gateway_base == "https://dev.apis.huit.harvard.edu"


def test_env_override_is_scoped_to_its_stack(monkeypatch):
    monkeypatch.setenv("VERITAS_NEW_STACK_AUTH_BASE", "https://env.example.edu/oauth2/v1")
    assert stacks.resolve("new-stack").auth_base == "https://env.example.edu/oauth2/v1"
    assert stacks.resolve("dev").auth_base == "https://go.dev.apis.huit.harvard.edu/oauth2/v1"


@pytest.mark.parametrize("bad_name", ["", "Dev", "dev/../x", "a" * 40, "1dev"])
def test_bad_stack_names_refused(bad_name):
    with pytest.raises(stacks.StackError):
        stacks.resolve(bad_name)


def test_non_https_base_refused():
    with pytest.raises(stacks.StackError, match="https"):
        stacks.resolve("lab", {"lab": {"auth_base": "http://auth.example.edu"}})


def test_pkce_matches_rfc7636_appendix_b():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce.challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_length_is_legal():
    assert 43 <= len(pkce.make_verifier()) <= 128


def test_file_store_round_trip_and_permissions(isolated_home):
    store = TokenStore()
    record = {"client": "awa-cli", "access_token": "secret-ish"}
    store.put("dev", "awa-cli", record)
    assert store.get("dev", "awa-cli") == record
    assert store.get("stage", "awa-cli") is None

    tokens_file = isolated_home / "tokens.json"
    if os.name != "nt":   # Windows does not enforce POSIX modes; the profile directory is private
        assert stat.S_IMODE(tokens_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(isolated_home.stat().st_mode) == 0o700

    store.delete("dev", "awa-cli")
    assert store.get("dev", "awa-cli") is None
    store.delete("dev", "never-stored")  # deleting nothing is not an error


def test_active_client_is_per_stack():
    store = TokenStore()
    store.set_active_client("dev", "awa-cli")
    store.set_active_client("stage", "other")
    assert store.active_client("dev") == "awa-cli"
    assert store.active_client("stage") == "other"
    store.set_active_client("dev", None)
    assert store.active_client("dev") is None
    assert store.active_client("stage") == "other"


def test_unknown_store_backend_is_refused(monkeypatch):
    monkeypatch.setenv("VERITAS_TOKEN_STORE", "s3")
    with pytest.raises(StoreError):
        default_backend()
