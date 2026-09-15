import json
import shutil
import subprocess
import time
from dataclasses import dataclass

from .models import FoundryError, Profile


@dataclass(frozen=True, repr=False)
class AccessToken:
    value: str
    expires_on: int


def azure(args: list[str]) -> object:
    executable = shutil.which("az")
    if executable is None:
        raise FoundryError("Azure CLI is not installed. Install Azure CLI >= 2.54 and run "
                           "foundryconnect login --sign-in.", "dependency")
    try:
        result = subprocess.run([executable, *args, "--output", "json", "--only-show-errors"],
                                capture_output=True, text=True, timeout=120 if "login" in args else 25)
    except subprocess.TimeoutExpired as exc:
        raise FoundryError("Azure CLI timed out. Check connectivity and sign in again.", "authentication") from exc
    except OSError as exc:
        raise FoundryError("Could not start Azure CLI.", "dependency") from exc
    if result.returncode:
        # Azure CLI errors may contain credentials or identity details. Do not echo them.
        raise FoundryError("Azure CLI failed. Run az account show or az login --tenant <tenant-id> "
                           "directly for details; no credentials were saved.", "authentication")
    if not result.stdout.strip() and args[:2] == ["account", "set"]:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FoundryError("Azure CLI returned invalid JSON; upgrade Azure CLI.", "authentication") from exc


def account(profile: Profile | None = None) -> dict:
    data = azure(["account", "show"])
    if not isinstance(data, dict) or not isinstance(data.get("tenantId"), str) or not isinstance(data.get("id"), str):
        raise FoundryError("Azure CLI did not return an active tenant and subscription.", "authentication")
    if profile and (data["tenantId"].lower() != profile.tenant.lower()
                    or data["id"].lower() != profile.subscription.lower()):
        raise FoundryError("Azure CLI tenant/subscription does not match this profile. Run "
                           f"az login --tenant {profile.tenant} and "
                           f"az account set --subscription {profile.subscription}. "
                           "Native agents use the active Azure CLI context.", "authentication")
    return data


def token(profile: Profile, scope: str) -> AccessToken:
    data = azure(["account", "get-access-token", "--tenant", profile.tenant,
                  "--subscription", profile.subscription, "--scope", scope])
    if not isinstance(data, dict):
        raise FoundryError("Azure CLI returned an invalid token response.", "authentication")
    value = data.get("accessToken")
    if (not isinstance(value, str) or not value or len(value) > 65536
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
        raise FoundryError("Azure CLI returned an empty or malformed token.", "authentication")
    expiry = data.get("expires_on")
    try:
        if isinstance(expiry, bool) or not isinstance(expiry, (str, int)):
            raise ValueError
        expires_on = int(expiry)
    except (ValueError, TypeError) as exc:
        raise FoundryError("Azure CLI token response lacks a valid expires_on timestamp. "
                           "Upgrade Azure CLI to >= 2.54.", "authentication") from exc
    if expires_on <= time.time() + 120:
        raise FoundryError("Azure CLI returned a token with less than two minutes remaining. "
                           "Reauthenticate; an expiring token will not be supplied.", "authentication")
    return AccessToken(value, expires_on)


def login(profile: Profile | None, sign_in: bool, tenant: str | None = None,
          subscription: str | None = None, device_code: bool = False) -> dict:
    if device_code and not sign_in:
        raise FoundryError("--device-code requires --sign-in.")
    if sign_in:
        tenant = profile.tenant if profile else tenant
        subscription = profile.subscription if profile else subscription
        if not tenant:
            raise FoundryError("Explicit sign-in requires --tenant UUID or --profile NAME.")
        # Keep login interactive: Azure CLI must be able to display browser/device-code guidance.
        executable = shutil.which("az")
        if executable is None:
            raise FoundryError("Install Azure CLI >= 2.54 before signing in.", "dependency")
        args = [executable, "login", "--tenant", tenant, "--output", "none"]
        if device_code:
            args.append("--use-device-code")
        try:
            result = subprocess.run(args, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise FoundryError("Azure sign-in timed out.", "authentication") from exc
        except OSError as exc:
            raise FoundryError("Could not start Azure CLI.", "dependency") from exc
        if result.returncode:
            raise FoundryError("Azure sign-in failed.", "authentication")
        if subscription:
            azure(["account", "set", "--subscription", subscription])
    return account(profile)
