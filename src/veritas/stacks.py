"""Stack -> base URLs.

Nothing here limits which stacks exist. A stack is any short lowercase name; its two base URLs
are resolved, first match wins:

  1. environment, scoped to that stack:  VERITAS_<STACK>_AUTH_BASE / VERITAS_<STACK>_GATEWAY_BASE
     (stack upper-cased, '-' -> '_'). Scoped on purpose: an unscoped override set for one stack
     and forgotten would silently send every other stack's tokens to the wrong host.
  2. config.json in VERITAS_HOME:        {"stacks": {"<stack>": {"auth_base": ..., "gateway_base": ...}}}
  3. the HUIT convention:                auth    https://go.<stack>.apis.huit.harvard.edu/oauth2/v1
                                                 (prod: https://go.apis.huit.harvard.edu/oauth2/v1)
                                         gateway https://<stack>.apis.huit.harvard.edu (prod too)

The auth base is the AS *issuer*. Endpoint paths (authorize, token, userinfo) are not built
here: they come from the issuer's discovery document (see oauth.AuthServer).
"""

import os
import re
from dataclasses import dataclass

KNOWN_STACKS = ("dev", "test", "sand", "stage", "prod")
DEFAULT_STACK = "dev"
STACK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

# Stacks whose AS host has no stack segment.
UNSEGMENTED_AUTH_STACKS = ("prod",)
AUTH_BASE_TEMPLATE = "https://go.{stack}.apis.huit.harvard.edu/oauth2/v1"
UNSEGMENTED_AUTH_BASE = "https://go.apis.huit.harvard.edu/oauth2/v1"
GATEWAY_BASE_TEMPLATE = "https://{stack}.apis.huit.harvard.edu"


class StackError(ValueError):
    pass


@dataclass(frozen=True)
class Stack:
    name: str
    auth_base: str
    gateway_base: str

    def __str__(self):
        # Messages and store keys use the bare name.
        return self.name


def validate_stack_name(name):
    if not name or not STACK_NAME_PATTERN.match(name):
        raise StackError(f"stack {name!r} is not a valid name (lowercase letters, digits, '-'; "
                         f"known: {', '.join(KNOWN_STACKS)})")
    return name


def _env_override(stack_name, suffix):
    variable = f"VERITAS_{stack_name.upper().replace('-', '_')}_{suffix}"
    return os.environ.get(variable) or None


def _https_base(value, what, stack_name):
    if not value.startswith("https://"):
        raise StackError(f"{what} for stack {stack_name!r} must be an https:// URL, got {value!r}")
    return value.rstrip("/")


def resolve(stack_name, stack_config=None):
    """Build the Stack for ``stack_name``. ``stack_config`` is config.json's ``stacks`` mapping."""
    validate_stack_name(stack_name)
    configured = (stack_config or {}).get(stack_name) or {}

    auth_base = (_env_override(stack_name, "AUTH_BASE") or configured.get("auth_base")
                 or (UNSEGMENTED_AUTH_BASE if stack_name in UNSEGMENTED_AUTH_STACKS
                     else AUTH_BASE_TEMPLATE.format(stack=stack_name)))
    gateway_base = (_env_override(stack_name, "GATEWAY_BASE") or configured.get("gateway_base")
                    or GATEWAY_BASE_TEMPLATE.format(stack=stack_name))
    return Stack(name=stack_name,
                 auth_base=_https_base(auth_base, "auth_base", stack_name),
                 gateway_base=_https_base(gateway_base, "gateway_base", stack_name))
