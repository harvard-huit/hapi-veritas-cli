"""veritas command line."""

import copy
import json
import ntpath
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
# command or run another one.
CMD_METACHARACTERS = frozenset('"%^&|<>\r\n')
# With delayed expansion on, cmd.exe also replaces !VAR! with that variable's value (a prompt holding
# !OPENAI_API_KEY! would send the key) and deletes a lone `!`. It is off unless the registry turns
# it on, so `!` is refused only then: "Fix this!" stays usable everywhere else.
DELAYED_EXPANSION_KEY = r"Software\Microsoft\Command Processor"


def find_program(name, windows=WINDOWS, environment=None):
    """The program's path on PATH only. On Windows, shutil.which searches the current directory
    first, so a `codex.cmd` committed to the repo you are in would run instead of the real one;
    relative PATH entries (`bin`, `.\\tools`) would lead back into it too, so they are skipped."""
    environment = os.environ if environment is None else environment
    if not windows:
        return shutil.which(name, path=environment.get("PATH"))
    extensions = [extension for extension in environment.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
                  if extension]
    candidates = [name] if os.path.splitext(name)[1] else []
    candidates += [name + extension for extension in extensions]
    for directory in environment.get("PATH", "").split(";"):
        directory = directory.strip().strip('"')
        if not directory or not ntpath.isabs(directory):
            continue
        for candidate in candidates:
            program = os.path.join(directory, candidate)
            if os.path.isfile(program):
                return program
    return None


def delayed_expansion_enabled(windows=WINDOWS):
    """Whether cmd.exe runs with delayed expansion by default: the DelayedExpansion DWORD under
    HKCU, or HKLM when HKCU has none (`cmd /?`: the per-user setting takes precedence)."""
    if not windows:
        return False
    try:
        import winreg
    except ImportError:   # only when Windows is simulated elsewhere (tests); real Windows has it
        return False
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, DELAYED_EXPANSION_KEY) as key:
                value, _ = winreg.QueryValueEx(key, "DelayedExpansion")
        except OSError:
            continue   # no key or no value in this hive: ask the next one
        return value == 1
    return False


def cmd_shim_problem(arguments, delayed_expansion=False):
    """The first argument cmd.exe would misread, with the offending characters, when the target is
    a .cmd/.bat shim; None when the launch is safe."""
    if os.path.splitext(arguments[0])[1].lower() not in (".cmd", ".bat"):
        return None
    unsafe = CMD_METACHARACTERS | {"!"} if delayed_expansion else CMD_METACHARACTERS
    for argument in arguments:
        found = sorted(unsafe & set(argument))
        if found:
            return argument, found
    return None


def describe_cmd_problem(characters):
    named = " ".join(repr(character) for character in characters)
    if "!" in characters:
        named += (" (delayed expansion is on for cmd.exe in this Windows account: DelayedExpansion=1 "
                  "under HKCU or HKLM\\" + DELAYED_EXPANSION_KEY + ")")
    return named


def launch(arguments, environment, windows=WINDOWS):
    """Replace this process with the tool (Claude Code, Codex); on Windows, run it as a child and
    exit with its code."""
    if not windows:
        os.execve(arguments[0], arguments, environment)
    problem = cmd_shim_problem(arguments, delayed_expansion_enabled(windows))
    if problem:
        argument, characters = problem
        fail(f"{os.path.basename(arguments[0])} is a cmd.exe script, which would misread "
             f"{describe_cmd_problem(characters)} in {argument!r}. Leave those characters out (put "
             f"a long prompt in a file and point the tool at it)")
    process = subprocess.Popen(arguments, env=environment)
    while True:
        try:
            sys.exit(process.wait())
        except KeyboardInterrupt:
            continue   # Ctrl+C belongs to the tool, which shares the console; keep waiting


# What an app name (or m2m consumerKey) may contain before it goes into a helper command line that
# Claude Code may run through cmd.exe or Git Bash. Apigee app names are letters, digits, spaces
# and . - _ ; nothing here is special to either shell inside double quotes.
CLIENT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")


