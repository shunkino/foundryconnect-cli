"""Source-verified, keyless agent configuration plans."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .models import AGENTS, AI_SCOPE, COGNITIVE_SCOPE, FoundryError, Plan, Profile


MIN_VERSIONS = {
    "codex": (0, 128, 0),
    "claude": (2, 0, 45),
    "hermes": (0, 15, 0),
    "opencode": (1, 18, 25),
}
_PROTOCOLS = {
    "codex": {"responses"},
    "claude": {"anthropic"},
    "hermes": {"responses", "chat", "anthropic"},
    "opencode": {"responses", "chat"},
}
_IDENTITY_ENV = {
    "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
    "AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_CLIENT_CERTIFICATE_PASSWORD",
    "AZURE_CLIENT_SEND_CERTIFICATE_CHAIN", "AZURE_FEDERATED_TOKEN_FILE",
    "AZURE_AUTHORITY_HOST", "AZURE_TOKEN_CREDENTIALS", "AZURE_USERNAME",
    "AZURE_PASSWORD", "IDENTITY_ENDPOINT", "IDENTITY_HEADER", "IDENTITY_SERVER_THUMBPRINT",
    "MSI_ENDPOINT", "MSI_SECRET", "IMDS_ENDPOINT", "AZURE_POD_IDENTITY_AUTHORITY_HOST",
}
_ENV_CONFLICTS = {
    "codex": {
        "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN",
        "OPENAI_BASE_URL", "OPENAI_API_BASE", "CODEX_API_KEY", "CODEX_PROFILE",
    },
    "claude": {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        "CLAUDE_CODE_USE_MANTLE",
    },
    "hermes": {
        "AZURE_FOUNDRY_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "AZURE_FOUNDRY_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE",
        "CUSTOM_BASE_URL", "HERMES_MODEL", "HERMES_PROVIDER", "HERMES_API_KEY",
        "HERMES_BASE_URL", "LLM_MODEL", "LLM_PROVIDER", "HERMES_API_MODE",
    },
    "opencode": {
        "AZURE_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN",
        "AZURE_RESOURCE_NAME", "AZURE_RESOURCE_GROUP", "AZURE_BASE_URL",
        "OPENAI_API_KEY", "OPENCODE_CONFIG_CONTENT", "OPENCODE_CONFIG_DIR",
    },
}
_FALSE_FLAGS = {
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
}
_CREDENTIAL_KEYS = {
    "api_key", "apikey", "api-key", "api", "env_key", "experimental_bearer_token",
    "auth_token", "access_token", "azure_ad_token", "azureadtokenprovider",
    "azure_ad_token_provider", "authorization", "x-api-key", "subscription-key",
    "ocp-apim-subscription-key",
}


def _agent(agent: str) -> None:
    if agent not in AGENTS:
        raise FoundryError("Unknown agent. Choose codex, claude, hermes or opencode.")


def _absolute(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _config_home() -> Path:
    return _absolute(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def _profile_home() -> Path:
    return _absolute(os.environ.get("FOUNDRYCONNECT_HOME") or _config_home() / "foundryconnect")


def _validate(agent: str, profile: Profile) -> None:
    _agent(agent)
    if profile.protocol not in _PROTOCOLS[agent]:
        raise FoundryError(f"{agent} does not support the selected protocol in this adapter.",
                           "compatibility")
    try:
        parsed = urlsplit(profile.endpoint)
    except ValueError as exc:
        raise FoundryError("A valid normalized public Azure resource endpoint is required.",
                           "compatibility") from exc
    host = parsed.hostname or ""
    host_pattern = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.(?:openai|services\.ai)\.azure\.com"
    expected_path = "/anthropic" if profile.protocol == "anthropic" else "/openai/v1"
    if (parsed.scheme != "https" or not re.fullmatch(host_pattern, host)
            or parsed.netloc != host or parsed.path != expected_path
            or parsed.query or parsed.fragment
            or (profile.protocol == "anthropic" and not host.endswith(".services.ai.azure.com"))):
        raise FoundryError("A normalized public Azure resource endpoint matching the protocol is required.",
                           "compatibility")
    if not profile.deployment or not re.fullmatch(r"[A-Za-z0-9._-]+", profile.deployment):
        raise FoundryError("Deployment must contain only letters, digits, dots, underscores or hyphens.")
    if not profile.name or not re.fullmatch(r"[A-Za-z0-9._-]+", profile.name):
        raise FoundryError("Profile name must contain only letters, digits, dots, underscores or hyphens.")


def scope_for(agent: str, profile: Profile) -> str:
    """Return the audience used by this agent's verified token strategy."""
    _validate(agent, profile)
    if agent == "claude":
        return COGNITIVE_SCOPE
    if agent == "hermes":
        return AI_SCOPE
    return AI_SCOPE if ".services.ai.azure.com/" in profile.endpoint else COGNITIVE_SCOPE


