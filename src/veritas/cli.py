"""veritas command line."""

import json
import os
import sys
import time
import webbrowser
from datetime import datetime
from urllib.parse import urlsplit

import click
import httpx

from . import __version__, pkce
from .oauth import (DEFAULT_REDIRECT_URI, AuthServer, LoopbackError, OAuthError,
                    receive_callback)
from .stacks import DEFAULT_STACK, KNOWN_STACKS, StackError, resolve
from .store import StoreError, TokenStore

# Refresh this long before the AS says the token expires, so a token is never handed out with
# only a second or two of life left.
EXPIRY_SKEW_SECONDS = 60
# The proxy BASE PATH, not its name: ais-openai-direct-v2 is served at /ais-openai-direct/v2.
DEFAULT_QUOTA_BASE_PATH = "/ais-openai-direct/v2"
# Response headers worth showing next to a `call`: the AIS quota and Apigee rate-limit ones.
QUOTA_HEADER_MARKERS = ("quota", "ratelimit", "x-ais", "remaining")


def auth_server(stack):
    """Factory, so tests can swap the HTTP transport."""
    return AuthServer(stack)


def token_store():
    return TokenStore()


# --- helpers ----------------------------------------------------------------------------------
def fail(message, body=None):
    click.secho(f"veritas: {message}", fg="red", err=True)
    if body is not None:
        click.echo(json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body,
                   err=True)
    sys.exit(1)


def describe_time(epoch):
    if not epoch:
        return "unknown"
    remaining = int(epoch - time.time())
    stamp = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    if remaining <= 0:
        return f"{stamp} (expired)"
    if remaining < 3600:
        return f"{stamp} (in {remaining // 60}m)"
    if remaining < 86400:
        return f"{stamp} (in {remaining // 3600}h)"
    return f"{stamp} (in {remaining // 86400}d)"


def is_expired(epoch, skew=EXPIRY_SKEW_SECONDS):
    return bool(epoch) and time.time() >= epoch - skew


def resolve_client(store, stack, app):
    client = app or store.active_client(stack)
    if not client:
        fail(f"no client for {stack}: run `veritas login -e {stack} --app <APP NAME>` "
             f"(or `veritas m2m`), or pass --app")
    return client


def fresh_record(store, stack, client):
    """The stored token for (stack, client), refreshed first if it has expired."""
    record = store.get(stack, client)
    if not record:
        fail(f"no token for {client} on {stack}: run `veritas login -e {stack} --app {client}`")
    if not is_expired(record.get("expires_at")):
        return record
    if record.get("grant") == "client_credentials":
        fail(f"the m2m token for {client} on {stack} expired and client_credentials has no "
             f"refresh token: run `veritas m2m -e {stack}` again")
    refresh_token = record.get("refresh_token")
    if not refresh_token or is_expired(record.get("refresh_expires_at"), skew=0):
        fail(f"the session for {client} on {stack} expired: run "
             f"`veritas login -e {stack} --app {client}`")
    try:
        refreshed = auth_server(stack).refresh(client, refresh_token)
    except OAuthError as problem:
        fail(f"refresh failed (HTTP {problem.status}); run `veritas login -e {stack} "
             f"--app {client}`", problem.body)
    store.put(stack, client, refreshed)
    return refreshed


def _default_stack_name():
    try:
        return token_store().default_stack() or DEFAULT_STACK
    except StoreError:
        return DEFAULT_STACK


def _resolve_stack(context, parameter, stack_name):
    try:
        return resolve(stack_name, token_store().stack_config())
    except StackError as problem:
        raise click.BadParameter(str(problem), ctx=context, param=parameter)


def with_stack(command):
    return click.option(
        "-e", "--env", "stack", envvar="VERITAS_ENV", default=_default_stack_name,
        callback=_resolve_stack, metavar="STACK",
        help=f"Stack to talk to: {', '.join(KNOWN_STACKS)}, or any stack configured in "
             f"config.json. Default: VERITAS_ENV, then config default_stack, then "
             f"{DEFAULT_STACK}.")(command)


def with_app(command):
    return click.option("--app", envvar="VERITAS_APP",
                        help="Client to use: the app name (login) or consumerKey (m2m). "
                             "Defaults to the stack's active client (VERITAS_APP).")(command)


# --- commands ---------------------------------------------------------------------------------
@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="veritas")
def main():
    """Get Apigee OAuth tokens from adex-oauth2-as and call AIS proxies with them."""


@main.command()
@with_stack
@with_app
@click.option("--port", type=int, default=None,
              help="Loopback port; must match the app's registered callback. [default: 8080]")
@click.option("--redirect-uri", default=None,
              help=f"Full loopback redirect URI, if not {DEFAULT_REDIRECT_URI}.")
@click.option("--scope", default=None, help="Scope(s) to request, space separated.")
@click.option("--resource", default=None, help="RFC 8707 resource; becomes the token's aud.")
@click.option("--no-browser", is_flag=True, help="Print the sign-in URL instead of opening it.")
@click.option("--timeout", type=int, default=300, show_default=True,
              help="Seconds to wait for the browser sign-in.")
