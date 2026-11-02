"""The Python API: headers and auth for whoever ran `veritas login`, from the CLI's own store."""
import time

import httpx
import pytest

import veritas
from veritas.store import TokenStore
from test_cli import stored, token_endpoint  # noqa: F401  (fixture)


def signed_in(stack="dev", client="awa-cli", **record):
    store = TokenStore()
    store.put(stack, client, dict(stored(), **record))
    store.set_active_client(stack, client)


def test_headers_for_the_signed_in_user(token_endpoint):
    signed_in()
    assert veritas.headers(stack="dev") == {"Authorization": "Bearer old-access"}
    assert veritas.authorization(stack="dev") == "Bearer old-access"
    assert veritas.token(stack="dev") == "old-access"


def test_default_stack_is_prod_and_veritas_env_picks_another(token_endpoint, monkeypatch):
    signed_in("prod", access_token="prod-access")
    signed_in("dev", access_token="dev-access")
    assert veritas.token() == "prod-access"
    monkeypatch.setenv("VERITAS_ENV", "dev")
    assert veritas.token() == "dev-access"


def test_app_picks_a_client_other_than_the_active_one(token_endpoint):
    signed_in(client="awa-workshop_x", access_token="workshop-access")
    signed_in(client="awa-cli")
    assert veritas.token(stack="dev", app="awa-workshop_x") == "workshop-access"


def test_expired_token_is_refreshed_and_stored(token_endpoint):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": "3599"})
    signed_in(expires_at=int(time.time()) + 10)
    assert veritas.token(stack="dev") == "new-access"
    assert TokenStore().get("dev", "awa-cli")["access_token"] == "new-access"


def test_not_signed_in_raises_with_the_fix_and_never_opens_a_browser(token_endpoint, monkeypatch):
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("must not open a browser"))
    with pytest.raises(veritas.VeritasError, match="veritas login -e stage"):
        veritas.headers(stack="stage")


def test_gateway_is_the_stack_base_url():
    assert veritas.gateway("dev") == "https://dev.apis.huit.harvard.edu"
    assert veritas.gateway("prod") == "https://prod.apis.huit.harvard.edu"


def test_auth_sends_bearer_with_httpx_and_refreshes_once_on_401(token_endpoint):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": "3599"})
    signed_in()
    seen = []

    def gateway_handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200)

    with httpx.Client(transport=httpx.MockTransport(gateway_handler)) as http:
        response = http.get("https://dev.apis.huit.harvard.edu/x", auth=veritas.Auth(stack="dev"))
    assert response.status_code == 200
    assert seen == ["Bearer old-access", "Bearer new-access"]


def test_auth_works_as_a_requests_auth(token_endpoint):
    signed_in()

    class PreparedRequest:   # the part of requests.PreparedRequest an auth touches
        headers = {}

    request = veritas.Auth(stack="dev")(PreparedRequest())
    assert request.headers["Authorization"] == "Bearer old-access"


def test_importing_veritas_does_not_load_the_cli():
    # The CLI is the layer that prints and exits; a library import must not pull it in. (click itself
    # may load anyway: httpx imports it when it is installed.)
    import subprocess
    import sys
    probe = "import sys, veritas; print('veritas.cli' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.stdout.strip() == "False", result.stderr
