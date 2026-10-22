import time

import httpx
import pytest
from click.testing import CliRunner

from veritas import cli
from veritas.oauth import AuthServer
from veritas.stacks import resolve
from veritas.store import TokenStore

ISSUER = "https://go.dev.apis.huit.harvard.edu/oauth2/v1"


@pytest.fixture
def token_endpoint(monkeypatch):
    """Route every AuthServer the CLI builds through a mock transport; record the requests."""
    calls = []
    responses = {"/oauth2/v1/.well-known/oauth-authorization-server": httpx.Response(200, json={
        "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token", "userinfo_endpoint": f"{ISSUER}/userinfo"})}

    def handler(request):
        if not request.url.path.endswith("/.well-known/oauth-authorization-server"):
            calls.append(request)
        return responses.get(request.url.path, httpx.Response(404))

    monkeypatch.setattr(cli, "auth_server", lambda stack: AuthServer(
        stack, http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    return calls, responses


def stored(grant="authorization_code", expires_in=3600, refresh_in=86400):
    now = int(time.time())
    return {"client": "awa-cli", "grant": grant, "access_token": "old-access",
            "refresh_token": "old-refresh" if grant == "authorization_code" else None,
            "obtained_at": now, "expires_at": now + expires_in,
            "refresh_expires_at": now + refresh_in if refresh_in else None,
            "application_name": None}


def test_token_prints_without_refresh_when_valid(token_endpoint):
    calls, _ = token_endpoint
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    store.set_active_client("dev", "awa-cli")
    result = CliRunner().invoke(cli.main, ["token"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "old-access"
    assert calls == []


def test_token_refreshes_when_expired(token_endpoint):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": "3599",
        "refresh_token_expires_in": "2591999"})
    store = TokenStore()
    store.put("dev", "awa-cli", stored(expires_in=30))  # inside the 60s skew
    store.set_active_client("dev", "awa-cli")

    result = CliRunner().invoke(cli.main, ["token", "-e", "dev"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "new-access"
    assert b"grant_type=refresh_token" in calls[0].content
    assert store.get("dev", "awa-cli")["refresh_token"] == "new-refresh"


def test_expired_m2m_token_asks_for_m2m_not_login(token_endpoint):
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", expires_in=-10,
                                            refresh_in=None))
    result = CliRunner().invoke(cli.main, ["token", "--app", "consumer-key"])
    assert result.exit_code == 1
    assert "veritas m2m" in result.output


def test_no_client_explains_how_to_log_in():
    result = CliRunner().invoke(cli.main, ["token", "-e", "stage"])
    assert result.exit_code == 1
    assert "veritas login -e stage --app" in result.output


def test_m2m_reads_secret_from_env_and_stores(token_endpoint, monkeypatch):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "m2m-access", "expires_in": "3599", "application_name": "svc-app"})
    monkeypatch.setenv("SVC_SECRET", "s3cret")
    result = CliRunner().invoke(cli.main, ["m2m", "--client-id", "consumer-key",
                                           "--secret-env", "SVC_SECRET"])
    assert result.exit_code == 0, result.output
    assert "s3cret" not in result.output and "m2m-access" not in result.output
    store = TokenStore()
    assert store.active_client("dev") == "consumer-key"
    assert store.get("dev", "consumer-key")["access_token"] == "m2m-access"


def test_m2m_without_secret_fails(monkeypatch):
    monkeypatch.delenv("APIGEE_CLIENT_SECRET", raising=False)
    result = CliRunner().invoke(cli.main, ["m2m", "--client-id", "consumer-key"])
    assert result.exit_code == 1
    assert "APIGEE_CLIENT_SECRET" in result.output


def test_whoami_for_m2m_reports_no_user(token_endpoint):
    calls, _ = token_endpoint
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", refresh_in=None))
    result = CliRunner().invoke(cli.main, ["whoami", "--app", "consumer-key"])
    assert result.exit_code == 0, result.output
    assert "carries no user" in result.output
    assert calls == []


def test_logout_clears_token_and_active(token_endpoint):
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    store.set_active_client("dev", "awa-cli")
    result = CliRunner().invoke(cli.main, ["logout"])
    assert result.exit_code == 0, result.output
    assert store.get("dev", "awa-cli") is None
    assert store.active_client("dev") is None


def test_call_target_resolution():
    dev = resolve("dev")
    assert cli._target_url(dev, "/ais-bedrock-llm/apigee/quota") == \
        "https://dev.apis.huit.harvard.edu/ais-bedrock-llm/apigee/quota"
    assert cli._target_url(dev, "https://dev.apis.huit.harvard.edu/x") == \
        "https://dev.apis.huit.harvard.edu/x"
    assert cli._target_url(dev, "https://example.org/x", any_host=True) == "https://example.org/x"


@pytest.mark.parametrize("target", ["https://example.org/x",
                                    "http://dev.apis.huit.harvard.edu/x",
                                    "https://stage.apis.huit.harvard.edu/x"])
def test_call_refuses_to_send_the_token_off_gateway(target):
    with pytest.raises(SystemExit):
        cli._target_url(resolve("dev"), target)


def test_unlisted_stack_from_config_is_accepted(isolated_home):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "config.json").write_text(
        '{"default_stack": "lab", "stacks": {"lab": {"auth_base": "https://auth.example.edu/v1"}}}')
    store = TokenStore()
    store.put("lab", "awa-cli", stored())
    store.set_active_client("lab", "awa-cli")
    result = CliRunner().invoke(cli.main, ["token"])  # default_stack from config
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "old-access"


def test_invalid_stack_name_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["token", "-e", "Not A Stack"])
    assert result.exit_code == 2
    assert "not a valid name" in result.output
