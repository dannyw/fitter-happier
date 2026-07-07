"""Strava OAuth: token storage, one-time authorization, and refresh.

Tokens and client credentials live in a gitignored JSON file under ``data/``
(the whole ``data/`` directory is gitignored). The client_id / client_secret
come from a Strava API application you register once at
https://www.strava.com/settings/api — put them in the tokens file or in the
``STRAVA_CLIENT_ID`` / ``STRAVA_CLIENT_SECRET`` environment variables.

Flow:

    python -m src.ingest.strava_auth        # one-time browser consent

opens Strava's consent page, captures the redirect on ``http://localhost``,
exchanges the code for tokens, and writes them to the tokens file. After that,
:func:`get_access_token` transparently refreshes the short-lived access token.
"""

from __future__ import annotations

import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

DEFAULT_TOKENS_PATH = Path("data/strava_tokens.json")

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"

# activity:read_all is required to read private / hidden activities.
SCOPE = "read,activity:read_all"

# Refresh the access token this many seconds before it actually expires.
_EXPIRY_SKEW_SEC = 120

# Loopback port for the OAuth redirect. Must match the "Authorization Callback
# Domain" (localhost) configured on the Strava API application.
_CALLBACK_PORT = 8721


# -- Token file I/O ------------------------------------------------------------


def load_tokens(path: str | Path = DEFAULT_TOKENS_PATH) -> dict[str, object]:
    """Load the tokens file, returning {} if it does not exist."""
    p = Path(path)
    if not p.is_file():
        return {}
    return json.loads(p.read_text())


def save_tokens(data: dict[str, object], path: str | Path = DEFAULT_TOKENS_PATH) -> None:
    """Write the tokens file with owner-only (0600) permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
    p.chmod(0o600)


def _client_creds(tokens: dict[str, object]) -> tuple[str, str]:
    """Resolve client_id / client_secret from env or the tokens file."""
    client_id = os.environ.get("STRAVA_CLIENT_ID") or tokens.get("client_id")
    client_secret = os.environ.get("STRAVA_CLIENT_SECRET") or tokens.get("client_secret")
    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing Strava client credentials. Set STRAVA_CLIENT_ID and "
            "STRAVA_CLIENT_SECRET (or add client_id/client_secret to the tokens "
            "file). Register an app at https://www.strava.com/settings/api"
        )
    return str(client_id), str(client_secret)


# -- Refresh -------------------------------------------------------------------


def needs_refresh(expires_at: float | None, now: float, skew: int = _EXPIRY_SKEW_SEC) -> bool:
    """True if there is no access token or it expires within *skew* seconds."""
    if expires_at is None:
        return True
    return now >= (float(expires_at) - skew)


def get_access_token(
    path: str | Path = DEFAULT_TOKENS_PATH,
    *,
    now: float | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Return a valid access token, refreshing and persisting it if needed."""
    now = time.time() if now is None else now
    tokens = load_tokens(path)
    if not tokens.get("refresh_token"):
        raise RuntimeError(
            "No Strava refresh token found. Run `python -m src.ingest.strava_auth` "
            "to authorize first."
        )

    if not needs_refresh(tokens.get("expires_at"), now):  # type: ignore[arg-type]
        return str(tokens["access_token"])

    client_id, client_secret = _client_creds(tokens)
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
    }
    owns_client = client is None
    client = client or httpx.Client(timeout=20)
    try:
        resp = client.post(TOKEN_URL, data=payload)
        resp.raise_for_status()
        fresh = resp.json()
    finally:
        if owns_client:
            client.close()

    tokens.update(
        {
            "access_token": fresh["access_token"],
            "refresh_token": fresh["refresh_token"],
            "expires_at": fresh["expires_at"],
        }
    )
    save_tokens(tokens, path)
    return str(tokens["access_token"])


# -- One-time authorization ----------------------------------------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures the ``?code=`` (or ``?error=``) from Strava's redirect."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — required name from BaseHTTPRequestHandler
        params = parse_qs(urlparse(self.path).query)
        _CallbackHandler.code = params.get("code", [None])[0]
        _CallbackHandler.error = params.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        msg = "Strava authorization complete. You can close this tab."
        self.wfile.write(msg.encode())

    def log_message(self, *args: object) -> None:  # silence stderr access log
        pass


def authorize(
    path: str | Path = DEFAULT_TOKENS_PATH,
    *,
    port: int = _CALLBACK_PORT,
    open_browser: bool = True,
) -> dict[str, object]:
    """Run the one-time OAuth consent flow and persist the resulting tokens."""
    tokens = load_tokens(path)
    client_id, client_secret = _client_creds(tokens)
    redirect_uri = f"http://localhost:{port}"

    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": SCOPE,
        }
    )
    auth_url = f"{AUTHORIZE_URL}?{query}"
    print(f"Opening browser for Strava authorization:\n  {auth_url}")
    if open_browser:
        webbrowser.open(auth_url)

    # Serve exactly one request: the redirect from Strava.
    _CallbackHandler.code = None
    _CallbackHandler.error = None
    server = HTTPServer(("localhost", port), _CallbackHandler)
    try:
        server.handle_request()
    finally:
        server.server_close()

    if _CallbackHandler.error:
        raise RuntimeError(f"Strava authorization denied: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise RuntimeError("No authorization code received from Strava.")

    with httpx.Client(timeout=20) as client:
        resp = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": _CallbackHandler.code,
            },
        )
        resp.raise_for_status()
        granted = resp.json()

    tokens.update(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": granted["access_token"],
            "refresh_token": granted["refresh_token"],
            "expires_at": granted["expires_at"],
            "scope": SCOPE,
            "athlete_id": (granted.get("athlete") or {}).get("id"),
        }
    )
    save_tokens(tokens, path)
    print(f"Authorization complete. Tokens saved to {path}")
    return tokens


def main() -> None:
    authorize()


if __name__ == "__main__":
    main()
