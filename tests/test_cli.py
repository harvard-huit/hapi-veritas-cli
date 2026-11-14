import pathlib
import json
import os
import shlex
import subprocess
import sys
import time

import httpx
import pytest
from click.testing import CliRunner

from veritas import cli
from veritas import client as core
from veritas.oauth import AuthServer
from veritas.stacks import resolve
from veritas.store import TokenStore

ISSUER = "https://go.dev.apis.huit.harvard.edu/oauth2/v1"


@pytest.fixture
def token_endpoint(monkeypatch):
    """Route every AuthServer the CLI builds through a mock transport; record the requests."""
    calls = []
    responses = {"/oauth2/v1/.well-known/oauth-authorization-server": httpx.Response(200, json={
        "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token", "userinfo_endpoint": f"{ISSUER}/userinfo"})}

    def handler(request):
        if not request.url.path.endswith("/.well-known/oauth-authorization-server"):
            calls.append(request)
        return responses.get(request.url.path, httpx.Response(404))

    monkeypatch.setattr(core, "auth_server", lambda stack: AuthServer(
        stack, http_client=httpx.Client(transport=httpx.MockTransport(handler))))
    return calls, responses


def stored(grant="authorization_code", expires_in=3600, refresh_in=86400):
    now = int(time.time())
    return {"client": "awa-cli", "grant": grant, "access_token": "old-access",
            "refresh_token": "old-refresh" if grant == "authorization_code" else None,
            "obtained_at": now, "expires_at": now + expires_in,
            "refresh_expires_at": now + refresh_in if refresh_in else None,
            "application_name": None}


def test_token_prints_without_refresh_when_valid(token_endpoint):
    calls, _ = token_endpoint
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    store.set_active_client("dev", "awa-cli")
    result = CliRunner().invoke(cli.main, ["token", "-e", "dev"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "old-access"
    assert calls == []


def test_token_refreshes_when_expired(token_endpoint):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": "3599",
        "refresh_token_expires_in": "2591999"})
    store = TokenStore()
    store.put("dev", "awa-cli", stored(expires_in=30))  # inside the 60s skew
    store.set_active_client("dev", "awa-cli")

    result = CliRunner().invoke(cli.main, ["token", "-e", "dev"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "new-access"
    assert b"grant_type=refresh_token" in calls[0].content
    assert store.get("dev", "awa-cli")["refresh_token"] == "new-refresh"


def test_expired_m2m_token_asks_for_m2m_not_login(token_endpoint):
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", expires_in=-10,
                                            refresh_in=None))
    result = CliRunner().invoke(cli.main, ["token", "-e", "dev", "--app", "consumer-key"])
    assert result.exit_code == 1
    assert "veritas m2m" in result.output


def test_no_client_explains_how_to_log_in():
    result = CliRunner().invoke(cli.main, ["token", "-e", "stage"])
    assert result.exit_code == 1
    assert "veritas login -e stage --app" in result.output


def test_m2m_reads_secret_from_env_and_stores(token_endpoint, monkeypatch):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "m2m-access", "expires_in": "3599", "application_name": "svc-app"})
    monkeypatch.setenv("SVC_SECRET", "s3cret")
    result = CliRunner().invoke(cli.main, ["m2m", "-e", "dev", "--client-id", "consumer-key",
                                           "--secret-env", "SVC_SECRET"])
    assert result.exit_code == 0, result.output
    assert "s3cret" not in result.output and "m2m-access" not in result.output
    store = TokenStore()
    assert store.active_client("dev") == "consumer-key"
    assert store.get("dev", "consumer-key")["access_token"] == "m2m-access"


def test_m2m_without_secret_fails(monkeypatch):
    monkeypatch.delenv("APIGEE_CLIENT_SECRET", raising=False)
    result = CliRunner().invoke(cli.main, ["m2m", "--client-id", "consumer-key"])
    assert result.exit_code == 1
    assert "APIGEE_CLIENT_SECRET" in result.output


def test_whoami_for_m2m_reports_no_user(token_endpoint):
    calls, _ = token_endpoint
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", refresh_in=None))
    result = CliRunner().invoke(cli.main, ["whoami", "-e", "dev", "--app", "consumer-key"])
    assert result.exit_code == 0, result.output
    assert "carries no user" in result.output
    assert calls == []


def test_logout_clears_token_and_active(token_endpoint):
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    store.set_active_client("dev", "awa-cli")
    result = CliRunner().invoke(cli.main, ["logout", "-e", "dev"])
    assert result.exit_code == 0, result.output
    assert store.get("dev", "awa-cli") is None
    assert store.active_client("dev") is None


