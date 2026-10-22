"""Talking to adex-oauth2-as.

Client types, as the AS treats them:
  native  — ``client_id`` is the Apigee APP NAME, no secret. The AS resolves the name to the
            consumerKey and injects the secret itself. Sending a name together with a secret fails.
  m2m     — ``client_id`` is the consumerKey, the secret goes in HTTP Basic (client_credentials).

Grants: authorization_code (PKCE S256 mandatory for native) -> 1h access + 30d refresh;
refresh_token -> new pair (the old refresh token keeps working); client_credentials -> 1h access,
never a refresh token.

Endpoint URLs come from the issuer's RFC 8414 metadata, fetched once per process and only when a
network call is actually needed, so a moved endpoint needs no CLI release. The metadata's issuer
must equal the configured auth base: a mismatch means the config points somewhere unexpected, and
tokens should not be sent there.
"""

import http.server
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")


class OAuthError(Exception):
    """The AS (or a protected endpoint) said no. ``body`` is what it actually sent."""

    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body


def _parse_body(response):
    try:
        return response.json()
    except ValueError:
        return response.text


def _seconds(value):
    # Apigee returns expires_in as a string ("3599"); treat missing/zero as "unknown".
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def token_record(client, grant, payload, now=None):
    """Turn a /token response into what we store. Only the fields we use are kept."""
    now = int(now if now is not None else time.time())
    access_lifetime = _seconds(payload.get("expires_in"))
    refresh_lifetime = _seconds(payload.get("refresh_token_expires_in"))
    return {
        "client": client,
        "grant": grant,
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "obtained_at": now,
        "expires_at": now + access_lifetime if access_lifetime else None,
        "refresh_expires_at": now + refresh_lifetime if refresh_lifetime else None,
        "application_name": payload.get("application_name"),
    }


class AuthServer:
    def __init__(self, stack, http_client=None, timeout=30.0):
        """``stack`` is a stacks.Stack; its auth_base is the issuer."""
        self.stack = stack
        self.issuer = stack.auth_base
        self.http = http_client or httpx.Client(timeout=timeout)
        self._metadata = None

    # --- discovery ----------------------------------------------------------------------------
    def metadata(self):
        if self._metadata is None:
            url = f"{self.issuer}/.well-known/oauth-authorization-server"
            response = self.http.get(url)
            body = _parse_body(response)
            if response.status_code != 200 or not isinstance(body, dict):
                raise OAuthError(f"could not read the AS metadata at {url}", response.status_code,
                                 body)
            if body.get("issuer", "").rstrip("/") != self.issuer:
                raise OAuthError(f"the AS at {self.issuer} reports issuer "
                                 f"{body.get('issuer')!r}; refusing to use it (check the stack's "
                                 f"auth_base)", response.status_code, None)
            self._metadata = body
        return self._metadata

    def endpoint(self, name):
        url = self.metadata().get(name)
        if not url:
            raise OAuthError(f"the AS metadata for {self.issuer} has no {name}")
        return url

    # --- authorization code -------------------------------------------------------------------
    def authorize_url(self, client_id, redirect_uri, code_challenge, state, scope=None,
                      resource=None):
        query = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if scope:
            query["scope"] = scope
        if resource:
            query["resource"] = resource
        return f"{self.endpoint('authorization_endpoint')}?{urlencode(query)}"

    def exchange_code(self, client_id, code, redirect_uri, code_verifier):
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        return token_record(client_id, "authorization_code", self._token_request(form))

    def refresh(self, client_id, refresh_token):
        form = {"grant_type": "refresh_token", "client_id": client_id,
                "refresh_token": refresh_token}
        return token_record(client_id, "authorization_code", self._token_request(form))

    # --- machine to machine -------------------------------------------------------------------
    def client_credentials(self, client_id, client_secret, scope=None, resource=None):
        form = {"grant_type": "client_credentials"}
        if scope:
            form["scope"] = scope
        if resource:
            form["resource"] = resource
        payload = self._token_request(form, basic_auth=(client_id, client_secret))
        return token_record(client_id, "client_credentials", payload)

    # --- userinfo -----------------------------------------------------------------------------
    def userinfo(self, access_token):
        response = self.http.get(self.endpoint("userinfo_endpoint"),
                                 headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code != 200:
            raise OAuthError("userinfo refused the token", response.status_code,
                             _parse_body(response))
        return response.json()

    def _token_request(self, form, basic_auth=None):
        response = self.http.post(self.endpoint("token_endpoint"), data=form,
                                  auth=basic_auth)
        body = _parse_body(response)
        # Check for the token rather than only the status: picking access_token out of an error
        # body would report a null token instead of the {"error": ...} the AS actually sent.
        if response.status_code != 200 or not isinstance(body, dict) or "access_token" not in body:
            raise OAuthError(f"token request ({form['grant_type']}) failed",
                             response.status_code, body)
        return body


# --- loopback redirect receiver ---------------------------------------------------------------
class LoopbackError(Exception):
    pass


@dataclass
class CallbackResult:
    code: str = None
    error: str = None
    error_description: str = None


_DONE_PAGE = (b"<!doctype html><meta charset=utf-8><title>veritas</title>"
              b"<body style='font-family:sans-serif;padding:2em'>"
              b"<h2>%s</h2><p>You can close this tab and return to the terminal.</p>")


def parse_redirect_uri(redirect_uri):
    parts = urlsplit(redirect_uri)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS or not parts.port:
        raise LoopbackError(f"redirect URI {redirect_uri!r} must be http://localhost:PORT/... or "
                            "http://127.0.0.1:PORT/... (a loopback listener on this machine)")
    return parts.port, parts.path or "/"


def receive_callback(redirect_uri, expected_state, timeout=300, on_ready=None):
    """Listen on the loopback redirect, return the first callback that hits the right path.

    Binds 127.0.0.1 only, so nothing off this machine can reach it. Requests to other paths (the
    browser's /favicon.ico) get a 404 and the wait continues. Fails rather than moving to another
    port when the port is taken: Apigee string-matches the registered callbackUrl, port included,
    so a different port could never complete the flow.
    """
    port, expected_path = parse_redirect_uri(redirect_uri)
    received = CallbackResult()
    finished = []

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            request_parts = urlsplit(self.path)
            if request_parts.path != expected_path:
                self.send_error(404)
                return
            query = {name: values[0] for name, values in parse_qs(request_parts.query).items()}
            if query.get("state") != expected_state:
                received.error = "state_mismatch"
                received.error_description = ("the state returned does not match this login; "
                                              "the callback did not come from the login veritas "
                                              "started")
            elif "error" in query:
                received.error = query["error"]
                received.error_description = query.get("error_description")
            elif "code" in query:
                received.code = query["code"]
            else:
                received.error = "no_code"
                received.error_description = "the callback carried neither a code nor an error"
            headline = b"Signed in" if received.code else b"Sign-in failed"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_PAGE % headline)
            finished.append(True)

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", port), CallbackHandler)
    except OSError as problem:
        raise LoopbackError(f"cannot listen on 127.0.0.1:{port} ({problem.strerror}). Something "
                            f"else is using the port; free it, because the app's registered "
                            f"callback fixes the port") from problem

    with server:
        server.timeout = 1
        if on_ready:
            on_ready()
        deadline = time.monotonic() + timeout
        while not finished:
            if time.monotonic() > deadline:
                raise LoopbackError(f"no callback within {timeout}s")
            server.handle_request()
    return received