def _opencode_path() -> Path:
    if os.environ.get("OPENCODE_CONFIG_CONTENT"):
        raise FoundryError("Unset OPENCODE_CONFIG_CONTENT: dynamic config cannot be safely managed.",
                           "compatibility")
    if os.environ.get("OPENCODE_CONFIG_DIR"):
        raise FoundryError("Unset OPENCODE_CONFIG_DIR: additional configuration layers are unsupported.",
                           "compatibility")
    selected = os.environ.get("OPENCODE_CONFIG")
    if selected:
        try:
            remote = bool(urlsplit(selected).scheme or selected.startswith("//"))
        except ValueError:
            remote = True
        if remote:
            raise FoundryError("OPENCODE_CONFIG must be a local JSON path.", "compatibility")
    path = _absolute(selected or _config_home() / "opencode" / "opencode.json")
    candidates = {path.with_suffix(".jsonc"), _config_home() / "opencode" / "opencode.jsonc",
                  Path.cwd() / "opencode.jsonc"}
    if path.suffix.lower() == ".jsonc" or any(p.exists() for p in candidates):
        raise FoundryError("Existing OpenCode JSONC configuration is unsupported; migrate it explicitly "
                           "to JSON first. No JSONC file will be overwritten or ignored.", "compatibility")
    return path


