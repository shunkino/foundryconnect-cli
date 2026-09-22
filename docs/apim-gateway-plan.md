# Planning: APIM as a gateway in front of Foundry

**Status:** Planning only. No implementation is authorized by this document.
**Sequencing:** Phase 1 (direct Foundry connection) ships first and stays the
default. APIM is Phase 2 and must not regress or complicate the direct path.

Evidence tags used below: **[V]** verified against a primary source (Learn doc,
tagged repo file, agent source), **[V-2nd]** verified only via a secondary or
community source, **[INF]** inferred, **[?]** open question requiring a live
APIM instance.

---

## 1. Why this is not a small change

The current MVP is built on two assumptions that APIM invalidates:

1. **The hostname identifies the service, the audience and the protocol.**
   `profiles.validate` accepts only
   `<resource>.openai.azure.com` or `<resource>.services.ai.azure.com`
   (`src/foundryconnect/profiles.py:41-44`), and `adapters._validate` re-checks
   the same pattern plus an exact path of `/openai/v1` or `/anthropic`
   (`src/foundryconnect/adapters.py:95-104`). `scope_for` picks the Entra
   audience from the hostname suffix (`adapters.py:107-114`). An APIM host is
   `<name>.azure-api.net` or a custom domain, with an operator-chosen API path
   prefix, and its audience is unrelated to the backend's.

2. **Every agent's native Foundry integration can be used.** The MVP's
   integration preference order (PRD §5.2) puts native Entra providers first,
   and all four adapters currently use them. Two of those native providers are
   **hostname-coupled** and will silently mis-authenticate against APIM
   (§4 below). APIM therefore forces a partial retreat to preference level 2
   (command-backed credential providers) — which is machinery FoundryConnect
   already owns for Codex (`adapters.py:152-173`) and can generalize.

There is also a direct policy collision: `check_conflicts` currently treats
`ocp-apim-subscription-key` and `subscription-key` as *forbidden competing
credentials* (`adapters.py:57-66`), and rejects any provider `http_headers`,
`ANTHROPIC_CUSTOM_HEADERS`, OpenCode `options.headers` or Hermes
`extra_headers`. APIM support means FoundryConnect must **own** some of those
keys rather than refuse them.

---

## 2. What APIM does and does not change on the wire

### 2.1 It is a pass-through proxy by default [V]

`forward-request` is applied at global scope by default; removing it stops
forwarding entirely. APIM does not rewrite bodies unless a policy is configured
to (`set-body`, `llm-semantic-cache-*`, `llm-content-safety`, `validate-content`).
So the user's assumption that "APIM will change the messaging format" is
**mostly wrong for the body, and mostly right for the URL and the credential.**

Practical conclusion: **the wire protocol dimension of a profile (`responses` /
`chat` / `anthropic`) survives APIM unchanged. The endpoint and auth dimensions
do not.** That is the shape the data model should follow.

### 2.2 The URL shape is preserved by the canonical patterns [V]

Microsoft's "Import a Microsoft Foundry API" flow offers three client
compatibility modes, and the v1 surface FoundryConnect already targets is one
of them:

| Mode | Client-visible path |
| --- | --- |
| Azure OpenAI v1 | `<prefix>/openai/v1/responses`, `.../chat/completions` |
| Azure OpenAI (classic) | `<prefix>/openai/deployments/<dep>/chat/completions?api-version=` |
| Azure AI Model Inference | `<prefix>/<model>/models/chat/completions` |

`Azure-Samples/AI-Gateway` models this as
`inferenceAPIType ∈ {AzureOpenAIV1, AzureOpenAI, AzureAI, OpenAI, PassThrough}`
with an `AIFoundryOpenAIV1.json` spec containing `/responses`,
`/chat/completions`, `/embeddings`. So an APIM-fronted profile is, in the good
case, *the existing normalized endpoint with a different origin and an extra
path prefix*.

Counter-example worth guarding against: `Azure/aoai-apim` exposes only
`/openai/chat/completions` and drops `/deployments/{id}/`, i.e. it is **not**
SDK-shape-compatible. We cannot assume a customer's APIM preserves any shape —
this must be a declared property of the profile, not inferred.

### 2.3 Streaming is the highest operational risk [V + ?]

