# veritas CLI

The `veritas` command gets OAuth tokens from the Apigee authorization server (`adex-oauth2-as`)
and uses them to call HUIT AIS proxies. This is workstream 1 of the hackathon; the spec is
[`../01-cli.md`](../01-cli.md).

## Install

```bash
cd veritas-cli
python3 -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/veritas --help
```

It needs Python 3.11 or newer.

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

Every request is charged to your **per-user** Bedrock limit, inside the app's. Claude Code sends a
lot of context with each request: a one-line Haiku prompt cost about $0.05. A per-user limit sized
for curl tests (cents per hour) will stop a session on its first request with a 429. Set
`CLAUDE_CODE_MAX_RETRIES=1` to make that fail fast instead of retrying.

## Stacks

`-e` selects the stack. If you don't pass it, the CLI uses `VERITAS_ENV`, then `default_stack`
from the config file, then `dev`. The stack names are not a fixed list: anything that matches
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