def test_call_target_resolution():
    dev = resolve("dev")
    assert cli._target_url(dev, "/ais-bedrock-llm/apigee/quota") == \
        "https://dev.apis.huit.harvard.edu/ais-bedrock-llm/apigee/quota"
    assert cli._target_url(dev, "https://dev.apis.huit.harvard.edu/x") == \
        "https://dev.apis.huit.harvard.edu/x"
    assert cli._target_url(dev, "https://example.org/x", any_host=True) == "https://example.org/x"


@pytest.mark.parametrize("target", ["https://example.org/x",
                                    "http://dev.apis.huit.harvard.edu/x",
                                    "https://stage.apis.huit.harvard.edu/x"])
def test_call_refuses_to_send_the_token_off_gateway(target):
    with pytest.raises(SystemExit):
        cli._target_url(resolve("dev"), target)


def test_unlisted_stack_from_config_is_accepted(isolated_home):
    isolated_home.mkdir(parents=True, exist_ok=True)
    (isolated_home / "config.json").write_text(
        '{"default_stack": "lab", "stacks": {"lab": {"auth_base": "https://auth.example.edu/v1"}}}')
    store = TokenStore()
    store.put("lab", "awa-cli", stored())
    store.set_active_client("lab", "awa-cli")
    result = CliRunner().invoke(cli.main, ["token"])  # default_stack from config
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "old-access"


def test_invalid_stack_name_is_a_usage_error():
    result = CliRunner().invoke(cli.main, ["token", "-e", "Not A Stack"])
    assert result.exit_code == 2
    assert "not a valid name" in result.output


def test_token_refresh_flag_forces_a_refresh(token_endpoint):
    calls, responses = token_endpoint
    responses["/oauth2/v1/token"] = httpx.Response(200, json={
        "access_token": "forced-access", "refresh_token": "forced-refresh", "expires_in": "3599",
        "refresh_token_expires_in": "2591999"})
    store = TokenStore()
    store.put("dev", "awa-cli", stored())          # still valid for an hour
    store.set_active_client("dev", "awa-cli")
    result = CliRunner().invoke(cli.main, ["token", "-e", "dev", "--refresh"])
    assert result.exit_code == 0, result.output
    assert "forced-access" in result.output
    assert b"grant_type=refresh_token" in calls[0].content


def test_claude_runs_claude_code_on_the_gateway_with_a_refreshing_token(monkeypatch, token_endpoint):
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    store.set_active_client("dev", "awa-cli")
    launched = {}
    monkeypatch.setattr(cli, "find_program", lambda name: "/usr/local/bin/claude")
    monkeypatch.setattr(cli, "launch", lambda arguments, environment: launched.update(
        arguments=arguments, environment=environment))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key-from-my-profile")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "another-key")

    result = CliRunner().invoke(cli.main, ["claude", "-e", "dev", "-p", "hello", "--model", "m"])
    assert result.exit_code == 0, result.output

    claude_path, settings_flag, settings_path, *passed_through = launched["arguments"]
    assert claude_path == "/usr/local/bin/claude"
    assert settings_flag == "--settings"
    assert passed_through == ["-p", "hello", "--model", "m"]
    # Settings travel as a FILE (cmd.exe mangles an inline JSON argument on Windows).
    helper = json.loads(pathlib.Path(settings_path).read_text())["apiKeyHelper"]
    assert "-P -m veritas.cli token -e dev --app awa-cli" in helper.replace('"', "").replace("'", "")

    environment = launched["environment"]
    assert environment["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert environment["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    assert environment["ANTHROPIC_BEDROCK_BASE_URL"] == "https://dev.apis.huit.harvard.edu/ais-bedrock-llm/v2"
    assert "ANTHROPIC_API_KEY" not in environment
    assert "AWS_BEARER_TOKEN_BEDROCK" not in environment


def test_claude_refuses_an_m2m_token(monkeypatch, token_endpoint):
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", refresh_in=None))
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["claude", "-e", "dev", "--app", "consumer-key"])
    assert result.exit_code == 1
    assert "names no user" in result.output


def test_claude_without_a_login_says_how_to_sign_in(monkeypatch):
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["claude", "-e", "stage"])
    assert result.exit_code == 1
    assert "veritas login" in result.output


def toml_literal(value):
    import tomllib
    return tomllib.loads(f"probe = {value}")["probe"]


def codex_flags(arguments):
    """{key: raw TOML value} from a list of `-c key=value` flags, and the arguments left over."""
    settings, rest, index = {}, [], 0
    while index < len(arguments):
        if arguments[index] == "-c":
            key, _, value = arguments[index + 1].partition("=")
            settings[key] = value
            index += 2
        else:
            rest.append(arguments[index])
            index += 1
    return settings, rest


