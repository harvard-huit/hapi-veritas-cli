"""PKCE (RFC 7636, S256 only — the AS advertises nothing else) and the OAuth state value."""

import base64
import hashlib
import secrets


def make_verifier():
    # 48 random bytes -> 64 url-safe characters, inside RFC 7636's 43..128.
    return secrets.token_urlsafe(48)


def challenge_for(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_state():
    return secrets.token_urlsafe(24)
