import base64
import socket
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from veritas.oauth import (AuthServer, LoopbackError, OAuthError, parse_redirect_uri,
                           receive_callback, token_record)
from veritas.stacks import resolve

ISSUER = "https://go.dev.apis.huit.harvard.edu/oauth2/v1"
METADATA_PATH = "/oauth2/v1/.well-known/oauth-authorization-server"
# Deliberately NOT the conventional paths, to prove the endpoints come from discovery.
METADATA = {"issuer": ISSUER,
            "authorization_endpoint": "https://login.example.edu/start",
            "token_endpoint": "https://tokens.example.edu/exchange",
            "userinfo_endpoint": "https://tokens.example.edu/me"}
TOKEN_URL = METADATA["token_endpoint"]


def mock_server(handler, metadata=METADATA):
    """AuthServer for dev whose transport serves ``metadata`` at discovery, ``handler`` elsewhere.
    ``seen`` records only the non-discovery requests."""
    seen = []

    def transport_handler(request):
        if request.url.path == METADATA_PATH:
            return httpx.Response(200, json=metadata)
        seen.append(request)
        return handler(request)

    return AuthServer(resolve("dev"), http_client=httpx.Client(transport=httpx.MockTransport(
        transport_handler))), seen


def form_of(request):
    return {name: values[0] for name, values in parse_qs(request.content.decode()).items()}


def test_native_code_exchange_sends_app_name_and_no_secret():
    server, seen = mock_server(lambda request: httpx.Response(200, json={
        "access_token": "access", "refresh_token": "refresh", "expires_in": "3599",
        "refresh_token_expires_in": "2591999"}))
    record = server.exchange_code("awa-veritas_cli", "the-code", "http://localhost:8080/callback",
                                  "the-verifier")
    request = seen[0]
    assert str(request.url) == TOKEN_URL
    assert "authorization" not in request.headers
    assert form_of(request) == {
        "grant_type": "authorization_code", "client_id": "awa-veritas_cli", "code": "the-code",
        "redirect_uri": "http://localhost:8080/callback", "code_verifier": "the-verifier"}
    assert record["refresh_token"] == "refresh"
    assert record["expires_at"] - record["obtained_at"] == 3599


def test_client_credentials_uses_basic_auth():
    server, seen = mock_server(lambda request: httpx.Response(200, json={
        "access_token": "access", "expires_in": 3599, "application_name": "some-app"}))
    record = server.client_credentials("consumer-key", "consumer-secret")
    expected = base64.b64encode(b"consumer-key:consumer-secret").decode()
    assert seen[0].headers["authorization"] == f"Basic {expected}"
    assert form_of(seen[0]) == {"grant_type": "client_credentials"}
    assert record["grant"] == "client_credentials"
    assert record["refresh_token"] is None


def test_error_body_is_kept_not_reduced_to_nulls():
    server, _ = mock_server(lambda request: httpx.Response(400, json={
        "error": "invalid_grant", "error_description": "bad code"}))
    with pytest.raises(OAuthError) as raised:
        server.exchange_code("app", "code", "http://localhost:8080/callback", "verifier")
    assert raised.value.status == 400
    assert raised.value.body["error"] == "invalid_grant"


def test_200_without_a_token_is_an_error():
    server, _ = mock_server(lambda request: httpx.Response(200, json={"fault": "odd"}))
    with pytest.raises(OAuthError):
        server.refresh("app", "refresh")


def test_metadata_fetched_once_per_server():
    discovery_hits = []

    def transport_handler(request):
        if request.url.path == METADATA_PATH:
            discovery_hits.append(request)
            return httpx.Response(200, json=METADATA)
        return httpx.Response(200, json={"access_token": "access", "expires_in": 3599})

    server = AuthServer(resolve("dev"), http_client=httpx.Client(
        transport=httpx.MockTransport(transport_handler)))
    server.client_credentials("key", "secret")
    server.client_credentials("key", "secret")
    assert len(discovery_hits) == 1


def test_issuer_mismatch_refused_before_any_token_request():
    server, seen = mock_server(lambda request: httpx.Response(200, json={"access_token": "x"}),
                               metadata={**METADATA, "issuer": "https://evil.example.com"})
    with pytest.raises(OAuthError, match="issuer"):
        server.client_credentials("key", "secret")
    assert seen == []


def test_token_record_treats_zero_lifetime_as_unknown():
    record = token_record("app", "authorization_code",
                          {"access_token": "access", "expires_in": "3599",
                           "refresh_token_expires_in": "0"}, now=1000)
    assert record["expires_at"] == 4599
    assert record["refresh_expires_at"] is None


def test_authorize_url_carries_pkce_and_state():
    server, _ = mock_server(lambda request: httpx.Response(404))
    url = server.authorize_url("my app", "http://localhost:8080/callback", "challenge", "state-1")
    parts = urlsplit(url)
    query = {name: values[0] for name, values in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == METADATA["authorization_endpoint"]
    assert query["client_id"] == "my app"
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == "state-1"


# --- loopback ---------------------------------------------------------------------------------
def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_receiver(redirect_uri, state, requests_to_send):
    outcome = {}

    def send_requests():
        for path in requests_to_send:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).read()
            except Exception:
                pass

    port = parse_redirect_uri(redirect_uri)[0]

    def receive():
        outcome["result"] = receive_callback(redirect_uri, state, timeout=10,
                                             on_ready=lambda: threading.Thread(
                                                 target=send_requests).start())

    receiver = threading.Thread(target=receive)
    receiver.start()
    receiver.join(15)
    return outcome["result"]


def test_loopback_returns_code_and_ignores_other_paths():
    port = free_port()
    result = run_receiver(f"http://localhost:{port}/callback", "good-state",
                          ["/favicon.ico", "/callback?code=abc123&state=good-state"])
    assert result.code == "abc123"
    assert result.error is None


def test_loopback_rejects_wrong_state():
    port = free_port()
    result = run_receiver(f"http://localhost:{port}/callback", "good-state",
                          ["/callback?code=abc123&state=forged"])
    assert result.code is None
    assert result.error == "state_mismatch"


def test_loopback_reports_access_denied():
    port = free_port()
    result = run_receiver(f"http://localhost:{port}/callback", "good-state",
                          ["/callback?error=access_denied&error_description=no+group"
                           "&state=good-state"])
    assert result.error == "access_denied"
    assert result.error_description == "no group"


def test_port_in_use_fails_instead_of_moving():
    with socket.socket() as occupier:
        occupier.bind(("127.0.0.1", 0))
        occupier.listen()
        port = occupier.getsockname()[1]
        with pytest.raises(LoopbackError, match="cannot listen"):
            receive_callback(f"http://localhost:{port}/callback", "state", timeout=1)


@pytest.mark.parametrize("redirect_uri", [
    "https://localhost:8080/callback", "http://example.com:8080/callback",
    "http://localhost/callback"])
def test_non_loopback_redirect_refused(redirect_uri):
    with pytest.raises(LoopbackError):
        parse_redirect_uri(redirect_uri)


def test_timeout_without_callback():
    started = time.monotonic()
    with pytest.raises(LoopbackError, match="no callback"):
        receive_callback(f"http://localhost:{free_port()}/callback", "state", timeout=1)
    assert time.monotonic() - started < 5