def test_codex_runs_codex_on_the_gateway_with_a_refreshing_token(monkeypatch, token_endpoint):
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    store.set_active_client("dev", "awa-cli")
    launched = {}
    monkeypatch.setattr(cli, "find_program", lambda name: "/usr/local/bin/codex")
    monkeypatch.setattr(cli, "launch", lambda arguments, environment: launched.update(
        arguments=arguments, environment=environment))

    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "exec", "hello", "-m", "m"])
    assert result.exit_code == 0, result.output

    codex_path, *arguments = launched["arguments"]
    assert codex_path == "/usr/local/bin/codex"
    settings, passed_through = codex_flags(arguments)
    assert passed_through == ["exec", "hello", "-m", "m"]
    provider = "model_providers.veritas-dev"
    assert toml_literal(settings["model_provider"]) == "veritas-dev"
    assert toml_literal(settings[f"{provider}.base_url"]) == \
        "https://dev.apis.huit.harvard.edu/ais-openai-direct/v2"
    assert toml_literal(settings[f"{provider}.auth.args"]) == \
        ["-P", "-m", "veritas.cli", "token", "-e", "dev", "--app", "awa-cli"]
    assert toml_literal(settings[f"{provider}.auth.refresh_interval_ms"]) < 3600 * 1000
    # No double quotes on the command line: cmd.exe misreads them when codex is npm's codex.cmd.
    assert not any('"' in argument for argument in arguments)
    # No model is chosen for the user, and no token or key is on the command line.
    assert not any(key == "model" for key in settings)
    assert "old-access" not in " ".join(launched["arguments"])


def test_codex_print_config_is_valid_toml_and_launches_nothing(monkeypatch, token_endpoint):
    tomllib = pytest.importorskip("tomllib")
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--app", "awa-cli", "--print-config"])
    assert result.exit_code == 0, result.output
    provider = tomllib.loads(result.output)["model_providers"]["veritas-dev"]
    assert provider["base_url"] == "https://dev.apis.huit.harvard.edu/ais-openai-direct/v2"
    assert provider["auth"]["args"][-2:] == ["--app", "awa-cli"]
    assert "old-access" not in result.output


@pytest.mark.parametrize("executable", ["/home/me/venv/bin/python",
                                        "/home/m\u00e9/\U0001d11e venv/bin/python",
                                        "C:\\Users\\me\\it's\\python.exe"])
@pytest.mark.parametrize("client", ["awa-cli", 'awa-"odd" app', "awa-it's"])
def test_codex_overrides_are_toml_that_round_trips(monkeypatch, executable, client):
    monkeypatch.setattr(cli.sys, "executable", executable)
    settings, _ = codex_flags(cli.codex_overrides(resolve("dev"), client))
    provider = "model_providers.veritas-dev"
    assert toml_literal(settings[f"{provider}.auth.command"]) == executable
    assert toml_literal(settings[f"{provider}.auth.args"])[-1] == client
    for value in settings.values():
        toml_literal(value)   # each value is a TOML literal on its own


def test_toml_value_handles_bools_and_refuses_undecodable_paths():
    assert cli.toml_value(True) == "true" and cli.toml_value(False) == "false"
    with pytest.raises(cli.CodexConfigError):
        cli.toml_value("/home/\udcff/python")


def test_codex_refuses_an_m2m_token(monkeypatch, token_endpoint):
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", refresh_in=None))
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--app", "consumer-key"])
    assert result.exit_code == 1
    assert "names no user" in result.output


def test_codex_not_installed_points_at_print_config(monkeypatch, token_endpoint):
    store = TokenStore()
    store.put("dev", "awa-cli", stored())
    monkeypatch.setattr(cli, "find_program", lambda name: None)
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--app", "awa-cli"])
    assert result.exit_code == 1
    assert "--install-config" in result.output


@pytest.fixture
def project_shadowing_veritas(tmp_path):
    """A project directory holding its own `veritas` package, as a cloned repo (or a coding agent
    writing to the workspace) could. Imported from here, it prints a fake token and exits 0."""
    package = tmp_path / "veritas"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text("print('SHADOWED-TOKEN')\n")
    return tmp_path


def run_from(directory, command):
    """Run a token-helper command as Claude Code or Codex would, from the user's project, with
    `--version` in place of `token ...` so nothing needs a login."""
    module_end = command.index("veritas.cli") + 1
    return subprocess.run([*command[:module_end], "--version"], cwd=directory,
                          capture_output=True, text=True, timeout=60)


def test_codex_auth_command_ignores_a_veritas_package_in_the_project(project_shadowing_veritas):
    executable, arguments = cli.codex_token_command(resolve("dev"), "awa-cli")
    result = run_from(project_shadowing_veritas, [executable, *arguments])
    assert "SHADOWED" not in result.stdout
    assert result.returncode == 0 and "veritas, version" in result.stdout


def test_claude_helper_ignores_a_veritas_package_in_the_project(project_shadowing_veritas):
    command = shlex.split(cli.helper_command(resolve("dev"), "awa-cli", windows=False))
    result = run_from(project_shadowing_veritas, command)
    assert "SHADOWED" not in result.stdout
    assert result.returncode == 0 and "veritas, version" in result.stdout