def make_plan(agent: str, profile: Profile) -> Plan:
    """Build an absolute-path plan without creating files or acquiring tokens."""
    _validate(agent, profile)
    if agent == "codex":
        provider = ("model_providers", "foundryconnect")
        changes = {
            ("model",): profile.deployment,
            ("model_provider",): "foundryconnect",
            provider + ("name",): "Azure Foundry (foundryconnect)",
            provider + ("base_url",): profile.endpoint,
            provider + ("wire_api",): "responses",
            provider + ("requires_openai_auth",): False,
            provider + ("supports_websockets",): False,
            provider + ("auth", "command"): sys.executable,
            provider + ("auth", "args"): [
                "-m", "foundryconnect", "auth", "token", "--profile", profile.name,
                "--home", str(_profile_home()),
            ],
            provider + ("auth", "timeout_ms"): 30000,
            provider + ("auth", "refresh_interval_ms"): 60000,
        }
        return Plan(agent, _absolute(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
                    / "config.toml", "toml", changes, "command-backed renewable bearer token",
                    ("The Python interpreter and foundryconnect installation must remain available. "
                     "The helper pins the absolute profile home; auth.json is never modified.",))
    if agent == "claude":
        values = {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_BASE_URL": profile.endpoint,
            "ANTHROPIC_MODEL": profile.deployment,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": profile.deployment,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": profile.deployment,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": profile.deployment,
            "ANTHROPIC_SMALL_FAST_MODEL": profile.deployment,
        }
        return Plan(agent, _absolute(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
                    / "settings.json", "json", {("env", k): v for k, v in values.items()},
                    "native Foundry DefaultAzureCredential with renewable bearer tokens",
                    ("Main, Sonnet, Opus, Haiku and small/fast model defaults all use your deployment "
                     "to avoid requests to undeployed models; these aliases do not change its capabilities.",
                     "Native identity can select cached SDK credentials other than Azure CLI. "
                     "Restart Claude after changing Azure accounts and verify real inference."))
    if agent == "hermes":
        mode = {"responses": "codex_responses", "chat": "chat_completions",
                "anthropic": "anthropic_messages"}[profile.protocol]
        values = {"provider": "azure-foundry", "default": profile.deployment,
                  "base_url": profile.endpoint, "api_mode": mode, "auth_mode": "entra_id"}
        changes = {("model", k): v for k, v in values.items()}
        changes[("model", "entra", "scope")] = scope_for(agent, profile)
        return Plan(agent, _absolute(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
                    / "config.yaml", "yaml", changes,
                    "native Azure Foundry Entra ID SDK renewable token provider",
                    ("Hermes requires azure-identity in its own Python environment. "
                     "Use hermes doctor to verify native credentials; SDK cache/chain identity "
                     "is not proven by an Azure CLI account check.",
                     "Hermes may infer Responses for GPT-5/codex/reasoning deployment names even "
                     "when chat is selected. Choose a deployment supporting the effective protocol."))
    model = ("provider", "azure", "models", profile.deployment)
    changes = {
        ("model",): "azure/" + profile.deployment,
        ("small_model",): "azure/" + profile.deployment,
        ("provider", "azure", "npm"): "@ai-sdk/azure",
        ("provider", "azure", "options", "baseURL"): profile.endpoint,
        ("provider", "azure", "options", "resourceName"): urlsplit(profile.endpoint).hostname.split(".")[0],
        model + ("id",): profile.deployment,
        model + ("name",): profile.deployment,
        model + ("options", "useCompletionUrls"): profile.protocol == "chat",
    }
    return Plan(agent, _opencode_path(), "json", changes,
                "native Azure /connect Microsoft Entra ID (Azure CLI), renewable per-request tokens",
                ("In OpenCode run /connect, choose Azure, then Microsoft Entra ID (Azure CLI), "
                 f"and select Azure resource {urlsplit(profile.endpoint).hostname.split('.')[0]}. "
                 "Reconnect if the existing native login names a different resource. Run az login first. "
                 "foundryconnect never creates or rewrites native auth.json.",
                 "A native oauth entry is only evidence of configuration, not verified credentials "
                 "or a matching resource. Model IDs are deployment names, not inferred model families."))


def check_version(agent: str) -> str:
    _agent(agent)
    try:
        result = subprocess.run([agent, "--version"], capture_output=True, text=True,
                                timeout=10, check=False)
    except (FileNotFoundError, PermissionError) as exc:
        raise FoundryError(f"{agent} is missing or not executable; install it first.", "dependency") from exc
    except subprocess.TimeoutExpired as exc:
        raise FoundryError(f"{agent} --version timed out.", "compatibility") from exc
    except (OSError, UnicodeError) as exc:
        raise FoundryError(f"Cannot execute {agent} --version.", "dependency") from exc
    if result.returncode != 0:
        raise FoundryError(f"{agent} --version failed; compatibility cannot be verified.",
                           "compatibility")
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    prefixes = {"codex": r"codex(?:-cli)?", "claude": r"claude(?: code)?",
                "hermes": r"hermes(?:-agent| agent)?", "opencode": r"opencode"}
    suffix = (r"(?:\s+\(\d{4}[.-]\d{1,2}[.-]\d{1,2}\))?" if agent == "hermes"
              else r"(?:\s+\(Claude Code\))?" if agent == "claude" else "")
    banner = output.splitlines()[0] if output else ""
    match = re.fullmatch(r"(?:" + prefixes[agent] + r"\s+)?v?(\d+\.\d+\.\d+)" + suffix,
                         banner, re.IGNORECASE)
    if agent != "hermes" and len(output.splitlines()) > 1:
        match = None
    if not match:
        raise FoundryError(f"Cannot verify a stable {agent} version; prerelease and unparseable "
                           "versions are unsupported.", "compatibility")
    version = match.group(1)
    parts = tuple(int(p) for p in version.split("."))
    floor = MIN_VERSIONS[agent]
    if parts < floor:
        minimum = ".".join(map(str, floor))
        raise FoundryError(f"{agent} {version} is below the supported minimum {minimum}; upgrade first.",
                           "compatibility")
    return version


def _at(document: Mapping, path: tuple[str, ...]):
    value = document
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _present(value) -> bool:
    return value is not None and value != "" and value is not False and value != {}


def _credentials(value) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key, child in value.items():
        if str(key).lower() in _CREDENTIAL_KEYS and _present(child):
            return True
        if _credentials(child):
            return True
    return False


def _env_conflicts(agent: str, values: Mapping) -> bool:
    keys = _ENV_CONFLICTS[agent] | (_IDENTITY_ENV if agent in {"claude", "hermes"} else set())
    for key in keys:
        value = values.get(key)
        if key in _FALSE_FLAGS and str(value).lower() in {"0", "false", "none", ""}:
            continue
        if _present(value):
            return True
    return False


def check_conflicts(plan: Plan, document: Mapping, environ: Mapping[str, str] | None = None) -> None:
    """Refuse competing credentials/routes without rendering their values."""
    _agent(plan.agent)
    if not isinstance(document, Mapping):
        raise FoundryError("Agent configuration must be a mapping.")
    env = os.environ if environ is None else environ
    if _env_conflicts(plan.agent, env):
        raise FoundryError("Conflicting credential, backend or endpoint environment override. "
                           "Remove the agent/Azure identity override before switching.", "conflict")
    if plan.agent == "claude":
        settings_env = document.get("env", {})
        if not isinstance(settings_env, Mapping):
            raise FoundryError("Claude env settings must be a mapping.", "conflict")
        if _env_conflicts("claude", settings_env) or any(
            _present(document.get(k)) for k in ("apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID")
        ):
            raise FoundryError("Claude settings contain competing credentials or backend selection.",
                               "conflict")
        for path, value in plan.changes.items():
            if path[0] == "env" and _present(env.get(path[1])) and env[path[1]] != value:
                raise FoundryError("Claude environment overrides an owned model or Foundry setting.",
                                   "conflict")
    elif plan.agent == "codex":
        provider = _at(document, ("model_providers", "foundryconnect")) or {}
        if not isinstance(provider, Mapping):
            raise FoundryError("Codex provider settings must be a mapping.", "conflict")
        if (_credentials(provider) or any(_present(provider.get(k)) for k in
                                         ("http_headers", "env_http_headers", "query_params", "aws"))
                or _present(document.get("profile"))):
            raise FoundryError("Codex has competing provider credentials, headers or a selected profile.",
                               "conflict")
    elif plan.agent == "hermes":
        model = document.get("model", {})
        if isinstance(model, Mapping) and (
            _credentials(model) or any(_present(model.get(k)) for k in
                                      ("headers", "default_headers", "extra_headers"))
            or _at(model, ("entra", "exclude_interactive_browser")) is False
        ):
            raise FoundryError("Hermes model contains competing credentials or identity overrides.",
                               "conflict")
        dotenv = plan.path.parent / ".env"
        if dotenv.exists():
            try:
                lines = dotenv.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError) as exc:
                raise FoundryError("Cannot inspect Hermes .env safely.", "conflict") from exc
            values = {}
            for line in lines:
                match = re.match(r"\s*(?:export\s+)?([A-Za-z_]\w*)\s*=\s*(.*)", line)
                if match:
                    values[match[1]] = match[2].strip().strip("'\"")
            if _env_conflicts("hermes", values):
                raise FoundryError("Hermes .env contains competing credentials or routing/identity "
                                   "overrides. Remove them before switching.", "conflict")
    else:
        provider = _at(document, ("provider", "azure")) or {}
        if not isinstance(provider, Mapping):
            raise FoundryError("OpenCode Azure provider settings must be a mapping.", "conflict")
        options = provider.get("options", {})
        if not isinstance(options, Mapping):
            raise FoundryError("OpenCode Azure options must be a mapping.", "conflict")
        if (_credentials(provider) or any(_present(options.get(k)) for k in ("headers", "fetch"))
                or options.get("useDeploymentBasedUrls") is True):
            raise FoundryError("OpenCode Azure configuration contains competing credentials or routing.",
                               "conflict")
        if any(k in document and not isinstance(document[k], list)
               for k in ("disabled_providers", "enabled_providers")):
            raise FoundryError("OpenCode provider selections must be arrays.", "conflict")
        if ("azure" in (document.get("disabled_providers") or [])
                or ("enabled_providers" in document
                    and "azure" not in document["enabled_providers"])):
            raise FoundryError("OpenCode provider selection disables Azure.", "conflict")
        deployment = str(plan.changes[("model",)]).removeprefix("azure/")
        model = _at(provider, ("models", deployment)) or {}
        if isinstance(model, Mapping) and (_present(model.get("provider"))
                                          or _present(_at(model, ("options", "headers")))):
            raise FoundryError("OpenCode model contains a competing provider or headers.", "conflict")


def native_login_status(agent: str) -> tuple[bool, str]:
    _agent(agent)
    if agent != "opencode":
        strategy = {
            "codex": "Codex invokes the renewable foundryconnect token helper; no native login is required.",
            "claude": "Claude uses native Foundry DefaultAzureCredential; native credentials are not "
                      "verified by this structural check.",
            "hermes": "Hermes uses native Entra ID with azure-identity; run hermes doctor to verify "
                      "the SDK credential chain.",
        }
        return True, strategy[agent]
    path = _absolute(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode" / "auth.json"
    guidance = "Run OpenCode /connect -> Azure -> Microsoft Entra ID (Azure CLI) after az login."
    try:
        if path.stat().st_size > 1_048_576:
            return False, "OpenCode native auth file is too large to inspect safely. " + guidance
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, "No OpenCode native Azure credential entry exists. " + guidance
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "Cannot safely read OpenCode native auth file. " + guidance
    entry = data.get("azure") if isinstance(data, Mapping) else None
    if not isinstance(entry, Mapping) or entry.get("type") != "oauth":
        return False, "No OpenCode native Azure oauth credential entry exists. " + guidance
    return True, ("An OpenCode native Azure oauth credential entry exists; credentials, expiry and "
                  "resource match are not verified. " + guidance)