- Consumption tier **cannot** do long-lived SSE at all; classic and v2 can.
- A 240 s idle timeout is enforced by the underlying Azure Load Balancer, not
  configurable in APIM. `forward-request timeout=` values above 240 s "may not
  be honored".
- Correct pass-through requires `<forward-request buffer-response="false"/>`,
  and `validate-content` / body-logging diagnostics must not be enabled on the
  API.
- **[?]** A Microsoft Q&A report (question 5992130) describes APIM buffering an
  entire Azure OpenAI SSE stream *despite* `buffer-response="false"`,
  unresolved at time of writing.

For a coding agent this is not cosmetic: buffered SSE means no token streaming
and a hard 4-minute cap on any single long reasoning turn. **`doctor` must be
able to detect this, and the docs must state it as a gateway-operator
requirement, not a FoundryConnect guarantee.**

### 2.4 Token accounting degrades under streaming [V]

`llm-token-limit` always *estimates* prompt and completion tokens when
`stream: true`, and concurrent requests can temporarily exceed the configured
limit. Relevant only as user-facing guidance: a gateway-enforced TPM limit will
behave approximately for interactive agent sessions.

### 2.5 Unverified [?]

Payload/buffer size limits per tier; gzip/`Content-Encoding` pass-through;
whether any `openai-*` / `anthropic-*` / `x-ms-*` response headers are stripped;
whether tool-calling JSON survives a bare pass-through API untouched (strongly
implied by AI-Gateway's `labs/access-controlling` stock-SDK assertions, not
stated).

---

## 3. Authentication: the actual design problem

Microsoft documents exactly two sanctioned patterns, and explicitly frames them
as layered rather than alternative [V]:

**(A) Backend auth by APIM's managed identity.** APIM holds the Cognitive
Services OpenAI User role and mints the backend token itself:

```xml
<authentication-managed-identity resource="https://cognitiveservices.azure.com"
    output-token-variable-name="managed-id-access-token" ignore-error="false" />
<set-header name="Authorization" exists-action="override">
    <value>@("Bearer " + (string)context.Variables["managed-id-access-token"])</value>
</set-header>
```

Note this **overrides** the client's `Authorization` header. Any client-side
Entra bearer token is discarded at the gateway.

**(B) Client auth at APIM**, which is either a subscription key or an Entra
token validated by `validate-azure-ad-token` for an audience that belongs to
the *gateway's* app registration, not to Cognitive Services.

Microsoft's own wording: OAuth 2.0 at APIM is *"part of a defense-in-depth
strategy. It's not a replacement for API key authentication or managed identity
authentication to an Azure OpenAI API."*

### 3.1 Candidate client modes, ranked for this product

| Mode | Client sends | FoundryConnect verdict |
| --- | --- | --- |
| `apim-entra` | Entra token for a custom APIM audience (`api://<app-id>/.default`), APIM validates, managed identity calls backend | **Preferred.** Keeps the product promise: renewable org identity, no stored secret. |
| `apim-subscription-key` | `Ocp-Apim-Subscription-Key` (or a renamed header, often `api-key`) | **Supported but demoted.** It is a long-lived shared secret — precisely what the PRD exists to eliminate (PRD §1, §3). |
| `apim-entra-passthrough` | Cognitive Services/AI-audience token forwarded to backend unchanged | **Out of scope.** Undocumented; requires the *client* to hold backend RBAC, defeating the point of the gateway. **[INF/?]** |
| mTLS / Credential Manager | client certificate / APIM-managed OAuth | Out of scope for Phase 2. Credential Manager targets third-party SaaS backends, not Foundry. |

### 3.2 `apim-entra` is a good fit for existing machinery

`az account get-access-token --resource api://<app-id>` is a real capability
**[V-2nd]**, and `auth.token()` already takes an arbitrary `scope` argument
(`src/foundryconnect/auth.py:52`) and already verifies the issued tenant.
The only change needed there is that the audience becomes a **profile field**
instead of being derived from the hostname by `scope_for`.

Caveats to document, not solve:
- Requires an app registration with "Expose an API" / App Roles, admin consent,
  and the Azure CLI first-party client ID pre-authorized. **[V-2nd / ?]**