def signed_in(name="awa-cli"):
    """Store a fresh user token for ``name`` on dev and make it the active client."""
    store = TokenStore()
    store.put("dev", name, stored())
    store.set_active_client("dev", name)


@pytest.mark.parametrize("command", ["codex", "claude"])
def test_tool_arguments_after_the_first_one_are_not_veritas_options(monkeypatch, token_endpoint, command):
    signed_in()
    launched = {}
    monkeypatch.setattr(cli, "find_program", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(cli, "launch", lambda arguments, environment: launched.update(arguments=arguments))
    passthrough = ["exec", "-e", "stage", "--app", "x", "--help", "--print-config", "--", "-m is a prompt"]
    result = CliRunner().invoke(cli.main, [command, "-e", "dev", *passthrough])
    assert result.exit_code == 0, result.output
    assert launched["arguments"][-len(passthrough):] == passthrough
    assert not any("stage" in argument for argument in launched["arguments"][:-len(passthrough)])


def test_an_expired_m2m_client_is_refused_as_m2m_not_sent_to_renew_it(monkeypatch, token_endpoint):
    store = TokenStore()
    store.put("dev", "consumer-key", stored(grant="client_credentials", expires_in=-60, refresh_in=None))
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    for command in ("codex", "claude"):
        result = CliRunner().invoke(cli.main, [command, "-e", "dev", "--app", "consumer-key"])
        assert result.exit_code == 1
        assert "names no user" in result.output and "veritas m2m" not in result.output


def test_find_program_on_windows_skips_the_current_directory(tmp_path, monkeypatch):
    project, tools = tmp_path / "project", tmp_path / "tools"
    project.mkdir()
    tools.mkdir()
    (project / "codex.cmd").write_text("@echo planted")
    (tools / "codex.cmd").write_text("@echo real")
    monkeypatch.chdir(project)
    if os.name == "nt":   # a real drive-letter path exists only on Windows
        environment = {"PATH": f".;{tools}", "PATHEXT": ".exe;.cmd"}
        assert cli.find_program("codex", windows=True, environment=environment) == str(tools / "codex.cmd")
    assert cli.find_program("codex", windows=True, environment={"PATH": ".", "PATHEXT": ".cmd"}) is None


@pytest.mark.parametrize("argument", ['say "hi"', "50%", "a&b", "x|y", "a>b", "c^d"])
def test_windows_launch_refuses_what_cmd_exe_would_misread(monkeypatch, argument):
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *arguments, **options: pytest.fail("must not run"))
    with pytest.raises(SystemExit) as exited:
        cli.launch([r"C:\npm\codex.cmd", "exec", argument], {}, windows=True)
    assert exited.value.code == 1


class FakeRegistry:
    """Stands in for winreg: {hive: {value name: value}} for the Command Processor key."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"

    def __init__(self, hives):
        self.hives = hives

    def OpenKey(self, hive, key):
        assert key == cli.DELAYED_EXPANSION_KEY
        if hive not in self.hives:
            raise FileNotFoundError(key)
        values = self.hives[hive]

        class Key:
            def __enter__(self):
                return values

            def __exit__(self, *exception):
                return False
        return Key()

    def QueryValueEx(self, values, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 4


@pytest.mark.parametrize("hives, enabled", [
    ({}, False),
    ({"HKLM": {}}, False),
    ({"HKLM": {"DelayedExpansion": 1}}, True),
    ({"HKCU": {"DelayedExpansion": 0}, "HKLM": {"DelayedExpansion": 1}}, False),   # user wins
    ({"HKCU": {"DelayedExpansion": 1}, "HKLM": {"DelayedExpansion": 0}}, True),
    ({"HKCU": {}, "HKLM": {"DelayedExpansion": 1}}, True),   # no user value: machine decides
])
def test_delayed_expansion_follows_the_registry_with_the_user_setting_first(monkeypatch, hives, enabled):
    monkeypatch.setitem(sys.modules, "winreg", FakeRegistry(hives))
    assert cli.delayed_expansion_enabled(windows=True) is enabled
    assert cli.delayed_expansion_enabled(windows=False) is False


def test_exclamation_marks_pass_unless_delayed_expansion_is_on(monkeypatch):
    started = []

    class Finished:
        def wait(self):
            return 0
    monkeypatch.setattr(cli.subprocess, "Popen", lambda arguments, env: started.append(arguments) or Finished())
    monkeypatch.setitem(sys.modules, "winreg", FakeRegistry({}))
    with pytest.raises(SystemExit):
        cli.launch([r"C:\npm\codex.cmd", "exec", "Fix this!"], {}, windows=True)
    assert started and started[0][-1] == "Fix this!"

    started.clear()
    monkeypatch.setitem(sys.modules, "winreg", FakeRegistry({"HKCU": {"DelayedExpansion": 1}}))
    with pytest.raises(SystemExit) as exited:
        cli.launch([r"C:\npm\codex.cmd", "exec", "show !OPENAI_API_KEY!"], {}, windows=True)
    assert exited.value.code == 1 and not started


def test_delayed_expansion_message_names_the_setting(capsys):
    assert "DelayedExpansion=1" in cli.describe_cmd_problem(["!"])
    assert "DelayedExpansion" not in cli.describe_cmd_problem(["&"])


def test_windows_launch_of_an_exe_passes_any_argument(monkeypatch):
    started = {}

    class Finished:
        def wait(self):
            return 0
    monkeypatch.setattr(cli.subprocess, "Popen", lambda arguments, env: started.update(arguments=arguments) or Finished())
    with pytest.raises(SystemExit):
        cli.launch([r"C:\tools\codex.exe", "exec", 'say "hi" & 50%'], {}, windows=True)
    assert started["arguments"][-1] == 'say "hi" & 50%'


MARKS_CONFIG = """model = "gpt-5.5"
model_provider = "apigee-prod"
model_reasoning_effort = "low"

