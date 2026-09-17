"""
auth.py - Google OAuth (device flow) for ytmusic-tui.

ytmusicapi needs the user's own Google Cloud OAuth client for authenticated
access (Google requires this for unofficial clients). We store the client
credentials and the resulting token under ~/.config/ytmusic-tui/ and expose
small helpers to drive the device-code flow from inside the TUI.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ytmusicapi.auth.oauth.credentials import OAuthCredentials
from ytmusicapi.auth.oauth.token import RefreshingToken


CONFIG_DIR = Path.home() / ".config" / "ytmusic-tui"
OAUTH_FILE = CONFIG_DIR / "oauth.json"
CLIENT_FILE = CONFIG_DIR / "oauth_client.json"

CONSOLE_URL = "https://console.cloud.google.com/apis/credentials"

# Google returns these while the user has not finished approving yet.
_PENDING = {"authorization_pending", "slow_down"}


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_client() -> Optional[Tuple[str, str]]:
    """Return the saved (client_id, client_secret), if any."""
    if not CLIENT_FILE.exists():
        return None
    try:
        data = json.loads(CLIENT_FILE.read_text(encoding="utf-8"))
        client_id = (data.get("client_id") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()
        if client_id and client_secret:
            return client_id, client_secret
    except Exception:
        pass
    return None


def save_client(client_id: str, client_secret: str) -> None:
    _ensure_dir()
    CLIENT_FILE.write_text(
        json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2),
        encoding="utf-8",
    )
    try:
        os.chmod(CLIENT_FILE, 0o600)
    except OSError:
        pass


def is_signed_in() -> bool:
    return OAUTH_FILE.exists()


def sign_out() -> None:
    if OAUTH_FILE.exists():
        try:
            OAUTH_FILE.unlink()
        except OSError:
            pass


def make_credentials(client_id: str, client_secret: str) -> OAuthCredentials:
    return OAuthCredentials(client_id, client_secret)


def begin_device_flow(client_id: str, client_secret: str) -> Tuple[OAuthCredentials, Dict[str, Any]]:
    """Start the device flow and return (credentials, code info)."""
    credentials = OAuthCredentials(client_id, client_secret)
    code = credentials.get_code()
    return credentials, code


def poll_device_token(credentials: OAuthCredentials, device_code: str) -> Optional[Dict[str, Any]]:
    """Return the token dict once approved, None while pending, raise on failure."""
    raw = credentials.token_from_code(device_code)
    if "access_token" in raw:
        return raw
    error = raw.get("error")
    if error in _PENDING:
        return None
    raise RuntimeError(raw.get("error_description") or error or "authorization failed")


def save_token(credentials: OAuthCredentials, raw: Dict[str, Any]) -> None:
    """Persist a device-flow token to OAUTH_FILE."""
    _ensure_dir()
    token = RefreshingToken(
        credentials=credentials,
        access_token=raw["access_token"],
        refresh_token=raw["refresh_token"],
        scope=raw["scope"],
        token_type=raw["token_type"],
        expires_in=raw.get("refresh_token_expires_in", raw.get("expires_in", 3600)),
    )
    token.update(raw)
    token.local_cache = OAUTH_FILE


def auth_kwargs() -> Optional[Dict[str, Any]]:
    """Keyword arguments to build an authenticated YTMusic client, or None."""
    client = load_client()
    if not (client and is_signed_in()):
        return None
    client_id, client_secret = client
    return {
        "auth": str(OAUTH_FILE),
        "oauth_credentials": OAuthCredentials(client_id, client_secret),
    }