- `Azure-Samples/AI-Gateway:labs/access-controlling` actually validates via
  `<client-application-ids>` against a Graph-audience (`User.Read`) token from a
  device-code flow — the custom-audience/App-Role variant is documented in the
  same notebook as the more granular option, but is *not* the demonstrated
  default. **[V]** So `apim-entra` must accept an operator-supplied audience
  string verbatim and must not guess one.

### 3.3 The subscription-key secret-handling problem

If `apim-subscription-key` is supported, FoundryConnect must decide where the
key lives. Writing it into `~/.claude/settings.json` or `opencode.json` directly
contradicts PRD §2.5 ("avoid writing tokens into agent configuration files").

Proposed rule: **never write the key into an agent config file.** Store it in
the OS keychain or a `0600` file under the profile home, and inject it at
request time:

| Agent | Mechanism | Note |
| --- | --- | --- |
| Codex | `env_http_headers` (env-var-sourced, not literal) [V] | Clean. Requires the variable be exported in the agent's environment. |
| Hermes | `key_cmd` — run per request, cached until near expiry [V] | Clean; can shell out to `foundryconnect auth token`. |
| Claude Code | `apiKeyHelper` [V] | Clean for `ANTHROPIC_AUTH_TOKEN`-style credentials. `ANTHROPIC_CUSTOM_HEADERS` is literal-only — unsuitable for the key. |
| OpenCode | `provider.<id>.options.headers` — literal [V] | **No dynamic option in config.** Worst case for both modes. |

This table is the single most consequential finding for implementation: the
same three agents that support a credential *command* are the three where
`apim-entra` works cleanly, and OpenCode is the outlier in both modes.

---

## 4. Per-agent impact of inserting a gateway

Common good news: **all four agents put the model name in the request body**,
so one APIM endpoint can serve many deployments without one config entry per
model. Codex additionally supports `profiles.<name>.model` sharing a single
provider entry [V].

### 4.1 Codex — lowest risk

Already fully generic: `base_url` + `wire_api = "responses"` + `auth.command`
(`adapters.py:152-173`). Path join is `base_url + "/responses"`, no deployment
or `api-version` synthesis [V]. For APIM:

- `base_url` becomes `https://<apim-host>/<prefix>/openai/v1`.
- `apim-entra`: reuse the existing `auth.command` helper, pass a new
  `--audience` (or have the helper read the audience from the profile).
- `apim-subscription-key`: add `[model_providers.x.env_http_headers]`.
- `check_conflicts` must stop unconditionally rejecting `http_headers` /
  `env_http_headers` (`adapters.py:325-330`) and instead reject only
  *unowned* keys.

### 4.2 Claude Code — mode switch required

The native path (`CLAUDE_CODE_USE_FOUNDRY=1` + `ANTHROPIC_FOUNDRY_BASE_URL`)
supplies a `DefaultAzureCredential` bearer for
`https://cognitiveservices.azure.com/.default` (`docs/compatibility.md`,
"Claude Code"). Against APIM that token is either rejected by
`validate-azure-ad-token` (wrong audience) or discarded by
`authentication-managed-identity`. **The native Foundry mode is unusable for
`apim-entra` unless the gateway is configured to accept the Cognitive Services
audience**, which is the out-of-scope passthrough mode.

Therefore APIM profiles must use the **generic Anthropic path** instead:

```json
{ "env": {
    "ANTHROPIC_BASE_URL": "https://<apim-host>/<prefix>",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "<alias>",
    "ANTHROPIC_DEFAULT_OPUS_MODEL":   "<alias>",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL":  "<alias>" },
  "apiKeyHelper": "<abs-path> auth token --profile NAME --home ..." }
```

with `CLAUDE_CODE_USE_FOUNDRY` explicitly **unset/0**. Note `apiKeyHelper` is
currently in the *conflict* list (`adapters.py:340-343`) and would become an
owned key for APIM profiles.

Encouraging context [V]: Claude-on-Foundry is Anthropic-wire-native
(`https://{resource}.services.ai.azure.com/anthropic/v1/*`), and APIM's GenAI
gateway explicitly supports the Anthropic Messages API schema **in v2 tiers**.
So no translation proxy is required — but **[?]** no end-to-end public example
of `ANTHROPIC_BASE_URL` pointed at APIM was found. This needs live testing.

### 4.3 OpenCode — hardest, may be deferred

The native `azure` provider is hostname-coupled in three separate places:

- the plugin picks the Entra scope by hostname (`*.services.ai.azure.com`
  outside `/models` → `ai.azure.com`, else `cognitiveservices.azure.com`)
  (`docs/compatibility.md`, "OpenCode");
- the current adapter derives `options.resourceName` from
  `hostname.split(".")[0]` (`adapters.py:211`) — meaningless for `azure-api.net`;
- the `/connect` flow selects an *Azure resource*, not a URL.

`@ai-sdk/azure` also owns path construction: by default `{baseURL}/v1{path}`,
or `{baseURL}/deployments/{modelId}{path}?api-version=` when
`useDeploymentBasedUrls` is true [V] — the latter being the only place any
agent synthesizes a deployment path.

Realistic options, in order of preference:
1. **Generic OpenAI-compatible provider** (`@ai-sdk/openai-compatible` or the
   `openai` provider with a custom `baseURL`) plus literal
   `options.headers`. Loses the native token refresh — so it pairs naturally
   with `apim-subscription-key`, not `apim-entra`.
2. **Defer OpenCode to Phase 2b** and report it as unsupported-for-APIM with a
   clear reason, consistent with PRD §5.2 step 4 ("report unsupported").

Option 2 is the honest default. Option 1 should only ship if a custom `fetch` or
plugin hook is confirmed to allow a per-request token — note the current
conflict checker already refuses `options.fetch` (`adapters.py:378`).

### 4.4 Hermes — flexible, second-lowest risk

Supports `extra_headers` / `default_headers` at provider scope and `key_cmd`
run per request with expiry-aware caching [V]. Two viable shapes: keep
`provider: azure-foundry` with an overridden `base_url` and an explicit
`entra.scope` set to the APIM audience, or define a generic custom provider
with `base_url` + `key_cmd`. The latter is more predictable against a gateway;
the former reuses the already-verified `auth_mode: entra_id` path but **[?]**
it is unverified whether the native provider tolerates a non-Azure hostname.

---

## 5. Multi-model routing, and the discovery opportunity

The stored requirement that model aliases must map to multiple deployments
while preserving a simple single-deployment fallback is **directly served by
APIM**, in three escalating patterns [V]:

1. One API per deployment (portal default).
2. One API, deployment in path (`/deployments/{deployment-id}/...`).
3. **Body-based model routing** — Microsoft's own
   `Azure-Samples/AI-Gateway:labs/model-routing/policy.xml` reads
   `context.Request.Body.As<JObject>()["model"]`, falls back to the
   `deployment-id` route parameter, and dispatches with `set-backend-service`.
   It co-routes GPT-5, GPT-4.1 and DeepSeek-R1 behind one host.

Plus backend pools (round-robin / weighted / priority, ≤30 backends, circuit
breakers with `acceptRetryAfter` for Azure OpenAI's very long 429 `Retry-After`).

### 5.1 Unified Model API is the strategic target [V, preview]

APIM's **Unified Model API** (preview; classic tiers need the AI Gateway early
release channel) normalizes everything into OpenAI-shaped, model-in-body calls:

```python
client = OpenAI(base_url="https://<apim>.azure-api.net/llm/v1",
                api_key="<subscription-key>")
client.chat.completions.create(model="claude-sonnet", messages=[...])
```

It translates between OpenAI Chat Completions and Anthropic Messages backends,
exposes provider-neutral **model aliases**, and auto-provisions a `/models`
discovery endpoint.

This is the cleanest possible target for a multi-agent config helper:
one host, one credential, N aliases, and a **data-plane** model list that needs
only a subscription key — no ARM/control-plane RBAC over the APIM resource.
`foundryconnect profile discover` should prefer this over
`Microsoft.ApiManagement/service/apis` ARM enumeration.