[model_providers.apigee-prod]
name = "Apigee Dev"
base_url = "https://dev.apis.huit.harvard.edu/ais-openai-direct/v2"
env_key = "CODEX_API_KEY_DEV"

[projects."/Users/me/repo"]
trust_level = "trusted"
"""


def test_install_config_selects_the_provider_at_top_level_and_keeps_everything_else():
    import tomllib
    text, previous = cli.install_codex_config(MARKS_CONFIG, resolve("dev"), "awa-cli")
    parsed = tomllib.loads(text)
    assert previous == "apigee-prod"
    assert parsed["model_provider"] == "veritas-dev"
    assert parsed["model_providers"]["veritas-dev"]["auth"]["args"][-2:] == ["--app", "awa-cli"]
    assert parsed["model_providers"]["apigee-prod"]["env_key"] == "CODEX_API_KEY_DEV"
    assert parsed["projects"]["/Users/me/repo"]["trust_level"] == "trusted"
    assert parsed["model"] == "gpt-5.5"


def test_install_config_again_replaces_its_block_instead_of_duplicating_it():
    import tomllib
    once, _ = cli.install_codex_config(MARKS_CONFIG, resolve("dev"), "awa-one")
    twice, previous = cli.install_codex_config(once, resolve("dev"), "awa-two")
    assert previous == "veritas-dev"
    assert twice.count("[model_providers.veritas-dev]") == 1
    assert tomllib.loads(twice)["model_providers"]["veritas-dev"]["auth"]["args"][-1] == "awa-two"
    assert cli.install_codex_config(twice, resolve("dev"), "awa-two")[0] == twice   # stable


def test_install_config_into_an_empty_or_missing_file():
    import tomllib
    text, previous = cli.install_codex_config("", resolve("dev"), "awa-cli")
    assert previous is None and tomllib.loads(text)["model_provider"] == "veritas-dev"


@pytest.mark.parametrize("config, message", [
    ("model = [\n", "can't read it"),
    ("[model_providers]\nveritas-dev = { name = 'inline' }\n", "an inline, quoted or dotted form"),
])
def test_install_config_refuses_and_changes_nothing(config, message):
    with pytest.raises(cli.CodexConfigError, match=message):
        cli.install_codex_config(config, resolve("dev"), "awa-cli")


def test_install_config_replaces_tables_pasted_from_print_config():
    import tomllib
    pasted = MARKS_CONFIG + "\n[model_providers.veritas-dev]\nname = 'pasted by hand'\n" \
                            "[model_providers.veritas-dev.auth]\ncommand = 'old'\n"
    text, _ = cli.install_codex_config(pasted, resolve("dev"), "awa-cli")
    assert text.count("[model_providers.veritas-dev]") == 1
    assert tomllib.loads(text)["model_providers"]["veritas-dev"]["auth"]["command"] != "old"


CODEX_ADDED = """
[mcp_servers.docs]
command = "npx"
args = ["-y", "docs-mcp"]

