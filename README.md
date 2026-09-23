# FoundryConnect CLI

Connect Codex, Claude Code, Hermes Agent and OpenCode to Microsoft Foundry with
renewable Microsoft Entra authentication. No API keys, copied tokens, inference
proxy, refresh daemon, OAuth application or separate refresh-token store.

**First MVP:** local configuration and diagnostics on macOS, Linux and WSL2,
using Python 3.11+ and Azure CLI 2.54+. Native Windows is not supported.
Agent integrations are based on released implementations, not certified
against every deployment. See [compatibility and source evidence](docs/compatibility.md).

| Agent | Minimum verified release | Authentication |
| --- | --- | --- |
| Codex | 0.128.0 | Request-driven credential command |
| Claude Code | 2.0.45 | Native Foundry Azure credential chain |
| Hermes | 0.15.0 / release 2026.5.28 | Native `azure-foundry` Entra provider |
| OpenCode | 1.18.25 | Native Azure CLI integration, with `/connect` |

Older, unparseable and prerelease versions are refused on installation.

## Install

From this checkout:

```sh
pipx install .
# Or use an isolated environment:
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/foundryconnect --help
```

The distribution is `foundryconnect-cli`; the executable is `foundryconnect`.
The examples below assume its executable is on `PATH`. Keep the installation
in place while Codex is configured: its credential command uses the absolute
Python interpreter path from this installation.

## Quick start

Use an existing resource and deployment. Your administrator must grant the
appropriate inference/data-plane role; this tool does not provision resources,
assign roles or require management-plane deployment listing.

```sh
# Read-only account check, also detects installed agents:
foundryconnect login

# Explicitly authorize interactive Azure CLI sign-in and account selection:
foundryconnect login --sign-in \
  --tenant 11111111-1111-1111-1111-111111111111 \
  --subscription 22222222-2222-2222-2222-222222222222

foundryconnect profile add work \
  --endpoint https://YOUR-RESOURCE.openai.azure.com \
  --deployment YOUR-RESPONSES-DEPLOYMENT \
  --tenant 11111111-1111-1111-1111-111111111111 \
  --subscription 22222222-2222-2222-2222-222222222222 \
  --protocol responses

foundryconnect codex on --profile work --dry-run
foundryconnect codex on --profile work
# In noninteractive scripts, review --dry-run first, then pass --yes.

foundryconnect codex status
foundryconnect doctor --profile work --agent codex

# Explicit approval of one small, potentially billable inference request:
foundryconnect doctor --profile work --agent codex --smoke-test
```

Start a new agent session after changing providers. Renewal then happens during
model requests; routine access-token expiry does not require restarting.
Conditional-access policy, revoked access or an expired Azure login session
can still require `az login`. Token acquisition is not proof of inference
authorization or model compatibility.

## Other agents

Hermes can use `responses`, `chat`, or `anthropic` profiles:

```sh
foundryconnect hermes on --profile work --dry-run
foundryconnect hermes on --profile work
```

Hermes must have `azure-identity` installed **in its own Python environment**.
FoundryConnect does not change agent dependencies.

OpenCode supports Responses and Chat Completions in this MVP:

```sh
foundryconnect opencode on --profile work
```

Then open OpenCode and use **`/connect` -> Azure -> Microsoft Entra ID
(Azure CLI)**. Enter the same resource name. This native connection step is
required: provider JSON alone does not enable renewable authentication.
FoundryConnect never creates or rewrites OpenCode's `auth.json`. `status`
reports whether an Azure OAuth entry exists, not that a running agent has
successfully used it.

Claude Code requires a separate Anthropic-compatible deployment/profile:

```sh
foundryconnect profile add claude-work \
  --endpoint https://YOUR-RESOURCE.services.ai.azure.com \
  --deployment YOUR-CLAUDE-DEPLOYMENT \
  --tenant 11111111-1111-1111-1111-111111111111 \
  --subscription 22222222-2222-2222-2222-222222222222 \
  --protocol anthropic
foundryconnect claude on --profile claude-work
foundryconnect doctor --profile claude-work --agent claude --smoke-test
```

All Claude model aliases, including Haiku, initially point to the one supplied
deployment so an undeployed default model is not selected. This may make small
tasks more expensive than a separately deployed Haiku model.

## Commands

| Command | Behavior |
| --- | --- |
| `login [--profile NAME]` | Check active Azure CLI tenant/subscription; does not sign in |
| `login --sign-in --profile NAME [--device-code]` | Explicit sign-in and subscription selection |
| `profile add NAME --endpoint URL --deployment NAME --tenant UUID --subscription UUID --protocol responses\|chat\|anthropic` | Store a credential-free profile |
| `profile list` | List endpoints, deployments and account identifiers, never credentials |
| `<agent> on [--profile NAME] [--dry-run] [--yes]` | Preview/install renewable auth, with real version/auth preflight before writes |
| `<agent> status` | Inspect ownership, drift, version, credential conflicts and current Azure auth |
| `<agent> off [--dry-run] [--yes]` | Restore/remove owned settings without logging out or revoking tokens |
| `doctor [--profile NAME] --agent AGENT [--smoke-test]` | Separate local configuration, account, token and opt-in inference diagnosis |
| `auth token --profile NAME` | Internal helper: **raw sensitive bearer token only on stdout**, errors on stderr |