def user_record(store, stack, client):
    """The client's token record, refreshed if needed, for a tool that acts as the user. An m2m
    token is refused before any refresh: it names no user, and an expired one would otherwise
    send the user to `veritas m2m`, only to be refused afterwards."""
    if not CLIENT_NAME_PATTERN.match(client):
        fail(f"app name {client!r} has characters an Apigee app name can't have (letters, digits, "
             f"spaces and . - _ only)")
    stored = store.get(stack, client)
    if stored and stored.get("grant") == "client_credentials":
        fail(f"{client} holds an m2m token, which names no user: sign in with "
             f"`veritas login -e {stack} --app <APP NAME>`")
    return fresh_record(store, stack, client)   # fail here, before launching, if not signed in


# Veritas reads its own options (-e, --app, ...) until the first word that isn't an option, and
# passes everything from there on to the tool unchanged: its own -e, --help and `--` included. Put
# the veritas options first, before any of the tool's options.
PASSTHROUGH_COMMAND = {"ignore_unknown_options": True, "allow_extra_args": True,
                       "allow_interspersed_args": False}


@main.command(context_settings=PASSTHROUGH_COMMAND)
@with_stack
@with_app
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def claude(stack, app, claude_args):
    """Run Claude Code through the stack's Bedrock proxy as you, on your user token.

    Every call is charged to your per-user AI Services limit inside the app's. Put the veritas
    options first, before any claude option: e.g. `veritas claude -e dev -p "hello"` or
    `veritas claude -e dev --model us.anthropic.claude-sonnet-5-5`."""
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


def _has_control_characters(text):
    return any((ord(character) < 0x20 and character != "\t") or ord(character) == 0x7F
               for character in text)


LITERAL_QUOTE = "'"
MULTILINE_LITERAL_QUOTE = LITERAL_QUOTE * 3


def toml_value(value):
    """A TOML literal for a bool, integer, string or list of them.

    Strings are TOML literal strings (single-quoted, or triple-single-quoted when they hold a single
    quote), so any Unicode text survives without escapes and no double quotes reach the command
    line (cmd.exe misreads them). Only a string with a control character or three single quotes in
    a row falls back to a basic string with escapes."""
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
    if not _has_control_characters(text):
        if LITERAL_QUOTE not in text:
            return LITERAL_QUOTE + text + LITERAL_QUOTE
        if MULTILINE_LITERAL_QUOTE not in text:
            return MULTILINE_LITERAL_QUOTE + text + MULTILINE_LITERAL_QUOTE
    # json.dumps leaves DEL unescaped, which TOML forbids.
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


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


def codex_provider_tables(stack, client, newline="\n"):
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
    return newline.join(lines)


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


def _managed_comment(provider_id):
    return (f"# {provider_id}: managed by `veritas codex --install-config`, which replaces the two "
            f"tables below")


def _is_marker(line, provider_id):
    """This version's comment, or the start and end markers the first 0.3.0 build wrote."""
    stripped = line.strip()
    return (stripped.startswith(f"# >>> {provider_id}: managed by `veritas codex --install-config`")
            or stripped == f"# <<< {provider_id} <<<"
            or stripped == _managed_comment(provider_id))


def _parse_toml(text, what):
    import tomllib
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as problem:
        raise CodexConfigError(f"{what} ({problem})")


def _without_provider(parsed, provider_id):
    """``parsed`` as it should read once this provider's tables are gone."""
    expected = copy.deepcopy(parsed)
    providers = expected.get("model_providers")
    if isinstance(providers, dict):
        providers.pop(provider_id, None)
        if not providers:
            del expected["model_providers"]
    return expected