[projects."/Users/me/other"]
trust_level = "trusted"
"""


def test_reinstalling_keeps_tables_codex_added_after_the_veritas_ones():
    import tomllib
    once, _ = cli.install_codex_config(MARKS_CONFIG, resolve("dev"), "awa-cli")
    twice, _ = cli.install_codex_config(once + CODEX_ADDED, resolve("dev"), "awa-two")
    parsed = tomllib.loads(twice)
    assert parsed["mcp_servers"]["docs"]["args"] == ["-y", "docs-mcp"]
    assert parsed["projects"]["/Users/me/other"]["trust_level"] == "trusted"
    assert parsed["model_providers"]["veritas-dev"]["auth"]["args"][-1] == "awa-two"


def test_upgrading_the_first_03_block_keeps_what_codex_put_inside_its_markers():
    """The first 0.3.0 build wrapped its tables in start/end markers, and Codex inserted new tables
    before the end marker; reinstalling then deleted them. That layout must upgrade cleanly."""
    import tomllib
    legacy = MARKS_CONFIG.replace('model_provider = "apigee-prod"', "model_provider = 'veritas-dev'") + (
        "\n# >>> veritas-dev: managed by `veritas codex --install-config`; edits here are replaced >>>\n"
        "[model_providers.veritas-dev]\nname = 'old'\n\n[model_providers.veritas-dev.auth]\n"
        "command = 'old'\n" + CODEX_ADDED + "# <<< veritas-dev <<<\n")
    text, _ = cli.install_codex_config(legacy, resolve("dev"), "awa-cli")
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["docs"]["command"] == "npx"
    assert parsed["projects"]["/Users/me/other"]["trust_level"] == "trusted"
    assert "<<<" not in text and ">>>" not in text
    assert text.count("[model_providers.veritas-dev]") == 1


def test_installing_a_second_stack_leaves_the_first_alone():
    import tomllib
    dev, _ = cli.install_codex_config(MARKS_CONFIG, resolve("dev"), "awa-cli")
    both, previous = cli.install_codex_config(dev, resolve("stage"), "awa-cli")
    parsed = tomllib.loads(both)
    assert previous == "veritas-dev" and parsed["model_provider"] == "veritas-stage"
    assert set(parsed["model_providers"]) == {"apigee-prod", "veritas-dev", "veritas-stage"}


def test_install_config_keeps_comments_and_crlf_elsewhere():
    crlf = "# my settings\r\nmodel = 'x'\r\n\r\n# my server\r\n[mcp_servers.a]\r\ncommand = 'a'\r\n"
    text, previous = cli.install_codex_config(crlf, resolve("dev"), "awa-cli")
    assert previous is None
    assert "# my settings" in text and "# my server\r\n[mcp_servers.a]" in text
    assert "\n" not in text.replace("\r\n", "")


def test_install_config_command_writes_codex_home_and_keeps_a_backup(monkeypatch, token_endpoint, tmp_path):
    import tomllib
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(MARKS_CONFIG)
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 0, result.output
    assert "apigee-prod" in result.output
    assert tomllib.loads((tmp_path / "config.toml").read_text())["model_provider"] == "veritas-dev"
    backups = list(tmp_path.glob("config.toml.bak-veritas-*"))
    assert len(backups) == 1 and backups[0].read_text() == MARKS_CONFIG
    assert cli.WINDOWS or backups[0].stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob("*.tmp"))
    # A second install keeps the first backup (the original) and adds its own.
    again = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--app", "awa-cli", "--install-config"])
    assert again.exit_code == 0, again.output
    assert MARKS_CONFIG in [backup.read_text() for backup in tmp_path.glob("config.toml.bak-veritas-*")]


def test_install_config_leaves_a_broken_file_alone(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text("model = [\n")
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 1 and "unchanged" in result.output
    assert (tmp_path / "config.toml").read_text() == "model = [\n"
    assert not list(tmp_path.glob("config.toml.bak-veritas*"))


def test_install_config_writes_through_a_symlink(monkeypatch, token_endpoint, tmp_path):
    if cli.WINDOWS:
        pytest.skip("symlinks need privileges on Windows")
    signed_in()
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "codex.toml").write_text(MARKS_CONFIG)
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").symlink_to(dotfiles / "codex.toml")
    monkeypatch.setenv("CODEX_HOME", str(home))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 0, result.output
    assert (home / "config.toml").is_symlink()
    assert "veritas-dev" in (dotfiles / "codex.toml").read_text()


def test_install_config_accepts_a_bom_and_refuses_utf16_cleanly(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_bytes(b"\xef\xbb\xbf" + MARKS_CONFIG.encode())
    assert CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"]).exit_code == 0
    written = config.read_bytes()
    assert not written.startswith(b"\xef\xbb\xbf") and b"apigee-prod" in written
    config.write_bytes(MARKS_CONFIG.encode("utf-16"))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 1 and "isn't UTF-8" in result.output
    assert config.read_bytes() == MARKS_CONFIG.encode("utf-16")


def test_write_codex_config_refuses_when_the_file_changed_meanwhile(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("model = 'mine'\n")
    with pytest.raises(cli.CodexConfigError, match="changed while"):
        cli.write_codex_config(config, "model = 'veritas'\n", b"model = 'older'\n")
    assert config.read_text() == "model = 'mine'\n"


def test_install_config_says_when_codex_was_on_its_default(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "new-home"))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 0, result.output
    assert "default provider (openai)" in result.output
    assert "CODEX_HOME is set" in result.output
    monkeypatch.setenv("CODEX_HOME", str(pathlib.Path.home() / ".codex"))
    assert not any("CODEX_HOME" in caution for caution in cli.config_cautions(resolve("dev")))


THREE_QUOTES = "'" * 3


@pytest.mark.parametrize("text", ["O'Brien", "it'", "a''b", "tab\there", "del\x7fchar",
                                  "x" + THREE_QUOTES + "y", "\U0001F600 path"])
def test_toml_value_round_trips_awkward_strings(text):
    assert toml_literal(cli.toml_value(text)) == text


def test_codex_with_an_apostrophe_path_needs_no_double_quotes(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", r"C:\Users\O'Brien\venv\Scripts\python.exe")
    overrides = cli.codex_overrides(resolve("dev"), "awa-cli")
    assert cli.cmd_shim_problem([r"C:\npm\codex.cmd", *overrides]) is None


def test_codex_blames_its_own_setting_not_the_prompt(monkeypatch, token_endpoint):
    signed_in()
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli.sys, "executable", r"C:\Users\AT&T\venv\Scripts\python.exe")
    monkeypatch.setattr(cli, "find_program", lambda name: r"C:\npm\codex.cmd")
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "exec", "hi"])
    assert result.exit_code == 1 and "veritas's own setting" in result.output
    assert "--install-config" in result.output


def test_find_program_on_windows_skips_relative_path_entries(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "bin").mkdir(parents=True)
    (project / "bin" / "codex.cmd").write_text("@echo planted")
    monkeypatch.chdir(project)
    environment = {"PATH": "bin;.\\bin", "PATHEXT": ".cmd"}
    assert cli.find_program("codex", windows=True, environment=environment) is None


@pytest.mark.parametrize("app", ["awa-$(id)", "awa`id`", 'awa"x', "awa%PATH%"])
def test_claude_on_windows_refuses_app_names_its_helper_shell_would_expand(monkeypatch, token_endpoint, app):
    signed_in(app)
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["claude", "-e", "dev", "--app", app])
    assert result.exit_code == 1 and "would expand" in result.output


@pytest.mark.parametrize("app", ["_tool", "team%x", " spaced", "awa-$(id)"])
def test_app_names_apigee_allows_work_where_no_shell_sees_them(monkeypatch, token_endpoint, app):
    signed_in(app)
    monkeypatch.setattr(cli, "WINDOWS", False)   # on Windows, veritas claude refuses some (test above)
    launched = []
    monkeypatch.setattr(cli, "find_program", lambda name: f"/usr/local/bin/{name}")
    monkeypatch.setattr(cli, "launch", lambda arguments, environment: launched.append(arguments))
    for command in ("codex", "claude"):
        result = CliRunner().invoke(cli.main, [command, "-e", "dev", "--app", app])
        assert result.exit_code == 0, result.output
    assert len(launched) == 2


def test_claude_settings_files_for_similar_names_dont_collide(isolated_home):
    first = cli.claude_settings_file(resolve("dev"), "my app")
    second = cli.claude_settings_file(resolve("dev"), "my_app")
    assert first != second


def test_switching_stacks_back_and_forth_doesnt_grow_the_file():
    text = MARKS_CONFIG
    for stack in ("dev", "stage") * 4:
        text, _ = cli.install_codex_config(text, resolve(stack), "awa-cli")
    settled = text
    for stack in ("dev", "stage"):
        text, _ = cli.install_codex_config(text, resolve(stack), "awa-cli")
    assert text == settled
    assert "\n\n\n" not in text


def test_an_empty_model_providers_header_is_not_a_reason_to_refuse():
    import tomllib
    text, _ = cli.install_codex_config("model = 'x'\n\n[model_providers]\n", resolve("dev"), "awa-cli")
    assert tomllib.loads(text)["model_providers"]["veritas-dev"]["wire_api"] == "responses"


def test_legacy_markers_of_another_stack_are_cleaned_up_too():
    legacy = ("model_provider = 'veritas-stage'\n\n"
              "# >>> veritas-stage: managed by `veritas codex --install-config`; edits here are replaced >>>\n"
              "[model_providers.veritas-stage]\nname = 'old'\n\n[model_providers.veritas-stage.auth]\n"
              "command = 'old'\n# <<< veritas-stage <<<\n")
    text, _ = cli.install_codex_config(legacy, resolve("dev"), "awa-cli")
    assert ">>>" not in text and "<<<" not in text
    assert "[model_providers.veritas-stage]" in text


def test_reinstalling_unchanged_writes_nothing(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(MARKS_CONFIG)
    assert CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"]).exit_code == 0
    first = (tmp_path / "config.toml").read_bytes()
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 0 and "already installed" in result.output
    assert (tmp_path / "config.toml").read_bytes() == first
    assert len(list(tmp_path.glob("config.toml.bak-veritas-*"))) == 1


def test_install_config_reports_os_errors_without_a_traceback(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").mkdir()   # a directory where the file should be
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 1 and "could not update" in result.output
    assert isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("entry", ["bin", ".\\bin", "C:bin", "\\bin"])
def test_find_program_on_windows_skips_every_non_absolute_form(tmp_path, monkeypatch, entry):
    monkeypatch.chdir(tmp_path)
    assert cli.find_program("codex", windows=True, environment={"PATH": entry, "PATHEXT": ".cmd"}) is None


@pytest.mark.parametrize("entry, searched", [
    ("C:\\Program Files\\nodejs", True), ("d:/tools", True), ("\\\\server\\share\\bin", True),
    ("bin", False), (".\\bin", False), ("C:bin", False), ("\\bin", False), ("%APPDATA%\\npm", False),
])
def test_which_windows_path_entries_are_searched(entry, searched):
    assert bool(cli.WINDOWS_ABSOLUTE_PATH.match(entry)) is searched


def test_claude_on_windows_blames_its_own_settings_path(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setattr(cli, "WINDOWS", True)
    monkeypatch.setenv("VERITAS_HOME", str(tmp_path / "AT&T"))
    signed_in()
    monkeypatch.setattr(cli, "find_program", lambda name: r"C:\npm\claude.cmd")
    monkeypatch.setattr(cli, "launch", lambda *arguments: pytest.fail("must not launch"))
    result = CliRunner().invoke(cli.main, ["claude", "-e", "dev"])
    assert result.exit_code == 1 and "own settings path" in result.output and "VERITAS_HOME" in result.output


def test_install_config_warns_about_settings_only_this_shell_has(monkeypatch, token_endpoint, tmp_path):
    signed_in()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("VERITAS_DEV_AUTH_BASE", "https://go.example.edu/oauth2/v1")
    monkeypatch.setenv("VERITAS_DEV_GATEWAY_BASE", "https://gateway.example.edu")
    result = CliRunner().invoke(cli.main, ["codex", "-e", "dev", "--install-config"])
    assert result.exit_code == 0, result.output
    assert "VERITAS_DEV_AUTH_BASE set in this shell won't reach" in result.output
    assert "GATEWAY_BASE is now written into config.toml" in result.output


def test_helper_command_on_windows_reads_the_same_in_cmd_and_git_bash(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", r"C:\Users\Mark Stacy\proj\.venv\Scripts\python.exe")
    command = cli.helper_command(resolve("dev"), "awa-veritas_cli", windows=True)
    assert command == ('"C:/Users/Mark Stacy/proj/.venv/Scripts/python.exe" "-P" "-m" "veritas.cli" '
                       '"token" "-e" "dev" "--app" "awa-veritas_cli"')
    assert "\\" not in command and "'" not in command


def test_helper_command_elsewhere_is_posix_quoted(monkeypatch):
    monkeypatch.setattr(cli.sys, "executable", "/home/me/my venv/bin/python")
    command = cli.helper_command(resolve("dev"), "awa-veritas_cli", windows=False)
    assert command == "'/home/me/my venv/bin/python' -P -m veritas.cli token -e dev --app awa-veritas_cli"


def test_settings_file_is_per_stack_and_client(isolated_home):
    first = cli.claude_settings_file(resolve("dev"), "awa-one app")
    second = cli.claude_settings_file(resolve("dev"), "awa-two")
    assert first != second and first.name.startswith("dev-awa-one_app-")
    assert second.name == "dev-awa-two.json"
    assert "awa-one app" in json.loads(first.read_text())["apiKeyHelper"]


def test_windows_launch_runs_a_child_and_exits_with_its_code(monkeypatch):
    started = {}

    class Child:
        def __init__(self, arguments, env):
            started.update(arguments=arguments, env=env)
            self.waits = 0

        def wait(self):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt   # Ctrl+C is Claude Code's; veritas keeps waiting
            return 3

    monkeypatch.setattr(cli.subprocess, "Popen", Child)
    monkeypatch.setattr(cli.os, "execve", lambda *arguments: pytest.fail("no exec on Windows"))
    with pytest.raises(SystemExit) as exited:
        cli.launch(["claude.cmd", "--settings", "x.json"], {"A": "1"}, windows=True)
    assert exited.value.code == 3
    assert started == {"arguments": ["claude.cmd", "--settings", "x.json"], "env": {"A": "1"}}


def test_default_stack_is_prod_and_veritas_env_overrides_it(monkeypatch):
    store = TokenStore()
    store.put("prod", "awa-cli", dict(stored(), access_token="prod-access"))
    store.set_active_client("prod", "awa-cli")
    store.put("dev", "awa-cli", dict(stored(), access_token="dev-access"))
    store.set_active_client("dev", "awa-cli")

    result = CliRunner().invoke(cli.main, ["token"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "prod-access"

    monkeypatch.setenv("VERITAS_ENV", "dev")
    result = CliRunner().invoke(cli.main, ["token"])
    assert result.output.strip() == "dev-access"
