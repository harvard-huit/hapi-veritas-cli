"""veritas command line."""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import click
import httpx

from . import __version__
from . import client as core
from .client import VeritasError
from .oauth import DEFAULT_REDIRECT_URI, OAuthError
from .stacks import DEFAULT_STACK, KNOWN_STACKS, StackError, resolve
from .store import StoreError, veritas_home, write_private_json
# The proxy BASE PATH, not its name: ais-openai-direct-v2 is served at /ais-openai-direct/v2.
DEFAULT_QUOTA_BASE_PATH = "/ais-openai-direct/v2"
# Response headers worth showing next to a `call`: the AIS quota and Apigee rate-limit ones.
QUOTA_HEADER_MARKERS = ("quota", "ratelimit", "x-ais", "remaining")
# `veritas claude`: Claude Code in Bedrock mode, pointed at the gateway's pass-through Bedrock proxy.
BEDROCK_PROXY_PATH = "/ais-bedrock-llm/v2"
DEFAULT_BEDROCK_REGION = "us-east-1"
# `veritas codex`: Codex on the gateway's OpenAI proxy (it appends /responses and /models).
OPENAI_PROXY_PATH = "/ais-openai-direct/v2"
# How often Codex re-runs `veritas token` on its own. It also re-runs it on a 401, so this only
# has to beat the one-hour access token, not match it.
CODEX_TOKEN_REFRESH_MS = 300_000
# Credentials Claude Code would prefer over the gateway token, or send in its place. Removed from
# the child's environment so a key exported in a profile can't silently win.
COMPETING_CLAUDE_CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                                "AWS_BEARER_TOKEN_BEDROCK")
# How this same CLI is re-run as a token helper (Claude Code's apiKeyHelper, Codex's auth.command).
# `-P` keeps the working directory off sys.path: both tools run the helper from the user's project,
# and with plain `-m` a `veritas/` or `click.py` there would run instead of veritas, with keychain
# access and its output sent as the bearer token. `-P` needs Python 3.11, which pyproject requires.
TOKEN_HELPER_ARGUMENTS = ("-P", "-m", "veritas.cli", "token")
# Windows has no exec: os.execve there starts a SEPARATE process and returns, leaving veritas and
# Claude Code fighting over one console, and it cannot run npm's claude.cmd at all.
WINDOWS = os.name == "nt"


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


def resolve_client(store, stack, app):
    try:
        return core.resolve_client(store, stack, app)
    except VeritasError as problem:
        fail(str(problem), problem.body)


def fresh_record(store, stack, client, force_refresh=False):
    try:
        return core.fresh_record(store, stack, client, force_refresh=force_refresh)
    except VeritasError as problem:
        fail(str(problem), problem.body)


def _resolve_stack(context, parameter, stack_name):
    try:
        return resolve(stack_name, core.token_store().stack_config())
    except StackError as problem:
        raise click.BadParameter(str(problem), ctx=context, param=parameter)


def with_stack(command):
    return click.option(
        "-e", "--env", "stack", envvar="VERITAS_ENV", default=core.default_stack_name,
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
    try:
        record = core.login(stack, app, port=port, redirect_uri=redirect_uri, scope=scope,
                            resource=resource, open_browser=not no_browser, timeout=timeout,
                            notify=lambda message: click.echo(message, err=True))
    except VeritasError as problem:
        fail(str(problem), problem.body)
    client = record["client"]
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
        record = core.auth_server(stack).client_credentials(client_id, client_secret, scope=scope,
                                                       resource=resource)
    except OAuthError as problem:
        fail(f"client_credentials failed (HTTP {problem.status})", problem.body)
    store = core.token_store()
    store.put(stack, client_id, record)
    store.set_active_client(stack, client_id)
    click.echo(f"m2m token stored for {record.get('application_name') or client_id} on {stack}; "
               f"expires {describe_time(record['expires_at'])}.")


@main.command()
@with_stack
@with_app
@click.option("--refresh", "force_refresh", is_flag=True,
              help="Refresh now, even if the access token is still valid (e.g. to test what a "
                   "refresh does after a permission change).")
def token(stack, app, force_refresh):
    """Print a valid access token (refreshing it if needed). For $(veritas token)."""
    store = core.token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app), force_refresh=force_refresh)
    if force_refresh:
        click.echo(f"refreshed: access token expires {describe_time(record['expires_at'])}", err=True)
    click.echo(record["access_token"])