def install_codex_config(text, stack, client):
    """config.toml text with this stack's veritas provider installed and selected, and the
    model_provider it replaces (None if there was none).

    The edit is by lines, so comments and layout elsewhere survive, but it is checked by meaning:
    with the provider's two tables removed, the file must parse to exactly what it held before
    minus that provider, and the final file to that plus the new provider and model_provider.
    Anything else raises CodexConfigError and the caller writes nothing. Tables Codex adds later
    (MCP servers, trusted projects) are separate tables, so re-running never removes them."""
    provider_id = codex_provider_id(stack)
    original = _parse_toml(text, "Python's TOML parser can't read it; veritas only writes files it "
                                 "can check, so paste the --print-config tables by hand instead. "
                                 "Parser said")
    newline = "\r\n" if "\r\n" in text else "\n"
    owned_header = re.compile(rf"\s*\[\s*model_providers\.{re.escape(provider_id)}(\.auth)?\s*\]\s*(#.*)?$")
    any_header = re.compile(r"\s*\[")
    lines = text.splitlines(keepends=True)
    removed = set()
    for index, line in enumerate(lines):
        if _is_marker(line, provider_id):
            removed.add(index)
        if not owned_header.match(line):
            continue
        span_end = next((later for later in range(index + 1, len(lines)) if any_header.match(lines[later])),
                        len(lines))
        # Comments and blank lines just above the next table belong to that table, not to ours.
        while span_end > index + 1 and (not lines[span_end - 1].strip()
                                        or lines[span_end - 1].lstrip().startswith("#")) \
                and not _is_marker(lines[span_end - 1], provider_id):
            span_end -= 1
        removed.update(range(index, span_end))
    kept = [line for index, line in enumerate(lines) if index not in removed]

    expected = _without_provider(original, provider_id)
    if _parse_toml("".join(kept), "removing the old veritas tables would break it") != expected:
        raise CodexConfigError(f"[model_providers.{provider_id}] is laid out in a way veritas can't "
                               f"replace without touching anything else (an inline table?): delete "
                               f"it by hand, then run this again")

    first_table = next((index for index, line in enumerate(kept) if any_header.match(line)), len(kept))
    selection = f"model_provider = {toml_value(provider_id)}{newline}"
    for index in range(first_table):
        if re.match(r"\s*model_provider\s*=", kept[index]):
            kept[index] = selection
            break
    else:
        kept.insert(0, selection)
    body = "".join(kept).rstrip("\r\n")
    tables = codex_provider_tables(stack, client, newline)
    result = (body + newline * 2 if body else "") + _managed_comment(provider_id) + newline + tables + newline

    expected["model_provider"] = provider_id
    expected.setdefault("model_providers", {})[provider_id] = \
        _parse_toml(tables, "the generated tables don't parse")["model_providers"][provider_id]
    if _parse_toml(result, "editing it would not leave valid TOML") != expected:
        raise CodexConfigError("its layout is unusual enough that the edit could not be verified "
                               "(is model_provider set somewhere other than the top of the file?)")
    return result, original.get("model_provider")


