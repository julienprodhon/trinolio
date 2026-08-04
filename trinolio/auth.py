"""Nolio OAuth: the .env holds the credentials and the rotating tokens."""

import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

BASE_URL = "https://www.nolio.io/api"


def find_env() -> Path:
    """$TRINOLIO_ENV, else the nearest .env walking up from the cwd or from this file."""
    if override := os.environ.get("TRINOLIO_ENV"):
        return Path(override)
    for start in (Path.cwd(), Path(__file__).parent):
        for directory in (start, *start.parents):
            if (directory / ".env").is_file():
                return directory / ".env"
    return Path.cwd() / ".env"


ENV = find_env()


def load_env() -> None:
    for line in ENV.read_text().splitlines() if ENV.exists() else []:
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def save_env(**updates: str) -> None:
    lines = ENV.read_text().splitlines() if ENV.exists() else []
    keys = {line.split("=", 1)[0]: i for i, line in enumerate(lines) if "=" in line}
    for key, value in updates.items():
        if key in keys:
            lines[keys[key]] = f"{key}={value}"
        else:
            lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines) + "\n")
    ENV.chmod(0o600)


def post_token(**data: str) -> dict[str, Any]:
    load_env()
    response = httpx.post(
        f"{BASE_URL}/token/",
        auth=(os.environ["NOLIO_CLIENT_ID"], os.environ["NOLIO_CLIENT_SECRET"]),
        data=data,
    )
    response.raise_for_status()
    return response.json()


def save_tokens(tokens: dict[str, Any]) -> str:
    # Nolio rotates the refresh token on every refresh, so the reply replaces both tokens and
    # not just the access one. Saved to the .env and to this process, which outlives the write.
    fresh = {
        "NOLIO_ACCESS_TOKEN": tokens["access_token"],
        "NOLIO_ACCESS_TOKEN_EXPIRES_AT": str(int(time.time()) + tokens["expires_in"]),
        "NOLIO_REFRESH_TOKEN": tokens["refresh_token"],
    }
    save_env(**fresh)
    os.environ.update(fresh)
    return tokens["access_token"]


def auth_url() -> str:
    load_env()
    params = {
        "response_type": "code",
        "client_id": os.environ["NOLIO_CLIENT_ID"],
        "redirect_uri": os.environ["NOLIO_REDIRECT_URI"],
        "state": secrets.token_urlsafe(16),
    }
    return f"{BASE_URL}/authorize/?{urlencode(params)}"


def exchange(code: str) -> str:
    load_env()
    return save_tokens(
        post_token(
            grant_type="authorization_code",
            code=code,
            redirect_uri=os.environ["NOLIO_REDIRECT_URI"],
        )
    )


def token() -> str:
    """A valid access token, refreshed if the stored one is about to expire.

    The refresh token rotates on every use, so the .env holding it must not be hand-edited while
    a command is running.
    """
    load_env()
    expires_at = int(os.environ.get("NOLIO_ACCESS_TOKEN_EXPIRES_AT", "0"))
    if time.time() < expires_at - 60:
        return os.environ["NOLIO_ACCESS_TOKEN"]
    try:
        return save_tokens(
            post_token(grant_type="refresh_token", refresh_token=os.environ["NOLIO_REFRESH_TOKEN"])
        )
    except httpx.HTTPStatusError as error:
        raise RuntimeError(
            f"refresh rejected with HTTP {error.response.status_code}, so the stored refresh "
            "token is stale. Run `trinolio auth-url` and `trinolio exchange CODE` again."
        ) from error


def get(path: str, **params: Any) -> Any:
    """An authenticated GET. `path` is the endpoint without the surrounding slashes."""
    response = httpx.get(
        f"{BASE_URL}/{path}/",
        headers={"Authorization": f"Bearer {token()}"},
        params=params,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()