def login(stack, app, port, redirect_uri, scope, resource, no_browser, timeout):
    """Sign in through HarvardKey (authorization code + PKCE) as a native app.

    --app is the Apigee APP NAME of a native (mcp-type) client. There is no secret: the AS
    resolves the name and authenticates the app itself.
    """
    store = token_store()
    client = resolve_client(store, stack, app)
    if port and redirect_uri:
        fail("pass --port or --redirect-uri, not both")
    if not redirect_uri:
        redirect_uri = DEFAULT_REDIRECT_URI if not port else f"http://localhost:{port}/callback"

    server = auth_server(stack)
    verifier = pkce.make_verifier()
    state = pkce.make_state()
    sign_in_url = server.authorize_url(client, redirect_uri, pkce.challenge_for(verifier), state,
                                       scope=scope, resource=resource)

    def open_sign_in():
        if no_browser:
            click.echo(f"Open this URL to sign in:\n\n  {sign_in_url}\n", err=True)
        else:
            click.echo(f"Opening the browser to sign in to {stack} as {client} ...", err=True)
            if not webbrowser.open(sign_in_url):
                click.echo(f"Could not open a browser. Open this URL:\n\n  {sign_in_url}\n",
                           err=True)
        click.echo(f"Waiting for the redirect on {redirect_uri} ...", err=True)

    try:
        callback = receive_callback(redirect_uri, state, timeout=timeout, on_ready=open_sign_in)
    except LoopbackError as problem:
        fail(str(problem))
    if callback.error:
        hint = ""
        if callback.error == "access_denied":
            hint = " (usually: you are not in a Grouper group that authorizes this app)"
        fail(f"sign-in failed: {callback.error}{hint}", callback.error_description)

    try:
        record = server.exchange_code(client, callback.code, redirect_uri, verifier)
    except OAuthError as problem:
        fail(f"code exchange failed (HTTP {problem.status}). A redirect_uri_mismatch means the "
             f"app's registered callback is not {redirect_uri}", problem.body)

    store.put(stack, client, record)
    store.set_active_client(stack, client)
    click.echo(f"Signed in to {stack} as {client}.")
    click.echo(f"  access token expires   {describe_time(record['expires_at'])}")
    click.echo(f"  refresh token expires  {describe_time(record['refresh_expires_at'])}")


@main.command()
@with_stack
@click.option("--client-id", envvar="APIGEE_CLIENT_ID", required=True,
              help="The app's consumerKey (APIGEE_CLIENT_ID).")
@click.option("--secret-env", default="APIGEE_CLIENT_SECRET", show_default=True,
              help="NAME of the environment variable holding the secret. Never pass the secret "
                   "itself on the command line.")
@click.option("--scope", default=None)
@click.option("--resource", default=None, help="RFC 8707 resource; becomes the token's aud.")
def m2m(stack, client_id, secret_env, scope, resource):
    """Machine-to-machine token (client_credentials). It carries no user, so it only ever
    gets the app's quota, and there is no refresh token: run this again when it expires."""
    client_secret = os.environ.get(secret_env)
    if not client_secret:
        fail(f"${secret_env} is empty: export the app's consumerSecret in it")
    try:
        record = auth_server(stack).client_credentials(client_id, client_secret, scope=scope,
                                                       resource=resource)
    except OAuthError as problem:
        fail(f"client_credentials failed (HTTP {problem.status})", problem.body)
    store = token_store()
    store.put(stack, client_id, record)
    store.set_active_client(stack, client_id)
    click.echo(f"m2m token stored for {record.get('application_name') or client_id} on {stack}; "
               f"expires {describe_time(record['expires_at'])}.")


@main.command()
@with_stack
@with_app
def token(stack, app):
    """Print a valid access token (refreshing it if needed). For $(veritas token)."""
    store = token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app))
    click.echo(record["access_token"])


@main.command("env")
@with_stack
@with_app
def env_command(stack, app):
    """Print `export ACCESS_TOKEN=...` for eval."""
    store = token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app))
    click.echo(f"export ACCESS_TOKEN='{record['access_token']}'")


@main.command()
@with_stack
@with_app
def whoami(stack, app):
    """Show who the token is for: the local record, then what /userinfo says."""
    store = token_store()
    client = resolve_client(store, stack, app)
    record = fresh_record(store, stack, client)
    click.echo(f"stack    {stack}")
    click.echo(f"client   {client}")
    click.echo(f"grant    {record['grant']}")
    click.echo(f"expires  {describe_time(record['expires_at'])}")
    if record["grant"] == "client_credentials":
        click.echo("user     none: an m2m token carries no user, so only the app quota applies")
        return
    try:
        claims = auth_server(stack).userinfo(record["access_token"])
    except OAuthError as problem:
        fail(f"/userinfo refused (HTTP {problem.status})", problem.body)
    click.echo("userinfo")
    click.echo(json.dumps(claims, indent=2, sort_keys=True))