@main.command()
@with_stack
@with_app
def header(stack, app):
    """Print `Authorization: Bearer <token>`, refreshed if needed. For curl -H "$(veritas header)"."""
    store = core.token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app))
    click.echo(f"Authorization: Bearer {record['access_token']}")


@main.command("env")
@with_stack
@with_app
def env_command(stack, app):
    """Print `export ACCESS_TOKEN=...` for eval."""
    store = core.token_store()
    record = fresh_record(store, stack, resolve_client(store, stack, app))
    click.echo(f"export ACCESS_TOKEN='{record['access_token']}'")


@main.command()
@with_stack
@with_app
def whoami(stack, app):
    """Show who the token is for: the local record, then what /userinfo says."""
    store = core.token_store()
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
        claims = core.auth_server(stack).userinfo(record["access_token"])
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
    store = core.token_store()
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
    store = core.token_store()
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
    store = core.token_store()
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
        click.echo(json.dumps(core.auth_server(stack).metadata(), indent=2))
    except OAuthError as problem:
        fail(f"discovery failed (HTTP {problem.status})", problem.body)
    except httpx.HTTPError as problem:
        fail(f"discovery failed: {problem}")


def claude_environment(stack, base_environment):
    """The environment Claude Code runs with: Bedrock mode against the stack's gateway, no AWS
    signing (the gateway authenticates), and no competing credential."""
    environment = {name: value for name, value in base_environment.items()
                   if name not in COMPETING_CLAUDE_CREDENTIALS}
    environment.update({
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": f"{stack.gateway_base}{BEDROCK_PROXY_PATH}",
    })
    environment.setdefault("AWS_REGION", DEFAULT_BEDROCK_REGION)
    return environment


def helper_command(stack, client, windows=WINDOWS):
    """The apiKeyHelper command line: this same CLI by interpreter path, so it works without
    `veritas` on PATH and in whatever virtualenv this one runs from. On Windows, Claude Code may run
    the helper through cmd.exe or Git Bash; double-quoted arguments and a forward-slash path read
    the same in both (bash would eat unquoted backslashes, cmd ignores single quotes)."""
    arguments = [sys.executable, *TOKEN_HELPER_ARGUMENTS, "-e", stack.name, "--app", client]
    if not windows:
        return shlex.join(arguments)
    arguments[0] = arguments[0].replace("\\", "/")
    return " ".join(f'"{argument}"' for argument in arguments)


def claude_settings_file(stack, client, windows=WINDOWS):
    """Write Claude Code's settings for this stack and client, and return the file's path.

    apiKeyHelper is how Claude Code refreshes a gateway token mid-session: it re-runs the helper
    (every 5 minutes, and on a 401) and sends the output as `Authorization: Bearer`. The settings go
    in a FILE rather than an inline JSON argument because cmd.exe, which runs npm's claude.cmd on
    Windows, mangles the quotes in an inline JSON string. One file per stack and client, so two
    sessions on different apps never rewrite each other's helper."""
    safe_client = re.sub(r"[^A-Za-z0-9._-]", "_", client)
    path = veritas_home() / "claude" / f"{stack.name}-{safe_client}.json"
    write_private_json(path, {"apiKeyHelper": helper_command(stack, client, windows)})
    return path


# cmd.exe re-reads the command line of a .cmd/.bat target (npm installs claude.cmd and codex.cmd),
# ignoring Python's \" escaping and expanding %VAR%: these characters in an argument can corrupt the
# command or run another one. `!` only matters with delayed expansion, which npm's shims don't use.
CMD_METACHARACTERS = frozenset('"%^&|<>\r\n')


def find_program(name, windows=WINDOWS, environment=None):
    """The program's path on PATH only. On Windows, shutil.which searches the current directory
    first, so a `codex.cmd` committed to the repo you are in would run instead of the real one."""
    environment = os.environ if environment is None else environment
    if not windows:
        return shutil.which(name, path=environment.get("PATH"))
    extensions = [extension for extension in environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
                  if extension]
    candidates = [name] if os.path.splitext(name)[1] else []
    candidates += [name + extension for extension in extensions]
    for directory in environment.get("PATH", "").split(";"):
        directory = directory.strip().strip('"')
        if directory in ("", "."):
            continue
        for candidate in candidates:
            program = os.path.join(directory, candidate)
            if os.path.isfile(program):
                return program
    return None


def cmd_shim_problem(arguments):
    """The first argument cmd.exe would misread, with the offending characters, when the target is
    a .cmd/.bat shim; None when the launch is safe."""
    if os.path.splitext(arguments[0])[1].lower() not in (".cmd", ".bat"):
        return None
    for argument in arguments:
        found = sorted(CMD_METACHARACTERS & set(argument))
        if found:
            return argument, found
    return None


def launch(arguments, environment, windows=WINDOWS):
    """Replace this process with the tool (Claude Code, Codex); on Windows, run it as a child and
    exit with its code."""
    if not windows:
        os.execve(arguments[0], arguments, environment)
    problem = cmd_shim_problem(arguments)
    if problem:
        argument, characters = problem
        fail(f"{os.path.basename(arguments[0])} is a cmd.exe script, which would misread "
             f"{' '.join(repr(character) for character in characters)} in {argument!r}. Leave those "
             f"characters out (put a long prompt in a file and point the tool at it)")
    process = subprocess.Popen(arguments, env=environment)
    while True:
        try:
            sys.exit(process.wait())
        except KeyboardInterrupt:
            continue   # Ctrl+C belongs to the tool, which shares the console; keep waiting


def user_record(store, stack, client):
    """The client's token record, refreshed if needed, for a tool that acts as the user. An m2m
    token is refused before any refresh: it names no user, and an expired one would otherwise
    send the user to `veritas m2m`, only to be refused afterwards."""
    stored = store.get(stack, client)
    if stored and stored.get("grant") == "client_credentials":
        fail(f"{client} holds an m2m token, which names no user: sign in with "
             f"`veritas login -e {stack} --app <APP NAME>`")
    return fresh_record(store, stack, client)   # fail here, before launching, if not signed in


# Veritas options come first; parsing stops at the tool's first argument, so everything from there
# on (its own -e, --help, `--`) reaches the tool unchanged.
PASSTHROUGH_COMMAND = {"ignore_unknown_options": True, "allow_extra_args": True,
                       "allow_interspersed_args": False}


@main.command(context_settings=PASSTHROUGH_COMMAND)
@with_stack
@with_app
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def claude(stack, app, claude_args):
    """Run Claude Code through the stack's Bedrock proxy as you, on your user token.

    Every call is charged to your per-user AI Services limit inside the app's. Put the veritas
    options first: everything from claude's first argument on goes to claude unchanged, e.g.
    `veritas claude -e dev -p "hello"` or `veritas claude -e dev --model us.anthropic.claude-sonnet-5-5`."""
    store = core.token_store()
    client = resolve_client(store, stack, app)
    user_record(store, stack, client)
    claude_path = find_program("claude")
    if not claude_path:
        fail("claude (Claude Code) is not on PATH: install it first")
    arguments = [claude_path, "--settings", str(claude_settings_file(stack, client)), *claude_args]
    launch(arguments, claude_environment(stack, os.environ))


class CodexConfigError(Exception):
    pass


def codex_provider_id(stack):
    return f"veritas-{stack.name}"


def codex_provider_name(stack, client):
    return f"Veritas {stack.name} {client}"


def codex_token_command(stack, client):
    """`veritas token` by interpreter path, as Codex's provider auth command. Codex runs it with no
    shell (command + argument array), so no quoting is needed on any platform."""
    return sys.executable, [*TOKEN_HELPER_ARGUMENTS, "-e", stack.name, "--app", client]


def codex_provider_settings(stack, client):
    """Codex settings, as (key under the provider, value) pairs, for a provider that sends the
    user token.

    `auth.command` is Codex's counterpart of Claude Code's apiKeyHelper: its output goes as
    `Authorization: Bearer`, re-fetched every refresh_interval_ms and on a 401. Nothing secret is
    in here, only the command that fetches the token from the veritas store."""
    command, arguments = codex_token_command(stack, client)
    return [
        ("name", codex_provider_name(stack, client)),
        ("base_url", f"{stack.gateway_base}{OPENAI_PROXY_PATH}"),
        ("wire_api", "responses"),
        ("auth.command", command),
        ("auth.args", arguments),
        ("auth.refresh_interval_ms", CODEX_TOKEN_REFRESH_MS),
    ]


