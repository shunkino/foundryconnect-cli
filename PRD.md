# FoundryConnect CLI — MVP Product Requirements

- **Project:** `foundryconnect-cli`
- **CLI command:** `foundryconnect`
- **Status:** Draft — ready for technical validation
- **Date:** September 15, 2026
- **Target agents:** Codex CLI, Claude Code, Nous Research Hermes Agent, OpenCode

## 1. Product summary

FoundryConnect connects existing coding agents to Microsoft Foundry using
Microsoft Entra authentication.

Its primary goal is to eliminate manual API-key distribution and token copying
while allowing coding sessions to continue through routine access-token expiry.

**Product promise:**

> Configure once, authenticate with your organizational identity, and keep
> working without restarting agents for routine token renewal.

FoundryConnect is a configuration and diagnostics tool, not an inference proxy,
a new identity provider, or a replacement for Azure CLI.

## 2. Goals

The MVP must:

1. Support the four target agents.
2. Treat renewable Entra authentication as the default and only MVP auth mode.
3. Prefer agent-native authentication over custom integration code.
4. Use command-backed credential providers where native Entra support is absent.
5. Avoid writing access tokens or refresh tokens into agent configuration files.
6. Preserve active sessions across routine token expiry.
7. Provide safe, reversible configuration changes.
8. Distinguish authentication, authorization, endpoint, and protocol failures.
9. Document and enforce minimum supported agent versions.

## 3. Non-goals

The MVP will not:

- Create Foundry resources, deployments, or RBAC assignments.
- Create, import, or rotate resource API keys.
- Register an OAuth application.
- Maintain its own OAuth refresh-token store.
- Run a background token-refresh daemon.
- Rewrite credential files periodically.
- Run a local inference proxy.
- Translate between Responses, Chat Completions, and Anthropic Messages APIs.
- Guarantee every model works with every agent.
- Configure IDE extensions.
- Guarantee simultaneous multi-tenant operation in all agents.
- Certify managed identity or workload identity across all four agents.
- Support native Windows in the initial release.

Initial platforms: macOS, Linux, and WSL2.

## 4. Target user

A developer who:

- Has Azure CLI installed.
- Can authenticate to the intended Azure tenant.
- Has access to an existing Foundry resource and deployment.
- Uses one or more supported local coding agents.
- Wants to avoid shared API keys and manual credential maintenance.

Azure infrastructure provisioning and permission assignment remain the
responsibility of the developer's organization.

## 5. Research status and architectural decision

### 5.1 Preliminary capability findings

These findings guide implementation but are not release-certified.
Documentation and development branches can differ from installable releases.

| Agent | Candidate integration | MVP strategy |
|---|---|---|
| Codex | Provider authentication command with refresh settings | Invoke a FoundryConnect token helper |
| Claude Code | Native Microsoft Foundry authentication through an Azure credential chain | Configure native Entra mode |
| Hermes Agent | Native Foundry Entra provider and request-time credential integration | Configure its native provider |
| OpenCode | Azure CLI authentication integration with request-time renewal | Reuse native integration |

Before shipping, record exact agent versions and verify each mechanism in
source, documentation, and executable tests.

### 5.2 Integration preference order

1. Native Entra credential provider.
2. Native command-backed credential provider.
3. Supported request-time authentication extension.
4. If none is available: report unsupported and recommend an upgrade.

Do not silently fall back to a static token or resource API key.

### 5.3 What “hooks” mean

Three mechanisms must remain distinct:

- `foundryconnect <agent> on`: installs configuration.
- Lifecycle hooks: run at session start, prompt submission, or tool events.
- Runtime authentication callbacks: supply credentials during model requests.

**Runtime authentication must drive renewal.**

Lifecycle hooks may provide diagnostics, but cannot be the only renewal
mechanism: background calls, subagents, and long tool loops might not trigger
the expected lifecycle event.

Updating an environment variable in another process does not update the
environment of an already-running agent. Rewriting a config file also does not
guarantee that an agent reloads credentials.

## 6. Proposed command interface

These commands describe the application to be built.

| Command | Purpose |
|---|---|
| `foundryconnect login` | Check Azure CLI authentication and offer explicit sign-in |
| `foundryconnect profile add work` | Create a connection profile |
| `foundryconnect profile list` | List profiles without credentials |
| `foundryconnect codex on --profile work` | Configure Codex |
| `foundryconnect claude on --profile work` | Configure Claude Code |
| `foundryconnect hermes on --profile work` | Configure Hermes |
| `foundryconnect opencode on --profile work` | Configure or guide native OpenCode setup |
| `foundryconnect <agent> on --dry-run` | Preview sanitized configuration changes |
| `foundryconnect <agent> status` | Report configuration and authentication strategy |
| `foundryconnect doctor --profile work` | Diagnose a connection |
| `foundryconnect <agent> off` | Safely remove owned configuration |
| `foundryconnect auth token --profile work` | Internal command-backed credential interface |

The package name is `foundryconnect-cli`; the executable is `foundryconnect`.

### 6.1 First-run journey

1. Detect Azure CLI and installed agents.
2. Check authentication and intended tenant.
3. Accept endpoint and deployment information.
4. Optionally discover deployments when permissions permit.
5. Validate protocol compatibility.
6. Show a sanitized configuration diff.
7. Apply approved changes.
8. Offer an explicitly approved inference smoke test.
9. Explain how to launch the agent normally.

Discovery must be optional. Lack of management-plane listing permissions must
not prevent configuring an otherwise accessible deployment.

### 6.2 Meaning of `on`

`on` installs a renewable authentication mechanism, not a token snapshot.

Output must distinguish:

- Configuration installed.
- Credentials currently obtainable.
- Inference successfully verified.
- Initial restart required to activate provider changes.
- Routine token renewal does not require restarting.

### 6.3 Meaning of `off`

`off` removes or restores owned configuration.

It must not:

- Run `az logout`.
- Revoke organizational access.
- Delete unrelated credentials.
- Claim that previously issued tokens have been revoked.

## 7. Agent requirements

### 7.1 Codex

Candidate strategy:
