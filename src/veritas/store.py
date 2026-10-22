"""Where tokens and the per-stack active client live.

Tokens go to the OS keychain through ``keyring``, keyed ``<stack>:<client>`` under the service
``veritas``. When no usable keychain backend exists (headless Linux, CI) they fall back to
``$VERITAS_HOME/tokens.json`` (default ``~/.config/veritas``), mode 0600. Never the working
directory: a token file next to code is one ``git add .`` from being published.

``VERITAS_TOKEN_STORE=file|keyring`` forces a backend.

The config file (``config.json``, no secrets) remembers the active client per stack, so that after
``veritas login --app X`` the other commands need no ``--app``. It also holds optional
``default_stack`` and per-stack base URL overrides (``stacks``; see stacks.py).
"""

import json
import os
from pathlib import Path

KEYRING_SERVICE = "veritas"


class StoreError(Exception):
    pass


def veritas_home():
    return Path(os.environ.get("VERITAS_HOME") or Path.home() / ".config" / "veritas")


def _record_key(stack, client):
    return f"{str(stack)}:{client}"


def _write_private_json(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(content, handle, indent=2, sort_keys=True)
    os.replace(temporary_path, path)


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as problem:
        raise StoreError(f"{path} is not valid JSON ({problem}); delete it and log in again")


class FileBackend:
    name = "file"

    def __init__(self, home=None):
        self.path = Path(home or veritas_home()) / "tokens.json"

    def get(self, key):
        value = _read_json(self.path).get(key)
        return json.dumps(value) if value is not None else None

    def put(self, key, serialized):
        records = _read_json(self.path)
        records[key] = json.loads(serialized)
        _write_private_json(self.path, records)

    def delete(self, key):
        records = _read_json(self.path)
        if records.pop(key, None) is not None:
            _write_private_json(self.path, records)


class KeyringBackend:
    name = "keyring"

    def __init__(self):
        import keyring
        import keyring.errors
        self._keyring = keyring
        self._errors = keyring.errors

    def get(self, key):
        return self._keyring.get_password(KEYRING_SERVICE, key)

    def put(self, key, serialized):
        try:
            self._keyring.set_password(KEYRING_SERVICE, key, serialized)
        except self._errors.KeyringError as problem:
            raise StoreError(f"keychain refused the token ({problem}); "
                             "set VERITAS_TOKEN_STORE=file to use the file store instead")

    def delete(self, key):
        try:
            self._keyring.delete_password(KEYRING_SERVICE, key)
        except self._errors.PasswordDeleteError:
            pass


def _keyring_usable():
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:
        return False
    active_backend = keyring.get_keyring()
    return not isinstance(active_backend, fail.Keyring) and active_backend.priority > 0


def default_backend():
    forced = os.environ.get("VERITAS_TOKEN_STORE", "").strip().lower()
    if forced == "file":
        return FileBackend()
    if forced == "keyring":
        return KeyringBackend()
    if forced:
        raise StoreError(f"VERITAS_TOKEN_STORE={forced!r}: expected 'file' or 'keyring'")
    return KeyringBackend() if _keyring_usable() else FileBackend()


class TokenStore:
    def __init__(self, backend=None, home=None):
        self.backend = backend or default_backend()
        self.config_path = Path(home or veritas_home()) / "config.json"

    # --- token records -----------------------------------------------------------------------
    def get(self, stack, client):
        serialized = self.backend.get(_record_key(stack, client))
        return json.loads(serialized) if serialized else None

    def put(self, stack, client, record):
        self.backend.put(_record_key(stack, client), json.dumps(record))

    def delete(self, stack, client):
        self.backend.delete(_record_key(stack, client))

    # --- config ------------------------------------------------------------------------------
    def stack_config(self):
        return _read_json(self.config_path).get("stacks", {})

    def default_stack(self):
        return _read_json(self.config_path).get("default_stack")

    # --- active client per stack -------------------------------------------------------------
    def active_client(self, stack):
        return _read_json(self.config_path).get("active_client", {}).get(str(stack))

    def set_active_client(self, stack, client):
        config = _read_json(self.config_path)
        active_by_stack = config.setdefault("active_client", {})
        if client is None:
            active_by_stack.pop(str(stack), None)
        else:
            active_by_stack[str(stack)] = client
        _write_private_json(self.config_path, config)