def toml_value(value):
    """A TOML literal for a bool, integer, string or list of them.

    Strings are TOML literal strings ('...') whenever they can be: no escapes, so any Unicode
    text survives, and no double quotes reach the command line (cmd.exe misreads those). A string
    holding ' or a control character falls back to a basic string with JSON escapes."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    text = str(value)
    if any(0xD800 <= ord(character) <= 0xDFFF for character in text):
        raise CodexConfigError(f"{text!r} is not valid Unicode (undecodable bytes in a path?), so "
                               f"it cannot go in a Codex config")
    if "'" not in text and not any(ord(character) < 0x20 and character != "\t" or ord(character) == 0x7F
                                   for character in text):
        return f"'{text}'"
    return json.dumps(text, ensure_ascii=False)


def codex_overrides(stack, client):
    """`-c key=value` flags selecting the veritas provider for this one run, leaving
    ~/.codex/config.toml untouched."""
    provider = f"model_providers.{codex_provider_id(stack)}"
    settings = [("model_provider", codex_provider_id(stack)),
                *((f"{provider}.{key}", value) for key, value in codex_provider_settings(stack, client))]
    flags = []
    for key, value in settings:
        flags += ["-c", f"{key}={toml_value(value)}"]
    return flags


def codex_provider_tables(stack, client):
    """The provider as config.toml tables (no top-level keys)."""
    provider_id = codex_provider_id(stack)
    plain = [(key, value) for key, value in codex_provider_settings(stack, client)
             if not key.startswith("auth.")]
    auth = [(key[len("auth."):], value) for key, value in codex_provider_settings(stack, client)
            if key.startswith("auth.")]
    lines = [f"[model_providers.{provider_id}]"]
    lines += [f"{key} = {toml_value(value)}" for key, value in plain]
    lines += ["", f"[model_providers.{provider_id}.auth]"]
    lines += [f"{key} = {toml_value(value)}" for key, value in auth]
    return "\n".join(lines)


def codex_config_block(stack, client):
    """The provider for pasting into config.toml by hand (`--print-config`)."""
    provider_id = codex_provider_id(stack)
    return "\n".join([
        f"# Paste these tables into ~/.codex/config.toml, replacing any earlier [model_providers.{provider_id}].",
        f"# Select the provider with a top-level  model_provider = \"{provider_id}\"  ABOVE the first",
        "# [table] in the file (below a table it would belong to that table).",
        "# Or let veritas do both: veritas codex --install-config",
        codex_provider_tables(stack, client),
    ])


def codex_config_path():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def _managed_markers(provider_id):
    return (f"# >>> {provider_id}: managed by `veritas codex --install-config`; edits here are replaced >>>",
            f"# <<< {provider_id} <<<")


def install_codex_config(text, stack, client):
    """config.toml text with this stack's veritas provider installed and selected, and the
    model_provider it replaces (None if there was none). Re-running replaces the managed block,
    so installing again (after a reinstall, or for another app on the stack) never duplicates it.
    The result is parsed before it is returned; anything unexpected raises CodexConfigError and
    the caller writes nothing."""
    import tomllib
    provider_id = codex_provider_id(stack)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as problem:
        raise CodexConfigError(f"it is not valid TOML ({problem}); fix it first")
    start, end = _managed_markers(provider_id)
    text = re.sub(rf"\n*^{re.escape(start)}\n.*?^{re.escape(end)}\n?", "\n", text,
                  flags=re.DOTALL | re.MULTILINE)
    if provider_id in tomllib.loads(text).get("model_providers", {}):
        raise CodexConfigError(f"it already defines [model_providers.{provider_id}] outside the "
                               f"block veritas manages (pasted from --print-config?): delete that "
                               f"table, then run this again")
    lines = text.splitlines(keepends=True)
    first_table = next((index for index, line in enumerate(lines) if re.match(r"\s*\[", line)), len(lines))
    selection = f"model_provider = {toml_value(provider_id)}\n"
    previous = tomllib.loads(text).get("model_provider")
    for index in range(first_table):
        if re.match(r"\s*model_provider\s*=", lines[index]):
            lines[index] = selection
            break
    else:
        lines.insert(0, selection)
    text = "".join(lines).rstrip("\n")
    text = (text + "\n\n" if text else "") + "\n".join([start, codex_provider_tables(stack, client), end]) + "\n"
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as problem:
        raise CodexConfigError(f"editing it would not leave valid TOML ({problem})")
    installed = parsed.get("model_providers", {}).get(provider_id, {})
    if parsed.get("model_provider") != provider_id or \
            installed.get("auth", {}).get("args") != codex_token_command(stack, client)[1]:
        raise CodexConfigError("its layout is unusual enough that the edit could not be verified")
    return text, previous


def write_codex_config(path, text):
    """Replace config.toml atomically, keeping its mode and a copy of the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".bak-veritas")
    mode = 0o600
    if path.exists():
        mode = path.stat().st_mode & 0o777
        shutil.copy2(path, backup)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return backup if backup.exists() else None