Caveats: preview; `/llm/v1` is *not* `/openai/v1` (so `chat` protocol, not
`responses`); and the translation layer means the "APIM doesn't change the
body" claim in §2.1 no longer holds in this mode. **[?]** Whether Responses-API
traffic (Codex's only wire protocol) is supported by Unified Model API is
unconfirmed and is a gating question for Codex + Unified Model API.

### 5.2 Discovery fallbacks

- `/openai/v1/models` returns `id` = deployment name — ideal for config
  generation **[V-2nd]**; **[?]** whether typical APIM templates proxy it.
- Legacy `/openai/models?api-version=` mixes catalog and deployed models.
- ARM `apis?api-version=2025-09-01-preview` enumerates logical APIs but needs
  control-plane RBAC — reject as the primary mechanism.
- **[?]** No prior art was found anywhere for "read an APIM instance and
  generate coding-agent config files". This is the genuinely novel part of the
  product.

---

## 6. Proposed shape of the change (for later design review)

### 6.1 Data model

Split the single `endpoint` string into explicit, independent dimensions and
make the gateway a first-class, opt-in profile kind:

```
Profile:
  name, tenant, subscription          # unchanged
  kind: "foundry" | "apim"            # new; defaults to "foundry"
  endpoint                            # validated per kind
  protocol: responses | chat | anthropic   # unchanged; survives pass-through
  deployment                          # direct: deployment name
  models: {alias -> deployment}       # apim: optional alias map, empty = fallback
  auth:
    mode: "entra-cli"                 # foundry (today's behaviour)
        | "apim-entra"                # audience-scoped Entra token
        | "apim-subscription-key"
    audience                          # explicit; replaces hostname-derived scope_for
    key_header                        # default Ocp-Apim-Subscription-Key; may be api-key
    key_ref                           # keychain/file reference, never a literal
```

Invariant to preserve: **a `kind: "foundry"` profile must produce byte-identical
plans to today's.** Phase 2 should land with the existing adapter tests
unchanged.

### 6.2 Validation changes

- `profiles.validate` / `adapters._validate`: keep the strict Azure hostname and
  exact-path rules for `kind: foundry`; for `kind: apim` accept any HTTPS host
  with no credentials/port/query/fragment and an arbitrary normalized path
  prefix. Still reject `http://`, userinfo, ports, query and fragment.
- `scope_for` becomes audience lookup on the profile for APIM profiles, keeping
  the current hostname-based derivation only for `kind: foundry`.
- `check_conflicts` moves from "these keys are always forbidden" to "these keys
  are forbidden unless owned by this plan" for `http_headers`,
  `env_http_headers`, `ANTHROPIC_CUSTOM_HEADERS`, `apiKeyHelper`,
  `extra_headers`, `options.headers` and the `*-subscription-key` names.

### 6.3 Diagnostics (`doctor`) additions

A gateway is an extra failure surface, and the MVP already commits to
distinguishing failure categories (PRD §2.8). New categories worth separating:

- gateway reachable but returns 401/403 → distinguish *APIM rejected the client*
  (subscription key / JWT audience) from *backend rejected APIM* (managed
  identity lacks Cognitive Services OpenAI User).
- 404 → API path prefix wrong, or the gateway does not preserve the expected
  URL shape (the `Azure/aoai-apim` failure mode).
- 429 with a very large `Retry-After` → gateway lacks circuit-breaker config.
- **streaming probe**: issue one `stream: true` request and assert the first
  chunk arrives well before completion. This is the only way to catch the
  buffered-SSE regression, and is worth doing even though it costs one billable
  request (reuse the existing explicit `--smoke-test` opt-in).

### 6.4 Documentation obligations

`docs/compatibility.md` is currently honest that configuration ≠ verified
inference. APIM needs the same discipline plus an explicit statement that
**the gateway's policy configuration is the operator's responsibility**:
tier (not Consumption), `buffer-response="false"`, no `validate-content` on
streaming APIs, URL-shape preservation, and the backend managed-identity role
assignment. FoundryConnect configures clients; it does not configure APIM and
must not imply that it validates the gateway's policy set.

---

## 7. Recommended phasing

| Phase | Scope |
| --- | --- |
| **1** (current) | Direct Foundry. No APIM concepts leak into the data model beyond reserving `kind`. |
| **2a** | `kind: apim` + `apim-entra` for **Codex and Hermes only** — the two agents with clean command-backed credentials and no hostname coupling. Manual endpoint/audience entry, no discovery. |
| **2b** | Claude Code via generic `ANTHROPIC_BASE_URL` + `apiKeyHelper`, with `CLAUDE_CODE_USE_FOUNDRY` explicitly disabled for APIM profiles. |
| **2c** | `apim-subscription-key` with keychain storage and per-agent injection; unblocks OpenCode via a generic OpenAI-compatible provider, or OpenCode is declared unsupported-for-APIM. |
| **3** | Discovery: `foundryconnect profile discover` against Unified Model API `/models` or `/openai/v1/models`, generating an alias map across agents. |

Gate on 2a: the live tests in §8 must pass before 2b/2c are designed in detail.

---

## 8. Must-test-before-designing list

Ordered by how much of the above they invalidate.

1. **SSE pass-through.** Does a streamed Responses/Messages call through APIM
   deliver incremental chunks with `buffer-response="false"`, on the target
   tier and region? If not, APIM support is not viable for interactive agents.
2. **Audience.** `ai.azure.com` vs `cognitiveservices.azure.com` — Microsoft's
   Foundry managed-identity doc and the APIM AI-auth doc disagree. Determine
   empirically for the target resource and API version.
3. **`az account get-access-token --resource api://<app-id>`** against a live
   APIM-fronted API, including admin consent and Azure CLI client
   pre-authorization. No first-party example combines these.
4. **Claude Code against `ANTHROPIC_BASE_URL` on APIM** — no public end-to-end
   demonstration exists.
5. **Header integrity** for tool calling and Anthropic `/v1/messages` through a
   bare pass-through API.
6. Whether Hermes's native `azure-foundry` provider tolerates a non-Azure
   hostname, or whether a generic custom provider is required.
7. Whether Unified Model API supports the **Responses** API (gates Codex).
8. Whether typical APIM templates proxy `/openai/v1/models` (gates discovery).
9. Per-tier payload/buffer size limits and gzip pass-through.
10. Whether the Foundry Agents/Projects surface
    (`services.ai.azure.com/api/projects/...`) can be fronted at all — fetch
    `learn.microsoft.com/en-us/azure/foundry/configuration/enable-ai-api-management-gateway-portal`.

---

## 9. Primary sources

**APIM GenAI gateway**
- https://learn.microsoft.com/en-us/azure/api-management/genai-gateway-capabilities
- https://learn.microsoft.com/en-us/azure/api-management/azure-ai-foundry-api
- https://learn.microsoft.com/en-us/azure/api-management/unified-model-api
- https://learn.microsoft.com/en-us/azure/api-management/llm-token-limit-policy
- https://learn.microsoft.com/en-us/azure/api-management/backends
- https://learn.microsoft.com/en-us/azure/api-management/how-to-server-sent-events
- https://learn.microsoft.com/en-us/azure/api-management/forward-request-policy
- https://learn.microsoft.com/en-us/azure/api-management/api-management-authenticate-authorize-ai-apis
- https://learn.microsoft.com/en-us/azure/api-management/api-management-subscriptions

**Reference implementations**
- https://github.com/Azure-Samples/AI-Gateway — `labs/model-routing/policy.xml`,
  `labs/backend-pool-load-balancing/policy.xml`, `labs/access-controlling/`,
  `modules/apim/v3/inference-api.bicep`
- https://github.com/Azure-Samples/ai-hub-gateway-solution-accelerator —
  `bicep/infra/modules/apim/policies/`
- https://github.com/Azure/apim-landing-zone-accelerator — `scenarios/workload-genai/`
- https://github.com/Azure/aoai-apim — counter-example, non-SDK-shape
- https://github.com/Azure-Samples/openai-apim-lb — archived, superseded

**Foundry / Claude**
- https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/claude-models
- https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-claude-code
- https://platform.claude.com/docs/en/build-with-claude/claude-in-microsoft-foundry
- https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/managed-identity

**Agent sources**
- Codex `codex-rs/model-provider-info/src/lib.rs` (`http_headers`,
  `env_http_headers`, `ModelProviderAuthInfo`)
- `vercel/ai` `packages/azure/src/azure-openai-provider.ts`
  (`useDeploymentBasedUrls`, `headers`, `tokenProvider`)
- `NousResearch/hermes-agent` `cli-config.yaml.example` (`key_cmd`,
  `extra_headers`), `website/docs/guides/azure-foundry.md`
- https://code.claude.com/docs/en/settings-reference (`apiKeyHelper`,
  `ANTHROPIC_CUSTOM_HEADERS`)

**Comparison gateways**
- https://docs.litellm.ai/docs/proxy/configs
- https://docs.portkey.ai/docs/product/ai-gateway/universal-api
