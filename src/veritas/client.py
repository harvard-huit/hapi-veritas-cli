"""Token logic shared by the CLI and the Python API. No printing and no exiting: every problem is
raised as VeritasError, whose message says what to run to fix it. The CLI turns those into exits;
a script or notebook can catch them."""

import os
import time
import webbrowser

from . import pkce
from .oauth import DEFAULT_REDIRECT_URI, AuthServer, LoopbackError, OAuthError, receive_callback
from .stacks import DEFAULT_STACK, StackError, resolve
from .store import StoreError, TokenStore

# Refresh this long before the AS says the token expires, so a token is never handed out with
# only a second or two of life left.
EXPIRY_SKEW_SECONDS = 60


class VeritasError(Exception):
    """A token could not be produced. ``body`` is the server's response, when there was one."""

    def __init__(self, message, body=None):
        super().__init__(message)
        self.body = body


def auth_server(stack):
    """Factory, so tests can swap the HTTP transport."""
    return AuthServer(stack)


def token_store():
    return TokenStore()


def is_expired(epoch, skew=EXPIRY_SKEW_SECONDS):
    return bool(epoch) and time.time() >= epoch - skew


def default_stack_name():
    try:
        return token_store().default_stack() or DEFAULT_STACK
    except StoreError:
        return DEFAULT_STACK


def resolve_stack(stack_name=None):
    """A Stack from a name, or from VERITAS_ENV, then the config's default_stack, then prod: the
    same order the CLI's -e uses."""
    name = stack_name or os.environ.get("VERITAS_ENV") or default_stack_name()
    try:
        return resolve(name, token_store().stack_config())
    except (StackError, StoreError) as problem:
        raise VeritasError(str(problem)) from problem


def resolve_client(store, stack, app):
    client = app or os.environ.get("VERITAS_APP") or store.active_client(stack)
    if not client:
        raise VeritasError(f"no client for {stack}: run `veritas login -e {stack} --app <APP NAME>` "
                           f"(or `veritas m2m`), or pass --app (app= in Python)")
    return client


def fresh_record(store, stack, client, force_refresh=False):
    """The stored token for (stack, client), refreshed first if it has expired (or if forced)."""
    record = store.get(stack, client)
    if not record:
        raise VeritasError(f"no token for {client} on {stack}: run "
                           f"`veritas login -e {stack} --app {client}`")
    if not force_refresh and not is_expired(record.get("expires_at")):
        return record
    if record.get("grant") == "client_credentials":
        raise VeritasError(f"the m2m token for {client} on {stack} expired and client_credentials "
                           f"has no refresh token: run `veritas m2m -e {stack}` again")
    refresh_token = record.get("refresh_token")
    if not refresh_token or is_expired(record.get("refresh_expires_at"), skew=0):
        raise VeritasError(f"the session for {client} on {stack} expired: run "
                           f"`veritas login -e {stack} --app {client}`")
    try:
        refreshed = auth_server(stack).refresh(client, refresh_token)
    except OAuthError as problem:
        raise VeritasError(f"refresh failed (HTTP {problem.status}); run `veritas login -e {stack} "
                           f"--app {client}`", problem.body) from problem
    store.put(stack, client, refreshed)
    return refreshed


def login(stack, app, port=None, redirect_uri=None, scope=None, resource=None, open_browser=True,
          timeout=300, notify=None):
    """Sign in through HarvardKey (authorization code + PKCE) as a native app, store the tokens,
    make the app the stack's active client, and return the token record. ``notify`` receives the
    progress lines (the sign-in URL among them); it defaults to discarding them."""
    notify = notify or (lambda message: None)
    store = token_store()
    client = resolve_client(store, stack, app)
    if port and redirect_uri:
        raise VeritasError("pass port or redirect_uri, not both")
    if not redirect_uri:
        redirect_uri = DEFAULT_REDIRECT_URI if not port else f"http://localhost:{port}/callback"

    server = auth_server(stack)
    verifier = pkce.make_verifier()
    state = pkce.make_state()
    sign_in_url = server.authorize_url(client, redirect_uri, pkce.challenge_for(verifier), state,
                                       scope=scope, resource=resource)

    def open_sign_in():
        if not open_browser:
            notify(f"Open this URL to sign in:\n\n  {sign_in_url}\n")
        else:
            notify(f"Opening the browser to sign in to {stack} as {client} ...")
            if not webbrowser.open(sign_in_url):
                notify(f"Could not open a browser. Open this URL:\n\n  {sign_in_url}\n")
        notify(f"Waiting for the redirect on {redirect_uri} ...")

    try:
        callback = receive_callback(redirect_uri, state, timeout=timeout, on_ready=open_sign_in)
    except LoopbackError as problem:
        raise VeritasError(str(problem)) from problem
    if callback.error:
        hint = ""
        if callback.error == "access_denied":
            hint = " (usually: you are not in a Grouper group that authorizes this app)"
        raise VeritasError(f"sign-in failed: {callback.error}{hint}", callback.error_description)

    try:
        record = server.exchange_code(client, callback.code, redirect_uri, verifier)
    except OAuthError as problem:
        raise VeritasError(f"code exchange failed (HTTP {problem.status}). A redirect_uri_mismatch "
                           f"means the app's registered callback is not {redirect_uri}",
                           problem.body) from problem

    store.put(stack, client, record)
    store.set_active_client(stack, client)
    return record
