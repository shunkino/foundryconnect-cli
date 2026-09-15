import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .models import FoundryError, Profile
from .storage import atomic_write, locked


def home() -> Path:
    override = os.environ.get("FOUNDRYCONNECT_HOME")
    if override:
        return Path(override).expanduser().absolute()
    return (Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
            / "foundryconnect").absolute()


def validate(profile: Profile) -> Profile:
    for label, value in (("profile name", profile.name), ("deployment", profile.deployment)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise FoundryError(f"Invalid {label}: use 1-128 letters, digits, dots, underscores or hyphens.")
    for label, value in (("tenant", profile.tenant), ("subscription", profile.subscription)):
        try:
            UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise FoundryError(f"{label} must be a UUID, not a display name.") from exc
    if profile.protocol not in ("responses", "chat", "anthropic"):
        raise FoundryError("Protocol must be responses, chat or anthropic.", "protocol")
    try:
        url = urlsplit(profile.endpoint)
        port = url.port
    except (ValueError, TypeError) as exc:
        raise FoundryError("Invalid endpoint URL.", "endpoint") from exc
    if (url.scheme != "https" or url.username or url.password or port is not None
            or url.query or url.fragment or not url.hostname):
        raise FoundryError("Endpoint must be HTTPS without credentials, port, query or fragment.", "endpoint")
    host = url.hostname.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:openai|services\.ai)\.azure\.com", host):
        raise FoundryError("MVP endpoints must use a public Azure resource hostname: "
                           "<resource>.openai.azure.com or <resource>.services.ai.azure.com.", "endpoint")
    suffix = "/anthropic" if profile.protocol == "anthropic" else "/openai/v1"
    if profile.protocol == "anthropic" and not host.endswith(".services.ai.azure.com"):
        raise FoundryError("Anthropic requires a services.ai.azure.com resource endpoint.", "protocol")
    if url.path.rstrip("/") not in ("", suffix):
        raise FoundryError(f"Expected a resource root or {suffix}; project URLs and other API paths "
                           "are not supported.", "endpoint")
    return Profile(profile.name, f"https://{host}{suffix}", profile.deployment,
                   str(UUID(profile.tenant)), str(UUID(profile.subscription)), profile.protocol)


def load_all(root: Path) -> dict[str, Profile]:
    path = root / "profiles.json"
    if path.is_symlink():
        raise FoundryError("Refusing a symlinked profile store.")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
            raise FoundryError("Unsupported profile store format.")
        profiles = {}
        for name, fields in data["profiles"].items():
            profile = validate(Profile(**fields))
            if name != profile.name:
                raise FoundryError("Profile store contains a mismatched name.")
            profiles[name] = profile
        return profiles
    except (json.JSONDecodeError, TypeError) as exc:
        raise FoundryError("Invalid profile store; repair profiles.json before continuing.") from exc


def get(root: Path, name: str | None) -> Profile:
    profiles = load_all(root)
    if name is None and len(profiles) == 1:
        return next(iter(profiles.values()))
    if name is None:
        raise FoundryError("Specify --profile NAME (or create a profile with profile add).")
    if name not in profiles:
        raise FoundryError(f"Profile '{name}' does not exist.")
    return profiles[name]


def add(root: Path, profile: Profile) -> Profile:
    profile = validate(profile)
    with locked(root):
        profiles = load_all(root)
        if profile.name in profiles:
            raise FoundryError(f"Profile '{profile.name}' already exists; use a new name.")
        profiles[profile.name] = profile
        atomic_write(root / "profiles.json", json.dumps(
            {"version": 1, "profiles": {key: asdict(value) for key, value in profiles.items()}},
            indent=2) + "\n")
    return profile