def shell_only_overrides(stack):
    """Veritas settings that live only in this shell's environment. Codex started by the IDE or
    the desktop app won't have them, so its `veritas token` runs without them."""
    scoped = stack.name.upper().replace("-", "_")
    names = ["VERITAS_HOME", "VERITAS_TOKEN_STORE", f"VERITAS_{scoped}_AUTH_BASE",
             f"VERITAS_{scoped}_GATEWAY_BASE"]
    return [name for name in names if os.environ.get(name)]


@main.command(context_settings=PASSTHROUGH_COMMAND)
@with_stack
@with_app
@click.option("--install-config", is_flag=True,
              help="Add the provider to ~/.codex/config.toml (or $CODEX_HOME) and select it, for "
                   "plain `codex`, the IDE extension and the desktop app. Re-running replaces it.")
@click.option("--print-config", is_flag=True,
              help="Print the provider as config.toml tables to paste by hand, instead of running "
                   "Codex.")
@click.argument("codex_args", nargs=-1, type=click.UNPROCESSED)
def codex(stack, app, install_config, print_config, codex_args):
    """Run OpenAI Codex through the stack's OpenAI proxy as you, on your user token.

    Every call is charged to your per-user AI Services limit inside the app's. Put the veritas
    options first: everything from codex's first argument on goes to codex unchanged, e.g.
    `veritas codex -e dev exec "hello"` or `veritas codex -e dev -m <model>`."""
    store = core.token_store()
    client = resolve_client(store, stack, app)
    user_record(store, stack, client)
    if install_config and print_config:
        fail("use --install-config or --print-config, not both")
    try:
        if print_config:
            click.echo(codex_config_block(stack, client))
            return
        if install_config:
            path = codex_config_path()
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            try:
                text, previous = install_codex_config(current, stack, client)
            except CodexConfigError as problem:
                fail(f"left {path} unchanged: {problem}")
            backup = write_codex_config(path, text)
            provider_id = codex_provider_id(stack)
            click.echo(f"Installed {provider_id} ({client}) in {path} and selected it: plain `codex`, "
                       f"the IDE extension and the desktop app now run on your {stack} user token.")
            if previous and previous != provider_id:
                click.echo(f"It replaces model_provider = \"{previous}\"; set that back to switch.")
            if backup:
                click.echo(f"Previous version: {backup}")
            overrides = shell_only_overrides(stack)
            if overrides:
                click.secho(f"Warning: {', '.join(overrides)} set in this shell won't reach Codex "
                            f"started from the IDE or desktop app.", fg="yellow", err=True)
            return
        overrides = codex_overrides(stack, client)
    except CodexConfigError as problem:
        fail(str(problem))
    codex_path = find_program("codex")
    if not codex_path:
        fail("codex (OpenAI Codex CLI) is not on PATH: install it first, or use "
             f"`veritas codex -e {stack} --install-config` to configure the IDE extension")
    launch([codex_path, *overrides, *codex_args], dict(os.environ))


def run():
    try:
        main()
    except StoreError as problem:
        fail(str(problem))


if __name__ == "__main__":
    run()