`<agent>` is `codex`, `claude`, `hermes` or `opencode`. A profile can be omitted
when exactly one exists. There is no implicit sign-in, inference, deployment
discovery or API-key fallback. `--dry-run` does not write files or acquire
credentials; it does not certify the installed agent version.

Exit code `0` means the command completed, `1` means an error or incomplete
readiness, `2` means invalid CLI arguments, and `130` means interruption.
For OpenCode, `on` succeeds when configuration is installed but explicitly
reports the remaining native-login step; `status`/`doctor` return `1` until
an Azure OAuth entry exists. Inference is never inferred from installation.

## Safe configuration and removal

Profiles and ownership receipts are stored under
`$XDG_CONFIG_HOME/foundryconnect` (default `~/.config/foundryconnect`).
`FOUNDRYCONNECT_HOME` overrides this location. Profiles contain no tokens.
Receipts include the original configuration for exact restoration; that
original file **may already contain unrelated secrets**, so treat receipts as
sensitive. New configuration, profile and receipt files are mode `0600`.

Configuration uses the agent's normal user-level location and supported home
overrides. JSON, TOML and YAML changes are scoped to owned settings. TOML/YAML
comments are retained; plain JSON formatting may change. JSONC and YAML
anchors/aliases are conservatively refused rather than destructively rewritten.
File symlinks are refused. Writes use atomic replacement, a private journal
and a lock between FoundryConnect processes.

```sh
foundryconnect codex off --dry-run
foundryconnect codex off --yes
```

If the file is unchanged since installation, `off` restores the original bytes
(or deletes the newly created file). Otherwise it restores only owned settings,
preserving unrelated edits. Changes to owned values cause a conflict instead
of overwriting the user's work. Resolve the reported owned-value conflicts
before retrying. Keep the receipt until removal is complete. Run `off` before
switching an agent to another profile.

Previews withhold old values, even if they are not secrets. FoundryConnect
does not print Azure CLI error bodies or inference response bodies. The
internal `auth token` command is the sole intentional token-output interface;
do not run it in a recorded terminal or redirect it to a config file.

## Scope and limitations

- Only public Azure resource endpoints are accepted: resource roots,
  `/openai/v1` or `/anthropic`. Sovereign clouds, custom gateways, project
  endpoints and protocol translation are not part of this MVP.
- Protocol selection is explicit. A URL does not prove that a particular
  deployment supports Responses, Chat Completions or Anthropic Messages.
- Native clients use the **active Azure CLI context**. `on`, `status` and
  `doctor` compare tenant and subscription. Changing that context later can
  affect active native sessions; simultaneous multi-tenant use is not promised.
- Claude/Hermes use a native Azure credential chain, which can select another
  installed identity source. Detected static credentials and conflicting
  environment settings are rejected, but another process's environment,
  project-level configuration and command-line overrides are not controlled.
  Launch without conflicting overrides. Managed/workload identity is not certified.
- Codex invokes the helper directly, without a shell, with a 60-second
  request-driven refresh interval and 30-second timeout. Each helper invocation
  asks Azure CLI for a token pinned to the profile's tenant/subscription.
  Tokens with less than two minutes left are refused, not written to disk.
- No agent process is launched/restarted or patched by this tool. An opt-in
  smoke test calls the endpoint directly; it is not an end-to-end agent-session
  or token-expiry certification.
- The initial CLI uses explicit profile flags rather than an interactive
  provisioning wizard. Deployment discovery is intentionally omitted so
  management-plane permissions are never required.

Doctor errors distinguish `authentication` (401/token/context failures),
`authorization` (403/RBAC or resource network policy), `endpoint`
(URL/DNS/TLS/routes), `protocol` (request/response mismatch), `quota` (429),
`service`, `dependency`, and `configuration`/compatibility failures. A 404 can
mean either a missing deployment or an unavailable protocol route; a 400 can
mean unsupported request parameters. It does not overclaim a unique cause.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary homes and mocked Azure/inference calls; they do not use
real organizational credentials or make billable inference requests.
The source [PRD](PRD.md) currently ends at section 7.1; this MVP implements its
available command, safety and renewal requirements, with release evidence and
deployment-certification limits recorded explicitly.

## Acknowledgments

FoundryConnect was inspired by [FireConnect](https://github.com/fw-ai/fireconnect),
which connects coding agents to Fireworks AI and is licensed under
[Apache License 2.0](https://github.com/fw-ai/fireconnect/blob/main/LICENSE).
Thanks to the FireConnect contributors for the original project and approach.
