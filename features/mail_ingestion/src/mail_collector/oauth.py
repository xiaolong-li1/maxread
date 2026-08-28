from __future__ import annotations

import json
import os
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


IMAP_SCOPES = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access openid profile email"
# Public client bundled by better-email-mcp for local Outlook device-code auth.
# Override with --client-id when operating a first-party registration.
DEFAULT_OUTLOOK_CLIENT_ID = "d56f8c71-9f7c-43f4-9934-be29cb6e77b0"


def _post_form(url: str, values: dict[str, str]) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        payload = error.read().decode(errors="replace")
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError(f"Microsoft OAuth HTTP {error.code}") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"Microsoft OAuth network error: {error.reason}") from None


def _save_cache(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)


def begin_device_flow(client_id: str, tenant: str) -> dict[str, object]:
    endpoint = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
    result = _post_form(endpoint, {"client_id": client_id, "scope": IMAP_SCOPES})
    if "error" in result:
        raise RuntimeError(str(result.get("error_description") or result["error"]))
    return result


def _id_token_username(id_token: object) -> str | None:
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        return None
    try:
        encoded = id_token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    for key in ("preferred_username", "email", "upn"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value.strip().lower()
    return None


def complete_device_flow(
    client_id: str,
    tenant: str,
    flow: dict[str, object],
    cache_path: Path,
    expected_username: str | None = None,
) -> dict[str, object]:
    endpoint = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    interval = int(flow.get("interval", 5))
    deadline = time.monotonic() + int(flow.get("expires_in", 900))
    device_code = str(flow["device_code"])
    while time.monotonic() < deadline:
        result = _post_form(endpoint, {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device_code,
        })
        error = result.get("error")
        if not error:
            authorized_username = _id_token_username(result.get("id_token"))
            if expected_username and authorized_username and authorized_username != expected_username.strip().lower():
                raise RuntimeError(
                    f"OAuth account mismatch: authorized {authorized_username}, expected {expected_username.strip().lower()}"
                )
            result["client_id"] = client_id
            result["tenant"] = tenant
            result["scope_request"] = IMAP_SCOPES
            result["authorized_username"] = authorized_username or ""
            result["expires_at"] = int(time.time()) + int(result.get("expires_in", 3600))
            _save_cache(cache_path, result)
            return result
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        raise RuntimeError(str(result.get("error_description") or error))
    raise RuntimeError("Microsoft device authorization expired")


def access_token(cache_path: Path, explicit_token: str | None = None) -> str:
    if explicit_token:
        return explicit_token
    if not cache_path.exists():
        raise RuntimeError("OAuth token cache not found; run outlook-auth")
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    token = str(cache.get("access_token") or "")
    if token and int(cache.get("expires_at", 0)) > int(time.time()) + 90:
        return token

    refresh_token = str(cache.get("refresh_token") or "")
    client_id = str(cache.get("client_id") or "")
    tenant = str(cache.get("tenant") or "consumers")
    if not refresh_token or not client_id:
        raise RuntimeError("OAuth token expired and cannot be refreshed; run outlook-auth again")
    endpoint = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    result = _post_form(endpoint, {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": IMAP_SCOPES,
    })
    if "error" in result:
        raise RuntimeError(str(result.get("error_description") or result["error"]))
    result["client_id"] = client_id
    result["tenant"] = tenant
    result["scope_request"] = IMAP_SCOPES
    result["refresh_token"] = result.get("refresh_token") or refresh_token
    result["expires_at"] = int(time.time()) + int(result.get("expires_in", 3600))
    _save_cache(cache_path, result)
    return str(result["access_token"])