def _read_data(data):
    if data is None:
        return None
    if data == "@-":
        return sys.stdin.buffer.read()
    if data.startswith("@"):
        with open(data[1:], "rb") as handle:
            return handle.read()
    return data.encode()


def _target_url(stack, target, any_host=False):
    """A path goes on the stack's gateway. A full URL must be https on that same host unless
    ``any_host``: the Bearer token goes wherever this points, so a typo'd or pasted URL must not
    quietly hand it to some other server."""
    if not target.startswith(("https://", "http://")):
        return f"{stack.gateway_base}/{target.lstrip('/')}"
    if any_host:
        return target
    gateway_host = urlsplit(stack.gateway_base).hostname
    target_parts = urlsplit(target)
    if target_parts.scheme != "https" or target_parts.hostname != gateway_host:
        fail(f"{target} is not on {stack}'s gateway (https://{gateway_host}); the token would be "
             f"sent there. Pass a path, the right -e, or --any-host if you mean it")
    return target


def _send(access_token, method, url, headers, body):
    request_headers = {"Authorization": f"Bearer {access_token}"}
    for header in headers:
        name, separator, value = header.partition(":")
        if not separator:
            fail(f"header {header!r} is not NAME: VALUE")
        request_headers[name.strip()] = value.strip()
    if body is not None and not any(name.lower() == "content-type" for name in request_headers):
        request_headers["Content-Type"] = "application/json"
    try:
        return httpx.request(method, url, headers=request_headers, content=body, timeout=180)
    except httpx.HTTPError as problem:
        fail(f"{method} {url} failed: {problem}")


@main.command()
@with_stack
@with_app
@click.argument("target")
@click.option("-X", "--request", "method", default=None,
              help="HTTP method. [default: GET, or POST when -d is given]")
@click.option("-d", "--data", default=None,
              help="Request body: a literal, @file, or @- for stdin. JSON by default.")
@click.option("-H", "--header", "headers", multiple=True, help="Extra header, 'Name: value'.")
@click.option("--any-host", is_flag=True,
              help="Allow a full URL off the stack's gateway (the token is sent there).")
def call(stack, app, target, method, data, headers, any_host):
    """Call a proxy with the Bearer token. TARGET is a path on <stack>.apis.huit.harvard.edu
    (e.g. /ais-openai-direct-v2/v1/models) or a full URL.

    The body goes to stdout; the status and quota headers go to stderr, so the body can be piped.
    Exits 1 on an HTTP error status.
    """
    url = _target_url(stack, target, any_host)
    store = token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app))
    body = _read_data(data)
    method = (method or ("POST" if body is not None else "GET")).upper()
    response = _send(record["access_token"], method, url, headers, body)

    click.secho(f"HTTP {response.status_code} {method} {url}", err=True,
                fg="green" if response.is_success else "red")
    for name, value in response.headers.items():
        if any(marker in name.lower() for marker in QUOTA_HEADER_MARKERS):
            click.echo(f"  {name}: {value}", err=True)
    sys.stdout.buffer.write(response.content)
    if response.content and not response.content.endswith(b"\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()
    if not response.is_success:
        sys.exit(1)


@main.command()
@with_stack
@with_app
@click.argument("base_path", default=DEFAULT_QUOTA_BASE_PATH, required=False)
def quota(stack, app, base_path):
    """Show the app's quota, and the user's when the token carries one, from
    GET <BASE_PATH>/apigee/quota. BASE_PATH is the proxy's base path, not its name. Each
    quota name has its own counters. Today's AIS base paths: /ais-openai-direct/v2 (default),
    /ais-openai-direct-hdsi/v2, /ais-openai-direct-comdev/v2, /ais-google-gemini,
    /ais-bedrock-llm."""
    store = token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app))
    url = f"{stack.gateway_base}/{base_path.strip('/')}/apigee/quota"
    response = _send(record["access_token"], "GET", url, (), None)
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if not response.is_success:
        fail(f"GET {url} returned HTTP {response.status_code}", payload)
    click.echo(json.dumps(payload, indent=2) if isinstance(payload, dict) else payload)


@main.command()
@with_stack
@with_app
def logout(stack, app):
    """Forget the stored token for the client (the active one by default)."""
    store = token_store()
    client = resolve_client(store, stack, app)
    store.delete(stack, client)
    if store.active_client(stack) == client:
        store.set_active_client(stack, None)
    click.echo(f"Removed the {stack} token for {client}.")


@main.command()
@with_stack
def discover(stack):
    """Print the authorization server's metadata (no credentials needed)."""
    try:
        click.echo(json.dumps(auth_server(stack).metadata(), indent=2))
    except OAuthError as problem:
        fail(f"discovery failed (HTTP {problem.status})", problem.body)
    except httpx.HTTPError as problem:
        fail(f"discovery failed: {problem}")


def run():
    try:
        main()
    except StoreError as problem:
        fail(str(problem))


if __name__ == "__main__":
    run()