def read_codex_config(path):
    """(raw bytes, text) of config.toml; (b"", "") when it doesn't exist yet. A UTF-8 byte order
    mark is accepted (Codex accepts it) and not written back."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return b"", ""
    try:
        return raw, raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise CodexConfigError("it isn't UTF-8 (Windows PowerShell 5.1 writes UTF-16 with `>`/`>>`): "
                               "save it as UTF-8 first")


def _write_atomically(directory, name, data, mode):
    descriptor, temporary_name = tempfile.mkstemp(dir=directory, prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary_name, mode)
        os.replace(temporary_name, directory / name)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def write_codex_config(path, text, original_raw):
    """Write config.toml and return the backup of the previous version (None for a new file).

    A symlinked config.toml (a dotfiles manager) is written through to its target, as Codex does.
    The file is re-read first and nothing is written if it changed since ``original_raw`` was read
    (Codex saving it meanwhile). Every run keeps its own timestamped backup, 0600 from the start,
    so the version from before the first install is never overwritten."""
    target = Path(os.path.realpath(path))
    current = target.read_bytes() if target.exists() else b""
    if current != original_raw:
        raise CodexConfigError("it changed while veritas was editing it (was Codex saving it?): run "
                               "this again")
    if not target.parent.exists():
        target.parent.mkdir(mode=0o700, parents=True)
    backup = None
    mode = 0o600
    if target.exists():
        mode = target.stat().st_mode & 0o777
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.bak-veritas-{stamp}")
        suffix = 1
        while backup.exists():
            backup = path.with_name(f"{path.name}.bak-veritas-{stamp}-{suffix}")
            suffix += 1
        _write_atomically(backup.parent, backup.name, current, 0o600)
    _write_atomically(target.parent, target.name, text.encode("utf-8"), mode)
    return backup


def config_cautions(stack):
    """What the user should know about settings that differ between this shell and a Codex
    started from the IDE or the Dock."""
    scoped = stack.name.upper().replace("-", "_")
    cautions = []
    lost = [name for name in ("VERITAS_HOME", "VERITAS_TOKEN_STORE", f"VERITAS_{scoped}_AUTH_BASE")
            if os.environ.get(name)]
    if lost:
        cautions.append(f"{', '.join(lost)} set in this shell won't reach `veritas token` when Codex "
                        f"is started from the IDE or the Dock.")
    if os.environ.get(f"VERITAS_{scoped}_GATEWAY_BASE"):
        cautions.append(f"VERITAS_{scoped}_GATEWAY_BASE is now written into config.toml: every Codex "
                        f"session sends your token to {stack.gateway_base} until you install again "
                        f"without it.")
    if os.environ.get("CODEX_HOME"):
        cautions.append("CODEX_HOME is set in this shell: a Codex started from the Dock reads "
                        "~/.codex/config.toml unless it is given CODEX_HOME too.")
    return cautions


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
    options first, before any codex option: e.g. `veritas codex -e dev exec "hello"` or
    `veritas codex -e dev -m <model>`."""
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
            install_and_report(stack, client)
            return
        overrides = codex_overrides(stack, client)
    except CodexConfigError as problem:
        fail(str(problem))
    codex_path = find_program("codex")
    if not codex_path:
        fail("codex (OpenAI Codex CLI) is not on PATH: install it first, or use "
             f"`veritas codex -e {stack} --install-config` to configure the IDE extension")
    own_problem = cmd_shim_problem([codex_path, *overrides], delayed_expansion_enabled()) if WINDOWS else None
    if own_problem:
        fail(f"veritas's own setting {own_problem[0]!r} holds {describe_cmd_problem(own_problem[1])}, "
             f"which cmd.exe (running {os.path.basename(codex_path)}) would misread. Run "
             f"`veritas codex -e {stack} --install-config` once, then start `codex` directly")
    launch([codex_path, *overrides, *codex_args], dict(os.environ))


def install_and_report(stack, client):
    path = codex_config_path()
    try:
        original_raw, current = read_codex_config(path)
        text, previous = install_codex_config(current, stack, client)
        backup = write_codex_config(path, text, original_raw)
    except CodexConfigError as problem:
        fail(f"left {path} unchanged: {problem}")
    provider_id = codex_provider_id(stack)
    click.echo(f"Installed {provider_id} ({client}) in {path} and selected it: plain `codex`, the "
               f"IDE extension and the desktop app now run on your {stack} user token (unless a "
               f"trusted project's own .codex/config.toml picks another provider).")
    if previous is None:
        click.echo("Before this, Codex used its default provider (openai): delete the "
                   "model_provider line to go back.")
    elif previous != provider_id:
        click.echo(f"It replaces model_provider = \"{previous}\"; set that back to switch.")
    if backup:
        click.echo(f"Previous version: {backup}")
    for caution in config_cautions(stack):
        click.secho(f"Note: {caution}", fg="yellow", err=True)


def run():
    try:
        main()
    except StoreError as problem:
        fail(str(problem))


if __name__ == "__main__":
    run()
