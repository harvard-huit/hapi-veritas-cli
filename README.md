# veritas CLI

The `veritas` command gets OAuth tokens from the Apigee authorization server (`adex-oauth2-as`)
and uses them to call HUIT AIS proxies, from the command line or from Python.

## Install

It needs **Python 3.11 or newer** and git. The repo is public, so no GitHub login is needed.

**As a command (recommended).** [pipx](https://pipx.pypa.io) puts `veritas` on your PATH in its
own isolated environment:

```bash
pipx install git+https://github.com/harvard-huit/hapi-veritas-cli.git
veritas --version
```

Upgrade with `pipx upgrade veritas-cli`, remove with `pipx uninstall veritas-cli`. No pipx?
`brew install pipx` on macOS, `python3 -m pip install --user pipx` elsewhere, then
`pipx ensurepath` and open a new terminal.

**As a library** for your own scripts or notebooks (`import veritas`), install it into that
project's virtual environment instead:

```bash
python3 -m venv .venv
.venv/bin/pip install git+https://github.com/harvard-huit/hapi-veritas-cli.git
```

Add `@<branch-or-tag>` to the end of the URL to pin a version, e.g.
`...hapi-veritas-cli.git@stage`. Without it you get the default branch, `development`.

**To work on the CLI itself:**

```bash
git clone https://github.com/harvard-huit/hapi-veritas-cli.git && cd hapi-veritas-cli
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/veritas --help
```

## Use it

Signing in as a person uses a native app with a loopback callback. Use the Apigee **app name**;
there is no secret:

```bash
veritas login --app awa-hello_world        # opens the browser for HarvardKey, catches the redirect on :8080
veritas whoami                             # local token record, then /userinfo
veritas token                              # prints a valid access token, refreshing it if it expired
veritas call /ais-openai-direct/v2/models
veritas quota /ais-bedrock-llm            # GET <base path>/apigee/quota (default /ais-openai-direct/v2)
veritas logout
```

Signing in as a machine (m2m) uses the consumerKey, with the secret read from an environment
variable. The secret is never passed on the command line:

```bash
export APIGEE_CLIENT_ID=<consumerKey> APIGEE_CLIENT_SECRET=<consumerSecret>
veritas m2m
veritas whoami          # reports "no user": m2m tokens only ever get the app's quota
```

Other useful commands:

- `veritas env` prints `export ACCESS_TOKEN=...` for use with `eval`.
- `veritas discover` prints the authorization server's metadata. It needs no credentials.
- `veritas call` sends the response body to stdout, and the status line and quota headers to
  stderr, so `veritas call ... | jq` works.

`login` and `m2m` both make the client they used the **active client** for that stack, so the
other commands don't need `--app`.

### An Authorization header for your own code

For skills, functions and workshop notebooks that call HUIT APIs **as the person running them**.
Nobody needs an API subscription or their own Apigee app. You register one user-token app (for a
workshop, the workshop's), add people to its Grouper group, and each person runs
`veritas login --app <that app>` once.

```bash
curl -H "$(veritas header -e dev)" https://dev.apis.huit.harvard.edu/ais-openai-direct/v2/apigee/quota
```

```python
import requests   # pip install requests
import veritas

veritas.headers(app="awa-workshop_x")            # {"Authorization": "Bearer ..."}
veritas.token(app="awa-workshop_x")              # just the token
veritas.gateway()                                # https://prod.apis.huit.harvard.edu

# Fetches the token on each request, refreshing it as needed:
quota_url = veritas.gateway() + "/ais-bedrock-llm/apigee/quota"
requests.get(quota_url, auth=veritas.Auth(app="awa-workshop_x"))

veritas.login(app="awa-workshop_x")              # the browser sign-in, from Python
```

- **`stack=`** works like `-e`: the default is `VERITAS_ENV`, then `default_stack`, then prod.
  **`app=`** works like `--app`: the default is `VERITAS_APP`, then the stack's active client.
- **The CLI and Python share one token store**, so a terminal login is picked up by Python and the
  other way round.
- **Only `login()` opens a browser.** Everything else raises `veritas.VeritasError` with the
  command to run, so a script fails fast instead of hanging on a sign-in page.
- **`import veritas` does not load the CLI**, the layer that prints and exits.

### Claude Code as you

```bash
veritas login -e dev --app awa-veritas_cli     # once; the app needs Bedrock granted in AI access
veritas claude -e dev                          # interactive Claude Code
veritas claude -e dev -p "explain this repo"   # anything after the veritas options goes to claude
veritas claude -e dev --model us.anthropic.claude-sonnet-5-5
```

`veritas claude` runs Claude Code in Bedrock mode against `<gateway>/ais-bedrock-llm/v2`, with no
AWS credentials. Your user token goes as `Authorization: Bearer`, through Claude Code's
`apiKeyHelper`, which re-runs `veritas token` every 5 minutes. So a session outlasts the one-hour
token. It removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_CUSTOM_HEADERS` and
`AWS_BEARER_TOKEN_BEDROCK` from Claude Code's environment, so a key in your profile can't be used
instead.

**Pick the model yourself:** `--model <inference profile id>` at launch, or `/model` inside Claude
Code. veritas sets no model on purpose, so no model ID is hardcoded that AWS could retire.
Left alone, Claude Code uses its own Bedrock default, which is currently Opus, the priciest. The
profiles available on a stack are listed by
`veritas call -e dev "/ais-bedrock-llm/v2/inference-profiles?maxResults=100"`.

Every request is charged to your **per-user** Bedrock limit, inside the app's. Claude Code sends a
lot of context with each request: a one-line Haiku prompt cost about $0.05. A per-user limit sized
for curl tests (cents per hour) will stop a session on its first request with a 429. Set
`CLAUDE_CODE_MAX_RETRIES=1` to make that fail fast instead of retrying.

### Codex as you

```bash
veritas login -e dev --app awa-veritas_cli     # once; the app needs OpenAI granted in AI access
veritas codex -e dev                           # interactive Codex
veritas codex -e dev exec "explain this repo"  # anything after the veritas options goes to codex
veritas codex -e dev -m <model>
```

`veritas codex` runs the Codex CLI against `<gateway>/ais-openai-direct/v2` with a provider named
`veritas-<stack>`, passed as `-c` overrides for that one run. Your `~/.codex/config.toml` is not
changed. The provider has no key. Its `auth.command` is `veritas token`, so Codex gets your user
token from the veritas store. Codex re-runs the command every 5 minutes and after any 401, so a
session outlasts the one-hour token. That command takes priority over an `OPENAI_API_KEY` or
`CODEX_API_KEY` in your environment.

Codex shows one startup warning: `Running without the shared background server: command-line
configuration overrides ... requires embedded mode.` It is harmless. Codex runs its own server for
the session instead of the shared one. The session is still saved, so `codex resume` and
`codex fork` work. What you lose is reaching the session *while it runs* from somewhere else:
`codex agents`, `codex queue`, remote control, or the desktop app. Every way of setting config
on the command line causes the warning, `--profile` included. To use the shared server, put the
provider in `~/.codex/config.toml` with `--print-config` (below), select it there, and start
plain `codex`.

**The IDE extension and the desktop app** start Codex themselves, so veritas can't pass it flags.
Add the provider to `~/.codex/config.toml` once:

```bash
veritas codex -e dev --app awa-veritas_cli --print-config >> ~/.codex/config.toml
```

Then select it with `model_provider = "veritas-dev"` at the top level, or in a profile. The block
contains the path of the Python veritas runs from, so print it again if you reinstall veritas. It
contains no token or secret.

veritas sets no model here either. Codex uses the `model` from your config or `-m`, and the proxy
has to allow it. Every request is charged to your **per-user** OpenAI limit, inside the app's.
Codex retries a failed request up to 5 times by default, so a per-user limit sized for curl tests
will be hit fast.

## Windows

Everything works on Windows. The examples above are bash; in PowerShell:

```powershell
py -3.12 -m pip install --user pipx
py -3.12 -m pipx ensurepath                    # then open a new terminal
pipx install git+https://github.com/harvard-huit/hapi-veritas-cli.git

$env:VERITAS_ENV = "dev"                       # instead of: export VERITAS_ENV=dev
veritas login --app awa-veritas_cli
curl.exe -H (veritas header) https://dev.apis.huit.harvard.edu/ais-bedrock-llm/apigee/quota
veritas claude --model us.anthropic.claude-haiku-4-5-20251001-v1:0
```

- **Use `curl.exe`, not `curl`.** In Windows PowerShell 5, `curl` is an alias for
  `Invoke-WebRequest`, which takes different arguments.
- **Tokens go in Windows Credential Manager**, the counterpart of the macOS Keychain. With
  `VERITAS_TOKEN_STORE=file` they go in `%USERPROFILE%\.config\veritas\tokens.json`; Windows
  doesn't enforce the 0600 mode there, but the profile folder is private to you by default.
- **`veritas claude` runs Claude Code as a child process** on Windows, because Windows has no
  `exec`, and exits with its exit code. Ctrl+C goes to Claude Code. Its settings are written to
  `%USERPROFILE%\.config\veritas\claude\<stack>-<app>.json`, and the token helper's command is
  quoted to read the same in cmd.exe and Git Bash.
- **The tests run on Windows, macOS and Linux in GitHub Actions** (`veritas-cli tests`).

## Stacks

`-e` selects the stack. If you don't pass it, the CLI uses `VERITAS_ENV`, then `default_stack`
from the config file, then `prod`. While developing, pass `-e dev`, or
`export VERITAS_ENV=dev` (or set `"default_stack": "dev"` in the config file) to change the default. The stack names are not a fixed list: anything that matches
`[a-z][a-z0-9-]*` is accepted.

For each stack, the CLI resolves two base URLs. The first source that has a value wins:

| Source | Example |
|---|---|
| An environment variable scoped to the stack | `VERITAS_STAGE_AUTH_BASE`, `VERITAS_STAGE_GATEWAY_BASE` |
| `config.json` in `$VERITAS_HOME` | `{"stacks": {"lab": {"auth_base": "https://…/oauth2/v1", "gateway_base": "https://…"}}}` |
| The HUIT naming convention | auth `https://go.<stack>.apis.huit.harvard.edu/oauth2/v1` (prod has no stack segment); gateway `https://<stack>.apis.huit.harvard.edu` |

Notes:

- **Overrides are scoped to one stack on purpose.** An unscoped override that was set for one
  stack and then forgotten would send tokens for every other stack to the wrong host.
- **Endpoint paths come from discovery.** The CLI never builds the authorize, token or userinfo
  URLs itself; it reads them from the authorization server's RFC 8414 metadata. So if an endpoint
  moves, the CLI needs no new release.
- **The issuer is checked.** The `issuer` in the metadata must equal the configured auth base, or
  the CLI refuses to send credentials there.

## Where tokens are kept

Tokens are stored in the OS keychain through `keyring`, under the service `veritas` and the key
`<stack>:<client>`. If no keychain is available, they go to `$VERITAS_HOME/tokens.json` with
mode 0600. `$VERITAS_HOME` defaults to `~/.config/veritas`. Set `VERITAS_TOKEN_STORE=file` or
`VERITAS_TOKEN_STORE=keyring` to force one of the two. Tokens are never written to the current
directory.

## Things that trip people up

- **The redirect port must match the app's callback exactly.** Apigee compares the callback as
  a string, port included. If port 8080 is busy, `login` stops with an error instead of switching
  ports, because any other port would fail with `redirect_uri_mismatch`.
- **`access_denied` at login** usually means you aren't in a Grouper group that authorizes the
  app.
- **Refresh tokens last 30 days.** Apigee also doesn't invalidate the previous refresh token when
  it issues a new one. m2m tokens have no refresh token at all, so when one expires, run
  `veritas m2m` again.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests cover PKCE, the token store, stack resolution, discovery, the issuer check, the
loopback receiver, and the CLI commands. They use a mock HTTP transport and never touch the
real keychain.
