"""veritas: get and use Apigee OAuth tokens for HUIT AIS proxies.

    import veritas
    veritas.headers()                       # {"Authorization": "Bearer ..."} for the signed-in user
    httpx.get(url, auth=veritas.Auth())     # same, refreshed per request; requests works too

See veritas.api for the whole API.
"""

__version__ = "0.2.0"

from .api import Auth, VeritasError, authorization, gateway, headers, login, token  # noqa: E402

__all__ = ["Auth", "VeritasError", "authorization", "gateway", "headers", "login", "token"]
