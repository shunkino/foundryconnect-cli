# Agent compatibility and evidence

This is a **source-verified configuration integration**, not certification of real
inference, every deployment, or all later releases. Unit tests mock the agents and
Azure; they cannot establish RBAC, model availability, network access, or successful
renewal across an actual token expiry. Run the explicit inference smoke test and a
long-lived agent session in your environment before relying on this operationally.

## Supported floors

`foundryconnect` rejects missing executables, unsuccessful or timed-out `--version`
commands, older versions, and unrecognized/prerelease version banners. These are
the adapter's **minimum supported versions**, not a claim that every integration
feature first appeared in that release. Later stable versions are admitted, not
individually certified.

| Agent | Minimum CLI version | Verified release/source | Protocols | Authentication |
| --- | --- | --- | --- | --- |
| Codex | **0.128.0** | [`rust-v0.128.0` configuration schema](https://github.com/openai/codex/blob/rust-v0.128.0/codex-rs/core/config.schema.json) | Responses only | Native command-backed provider bearer token |
| Claude Code | **2.0.45** | [2.0.45 changelog](https://github.com/anthropics/claude-code/blob/v2.0.45/CHANGELOG.md), [published 2.0.45 package](https://www.npmjs.com/package/@anthropic-ai/claude-code/v/2.0.45) | Anthropic Messages only | Native Foundry `DefaultAzureCredential` |
| Hermes | **0.15.0** | [`v2026.5.28` CLI version](https://github.com/NousResearch/hermes-agent/blob/v2026.5.28/hermes_cli/__init__.py) | Responses, Chat Completions, Anthropic Messages | Native Azure Foundry Entra ID token provider |
| OpenCode | **1.18.25** | [`v1.18.25` Azure auth plugin](https://github.com/anomalyco/opencode/blob/v1.18.25/packages/opencode/src/plugin/azure.ts) | Responses, Chat Completions only | Native `/connect` → Azure → Microsoft Entra ID (Azure CLI) |

Hermes release tags use calendar versions; the executable reports a separate
semantic version. `v2026.5.28` reports `0.15.0`, not `2026.5.28`. The adapter accepts
the native `Hermes Agent v0.15.0 (2026.5.28)` banner and its additional diagnostic
lines. OpenCode 1.18.25 includes the native Azure CLI integration and its
[Bun-dependency removal](https://github.com/anomalyco/opencode/commit/733562e92a96).

## Exact configuration and renewal contracts

### Codex

The TOML plan selects `model_provider = "foundryconnect"` and the user's deployment
as `model`. Its `model_providers.foundryconnect` entry owns:

- `base_url`: the normalized `/openai/v1` base;
- `wire_api = "responses"`, `requires_openai_auth = false`,
  `supports_websockets = false`;
- `auth.command`: the absolute Python interpreter used by foundryconnect;
- `auth.args`: `["-m", "foundryconnect", "auth", "token", "--profile", NAME,
  "--home", ABSOLUTE_PROFILE_HOME]`;
- `auth.timeout_ms = 30000`, `auth.refresh_interval_ms = 60000`.

`ModelProviderAuthInfo` in the linked tagged schema specifies executable/argument
invocation without a shell, the timeout, and maximum token-cache age. It also
documents the 401 refresh path when proactive refresh is disabled. This adapter
keeps proactive refresh enabled: the next eligible request invokes the helper
after the cache becomes 60 seconds old. The helper obtains an Azure CLI token;
no API key or token is stored in the TOML. This is not a background refresh timer
and does not guarantee that every in-flight request can survive an expiry.

The helper prints only the raw token to its agent-owned stdout pipe. Do not
manually invoke it in recorded terminals or logs. Its interpreter and installed
Python package must remain accessible. The absolute `--home` pins the persistent
profile directory even if the agent inherits different `HOME`, `XDG_CONFIG_HOME`,
or `FOUNDRYCONNECT_HOME` values. Neither Codex's `auth.json` nor existing ChatGPT
login credentials are created, replaced, or deleted.

### Claude Code

`settings.json` receives these `env` values:

```text
CLAUDE_CODE_USE_FOUNDRY=1
ANTHROPIC_FOUNDRY_BASE_URL=https://RESOURCE.services.ai.azure.com/anthropic
ANTHROPIC_MODEL=DEPLOYMENT
ANTHROPIC_DEFAULT_SONNET_MODEL=DEPLOYMENT
ANTHROPIC_DEFAULT_OPUS_MODEL=DEPLOYMENT
ANTHROPIC_DEFAULT_HAIKU_MODEL=DEPLOYMENT
ANTHROPIC_SMALL_FAST_MODEL=DEPLOYMENT
```

All model aliases deliberately use the **same user deployment**. This prevents
an Opus/Haiku/background task from selecting an undeployed default; it does not
turn the deployed model into those model families or confer their capabilities.
Explicit agent/subagent model overrides can still bypass these defaults.

The published 2.0.45 `cli.js` selects the native Foundry client when
`CLAUDE_CODE_USE_FOUNDRY` is enabled. Without `ANTHROPIC_FOUNDRY_API_KEY`, it supplies
the Azure SDK bearer-token provider backed by `DefaultAzureCredential`, using
`https://cognitiveservices.azure.com/.default`. The native client calls the
provider for authentication headers; the Azure SDK manages expiry-aware caching
and token renewal. It is not a one-time `ANTHROPIC_AUTH_TOKEN` export.

The native SDK forbids setting both `ANTHROPIC_FOUNDRY_RESOURCE` and an explicit
base URL. The adapter uses only the URL and rejects a competing resource setting.
It also rejects API-key helpers, static credentials, skip-auth settings and
competing Bedrock/Vertex backend switches.

The [official Foundry guide](https://code.claude.com/docs/en/azure-ai-foundry)
is further reading. The version/schema evidence above was checked against the
published package and changelog, rather than assuming the latest guide describes
every old release.

### Hermes

The YAML plan owns the following fields:

```yaml
model:
  provider: azure-foundry
  default: DEPLOYMENT
  base_url: NORMALIZED_PROTOCOL_BASE
  api_mode: codex_responses  # or chat_completions / anthropic_messages
  auth_mode: entra_id
  entra:
    scope: https://ai.azure.com/.default
```

Evidence at the floor:

- [`runtime_provider.py`](https://github.com/NousResearch/hermes-agent/blob/v2026.5.28/hermes_cli/runtime_provider.py):
  `_resolve_azure_foundry_runtime` reads these fields and returns a callable token
  provider for `entra_id` instead of a persisted API key.
- [`azure_identity_adapter.py`](https://github.com/NousResearch/hermes-agent/blob/v2026.5.28/agent/azure_identity_adapter.py):
  `EntraIdentityConfig`, `DefaultAzureCredential`, and
  `get_bearer_token_provider` establish the native SDK renewal contract.
- [`anthropic_adapter.py`](https://github.com/NousResearch/hermes-agent/blob/v2026.5.28/agent/anthropic_adapter.py):
  a callable credential is adapted using an HTTP request hook that obtains the
  bearer token for each outbound request, rather than freezing it into the
  Anthropic client's static `api_key`.

Hermes must have `azure-identity` installed in **its own** Python environment;
foundryconnect does not install it. Hermes may offer its own lazy installation
depending on local policy. `hermes doctor` is the appropriate native credential
probe. A structurally configured adapter is not proof the dependency is present.

Hermes can infer Responses mode for GPT-5/codex/reasoning deployment names even
when `chat_completions` was requested. Do not assume arbitrary deployment naming
can force an unsupported wire API; validate the deployed model and effective
mode. The adapter does not guess model families, context windows, or capabilities.

### OpenCode

**A native login step is required.** After `az login`, launch OpenCode and run
`/connect`, choose **Azure**, then **Microsoft Entra ID (Azure CLI)**. Select the
same resource as the profile. An API-key connection is not equivalent.

The plan uses the native `azure` provider, `npm = "@ai-sdk/azure"`, an explicit
`options.baseURL`, and `options.resourceName`. The model catalog entry is keyed
by the deployment name and sets both `id` and `name` to that deployment. Both
`model` and `small_model` select `azure/DEPLOYMENT`. The catalog model's
`options.useCompletionUrls` is `true` for chat and `false` for Responses.

The tagged [`provider.ts`](https://github.com/anomalyco/opencode/blob/v1.18.25/packages/opencode/src/provider/provider.ts)
maps the configured model `id` to the SDK model ID. It merges provider and model
options before `selectAzureLanguageModel`, which selects `sdk.chat` when
`useCompletionUrls` is true and otherwise prefers `sdk.responses`. This is why
the adapter does **not** claim Anthropic support even though SDK fallbacks exist:
those fallbacks are not a verified explicit Anthropic protocol selection.
No invented context limits, prices, or family identifiers are emitted.

The tagged Azure plugin installs its fetch hook only for a native auth entry
with `type: "oauth"`. It removes API-key headers and supplies an Azure CLI bearer
token for each request, caching by scope and refreshing when expiry is within
60 seconds. It chooses `https://ai.azure.com/.default` for
`*.services.ai.azure.com` requests outside `/models`, otherwise
`https://cognitiveservices.azure.com/.default`. This adapter uses only
`/openai/v1` routes. The native connect callback initially probes the Cognitive
Services audience, even when subsequent Foundry requests use the AI audience.

`native_login_status` reads the native auth file **without writing it** and
reports only whether an Azure `oauth` entry exists. It cannot prove that the
entry was created by the built-in plugin, that the Azure login is still valid,
or that the entry's resource matches the selected profile. It intentionally does
not print token fields, rewrite `auth.json`, or fabricate the plugin's internal
placeholder values.

## Paths, conflicts and limits

| Configuration | Location precedence |
| --- | --- |
| Persistent profiles | `FOUNDRYCONNECT_HOME`, otherwise `${XDG_CONFIG_HOME:-~/.config}/foundryconnect` |
| Codex | `${CODEX_HOME:-~/.codex}/config.toml` |
| Claude Code | `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json` |
| Hermes | `${HERMES_HOME:-~/.hermes}/config.yaml` |
| OpenCode | local `OPENCODE_CONFIG`, otherwise `${XDG_CONFIG_HOME:-~/.config}/opencode/opencode.json` |
| OpenCode native auth, read only | `${XDG_DATA_HOME:-~/.local/share}/opencode/auth.json` |

Paths are made absolute before planning. Planning never writes files or mints
tokens. URL-valued `OPENCODE_CONFIG`, inline `OPENCODE_CONFIG_CONTENT`, and
additional `OPENCODE_CONFIG_DIR` layers are rejected. A selected JSONC path or
existing sibling/default/current-directory `opencode.jsonc` is explicitly
rejected: the tool does not silently ignore or overwrite JSONC.

Only public Azure resources are accepted:

- Responses/chat: `https://RESOURCE.openai.azure.com/openai/v1` or
  `https://RESOURCE.services.ai.azure.com/openai/v1`;
- Anthropic: `https://RESOURCE.services.ai.azure.com/anthropic`.

Resource URLs cannot contain credentials, custom ports, query strings or
fragments. Sovereign clouds, `/models`, project URLs, gateways and arbitrary
custom hosts are outside the MVP.

Conflicting static credentials, owned-provider headers, backend switches, and
current-process routing/model overrides are rejected rather than erased. Hermes'
native `.env` is checked read-only because it can inject credentials into its
runtime. Claude/Hermes service-principal, certificate, workload-identity,
managed-identity endpoint and authority environment overrides are also refused.
Conflict messages do not include secret values.

The parent CLI checks the Azure CLI account's tenant and subscription before an
actual `on`. **That is not an identity guarantee for `DefaultAzureCredential`.**
Claude/Hermes may select another existing SDK/developer-tool credential, or
ambient managed identity not exposed by the inspected variables. Neither native
strategy has a verified profile-level tenant/subscription pin in the fields this
adapter owns. Use an isolated, known developer identity environment and perform
native credential/inference checks. Avoid account switching during an agent
session; restart after changing identity or provider configuration.

Project-local, managed/organization, plugin, launch-argument, agent/subagent and
future environment overrides may take precedence over user configuration.
These are not comprehensively audited. A successful configuration install or
status check is **not verified inference**, nor a guarantee that future requests
will continue under the same identity. Azure CLI reauthentication, conditional
access, expired refresh credentials, missing data-plane RBAC, quota, deployment
capabilities and network failures remain operational requirements. No local
proxy, refresh daemon, static token file, auth-file rewrite or automatic logout
is introduced by these adapters.
