"""The Python API: an Authorization header for whoever ran `veritas login`.

Built for skills, functions and classroom notebooks that call HUIT APIs as the person running
them. Nobody needs an API subscription or their own Apigee app: one registered user-token app
(say a workshop's), and each person signs in to it once with HarvardKey. Tokens live in the same
store as the CLI's, so a `veritas login` in a terminal is picked up here, and the other way round.

    import veritas

    veritas.headers(app="awa-workshop_x")    # {"Authorization": "Bearer ..."}
    httpx.get(url, auth=veritas.Auth(app="awa-workshop_x"))
    requests.get(url, auth=veritas.Auth(app="awa-workshop_x"))

Every function takes ``stack`` (default: VERITAS_ENV, then the config's default_stack, then prod)
and ``app`` (default: VERITAS_APP, then the stack's active client). Nothing here opens a browser
except login(): a missing or expired session raises VeritasError saying what to run, so a script
fails fast instead of hanging on a sign-in page.
"""

import httpx

from . import client as core
from .client import VeritasError


def _record(stack=None, app=None, force_refresh=False):
    resolved = core.resolve_stack(stack)
    store = core.token_store()
    return core.fresh_record(store, resolved, core.resolve_client(store, resolved, app),
                             force_refresh=force_refresh)


def token(stack=None, app=None, force_refresh=False):
    """A valid access token, refreshed first if it has expired (or ``force_refresh``)."""
    return _record(stack, app, force_refresh)["access_token"]


def authorization(stack=None, app=None):
    """The Authorization header VALUE: ``"Bearer <token>"``."""
    return f"Bearer {token(stack, app)}"


def headers(stack=None, app=None):
    """``{"Authorization": "Bearer <token>"}``, to merge into any request's headers."""
    return {"Authorization": authorization(stack, app)}


def gateway(stack=None):
    """The stack's API gateway base URL, e.g. ``https://dev.apis.huit.harvard.edu``."""
    return core.resolve_stack(stack).gateway_base


def login(app=None, stack=None, port=None, redirect_uri=None, open_browser=True, timeout=300,
          notify=print):
    """Sign in through HarvardKey in the browser and store the tokens (same as `veritas login`).
    Returns the stack and client it signed in to. ``app`` is the Apigee app name."""
    resolved = core.resolve_stack(stack)
    record = core.login(resolved, app, port=port, redirect_uri=redirect_uri,
                        open_browser=open_browser, timeout=timeout, notify=notify)
    return {"stack": resolved.name, "client": record["client"], "expires_at": record["expires_at"]}


class Auth(httpx.Auth):
    """Sends ``Authorization: Bearer`` with a token fetched for EACH request, so a long-running
    notebook or service never sends an expired one. Works as ``auth=`` for both httpx and requests.
    On a 401 it refreshes once and retries, which covers a token revoked before its expiry."""

    def __init__(self, stack=None, app=None):
        self.stack = stack
        self.app = app

    # httpx
    def auth_flow(self, request):
        request.headers["Authorization"] = authorization(self.stack, self.app)
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {token(self.stack, self.app, force_refresh=True)}"
            yield request

    # requests: an auth is any callable that takes and returns the PreparedRequest
    def __call__(self, request):
        request.headers["Authorization"] = authorization(self.stack, self.app)
        return request


__all__ = ["Auth", "VeritasError", "authorization", "gateway", "headers", "login", "token"]
