import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from foundryconnect.adapters import (
    MIN_VERSIONS, check_conflicts, check_version, make_plan, native_login_status, scope_for,
)
from foundryconnect.models import AI_SCOPE, COGNITIVE_SCOPE, FoundryError, Profile


def profile(protocol="responses", host="demo.openai.azure.com"):
    if protocol == "anthropic":
        host = "demo.services.ai.azure.com"
    path = "/anthropic" if protocol == "anthropic" else "/openai/v1"
    return Profile("work", "https://" + host + path, "my-deployment", "tenant", "subscription", protocol)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.home = Path.cwd() / ".adapter-test-home"
        environment = patch.dict(os.environ, {"HOME": str(self.home)}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_codex_exact_helper_and_no_writes(self):
        with patch("pathlib.Path.mkdir") as mkdir, patch("pathlib.Path.write_text") as write:
            plan = make_plan("codex", profile())
        mkdir.assert_not_called()
        write.assert_not_called()
        self.assertEqual(plan.path, self.home / ".codex/config.toml")
        self.assertEqual(plan.format, "toml")
        p = ("model_providers", "foundryconnect")
        self.assertEqual(plan.changes[p + ("wire_api",)], "responses")
        self.assertIs(plan.changes[p + ("requires_openai_auth",)], False)
        self.assertIs(plan.changes[p + ("supports_websockets",)], False)
        self.assertEqual(plan.changes[p + ("auth", "command")], sys.executable)
        self.assertEqual(plan.changes[p + ("auth", "args")], [
            "-m", "foundryconnect", "auth", "token", "--profile", "work",
            "--home", str(self.home / ".config/foundryconnect"),
        ])
        self.assertEqual(plan.changes[p + ("auth", "timeout_ms")], 30000)
        self.assertEqual(plan.changes[p + ("auth", "refresh_interval_ms")], 60000)
        self.assertFalse(any("api_key" in path or "env_key" in path for path in plan.changes))

    def test_home_overrides_and_pinned_helper_root(self):
        values = {"CODEX_HOME": "relative/codex", "CLAUDE_CONFIG_DIR": "relative/claude",
                  "HERMES_HOME": "relative/hermes", "XDG_CONFIG_HOME": "relative/xdg",
                  "FOUNDRYCONNECT_HOME": "relative/profiles"}
        with patch.dict(os.environ, values):
            for agent, directory, file in [
                ("codex", "codex", "config.toml"), ("claude", "claude", "settings.json"),
                ("hermes", "hermes", "config.yaml"), ("opencode", "xdg/opencode", "opencode.json"),
            ]:
                p = profile("anthropic" if agent == "claude" else "responses")
                self.assertEqual(make_plan(agent, p).path, Path.cwd() / "relative" / directory / file)
            args = make_plan("codex", profile()).changes[
                ("model_providers", "foundryconnect", "auth", "args")]
            self.assertEqual(args[-1], str(Path.cwd() / "relative/profiles"))
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/xdg"}):
            args = make_plan("codex", profile()).changes[
                ("model_providers", "foundryconnect", "auth", "args")]
            self.assertEqual(args[-1], str(Path.cwd() / "relative/xdg/foundryconnect"))

    def test_protocol_matrix(self):
        allowed = {"codex": {"responses"}, "claude": {"anthropic"},
                   "hermes": {"responses", "chat", "anthropic"}, "opencode": {"responses", "chat"}}
        for agent in allowed:
            for protocol in ("responses", "chat", "anthropic"):
                with self.subTest(agent=agent, protocol=protocol):
                    if protocol in allowed[agent]:
                        self.assertTrue(make_plan(agent, profile(protocol)).path.is_absolute())
                    else:
                        with self.assertRaises(FoundryError) as error:
                            make_plan(agent, profile(protocol))
                        self.assertEqual(error.exception.category, "compatibility")

    def test_endpoint_validation(self):
        for endpoint in (
            "http://demo.openai.azure.com/openai/v1",
            "https://demo.openai.azure.com.attacker.test/openai/v1",
            "https://demo.openai.azure.us/openai/v1",
            "https://user@demo.openai.azure.com/openai/v1",
            "https://demo.openai.azure.com:8443/openai/v1",
            "https://demo.openai.azure.com/openai/v1?api-key=secret",
            "https://demo.openai.azure.com/openai/v1#fragment",
            "https://demo.openai.azure.com/openai/v1/responses",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(FoundryError):
                make_plan("codex", Profile("work", endpoint, "dep", "t", "s", "responses"))
        with self.assertRaises(FoundryError):
            make_plan("claude", Profile("work", "https://demo.openai.azure.com/anthropic",
                                       "dep", "t", "s", "anthropic"))

    def test_native_schemas_and_audiences(self):
        claude = make_plan("claude", profile("anthropic"))
        self.assertEqual(claude.changes[("env", "CLAUDE_CODE_USE_FOUNDRY")], "1")
        for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                     "ANTHROPIC_SMALL_FAST_MODEL"):
            self.assertEqual(claude.changes[("env", name)], "my-deployment")
        self.assertEqual(scope_for("claude", profile("anthropic")), COGNITIVE_SCOPE)
        for protocol, mode in (("responses", "codex_responses"), ("chat", "chat_completions"),
                               ("anthropic", "anthropic_messages")):
            hermes = make_plan("hermes", profile(protocol))
            self.assertEqual(hermes.changes[("model", "provider")], "azure-foundry")
            self.assertEqual(hermes.changes[("model", "auth_mode")], "entra_id")
            self.assertEqual(hermes.changes[("model", "api_mode")], mode)
            self.assertEqual(hermes.changes[("model", "entra", "scope")], AI_SCOPE)
        for agent in ("codex", "opencode"):
            self.assertEqual(scope_for(agent, profile()), COGNITIVE_SCOPE)
            self.assertEqual(scope_for(agent, profile(host="demo.services.ai.azure.com")), AI_SCOPE)

    def test_opencode_deployment_and_explicit_protocol_selection(self):
        for protocol in ("responses", "chat"):
            plan = make_plan("opencode", profile(protocol))
            self.assertEqual(plan.changes[("model",)], "azure/my-deployment")
            self.assertEqual(plan.changes[("small_model",)], "azure/my-deployment")
            self.assertEqual(plan.changes[("provider", "azure", "npm")], "@ai-sdk/azure")
            self.assertEqual(plan.changes[("provider", "azure", "options", "resourceName")], "demo")
            # The provider appends /v1/<path>?api-version=v1, so baseURL must stop at /openai.
            base_url = plan.changes[("provider", "azure", "options", "baseURL")]
            self.assertTrue(base_url.endswith("/openai"), base_url)
            self.assertFalse(base_url.endswith("/openai/v1"), base_url)
            self.assertEqual(base_url + "/v1", profile(protocol).endpoint)
            p = ("provider", "azure", "models", "my-deployment")
            self.assertEqual(plan.changes[p + ("id",)], "my-deployment")
            self.assertEqual(plan.changes[p + ("options", "useCompletionUrls")], protocol == "chat")
            self.assertIn("/connect", " ".join(plan.guidance))
        with patch.dict(os.environ, {"OPENCODE_CONFIG": "custom/config.json"}):
            self.assertEqual(make_plan("opencode", profile()).path, Path.cwd() / "custom/config.json")

    def test_opencode_dynamic_remote_and_jsonc_refused(self):
        for key, value in (("OPENCODE_CONFIG", "https://example.com/config.json"),
                           ("OPENCODE_CONFIG", "file:///config.json"),
                           ("OPENCODE_CONFIG", "custom.jsonc"),
                           ("OPENCODE_CONFIG_CONTENT", "{}"), ("OPENCODE_CONFIG_DIR", "other")):
            with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                with self.assertRaises(FoundryError):
                    make_plan("opencode", profile())
        with patch("pathlib.Path.exists", return_value=True), self.assertRaisesRegex(FoundryError, "JSONC"):
            make_plan("opencode", profile())

    def test_conflicting_environment_never_prints_values(self):
        for agent, key in (("codex", "OPENAI_API_KEY"), ("claude", "ANTHROPIC_AUTH_TOKEN"),
                           ("hermes", "AZURE_FOUNDRY_API_KEY"), ("opencode", "AZURE_API_KEY"),
                           ("claude", "CLAUDE_CODE_USE_BEDROCK"), ("opencode", "AZURE_RESOURCE_NAME")):
            plan = make_plan(agent, profile("anthropic" if agent == "claude" else "responses"))
            with self.subTest(agent=agent, key=key), self.assertRaises(FoundryError) as error:
                check_conflicts(plan, {}, {key: "SENSITIVE_VALUE"})
            self.assertNotIn("SENSITIVE_VALUE", str(error.exception))
        for agent in ("claude", "hermes"):
            plan = make_plan(agent, profile("anthropic" if agent == "claude" else "responses"))
            for key in ("AZURE_CLIENT_SECRET", "AZURE_CLIENT_ID", "AZURE_TENANT_ID",
                        "AZURE_FEDERATED_TOKEN_FILE", "MSI_ENDPOINT", "IDENTITY_ENDPOINT",
                        "AZURE_AUTHORITY_HOST", "AZURE_TOKEN_CREDENTIALS",
                        "AZURE_CLIENT_CERTIFICATE_PATH"):
                with self.subTest(agent=agent, key=key), self.assertRaises(FoundryError):
                    check_conflicts(plan, {}, {key: "SENSITIVE_VALUE"})

    def test_claude_process_override_and_existing_backend_conflicts(self):
        plan = make_plan("claude", profile("anthropic"))
        check_conflicts(plan, {"env": {"CLAUDE_CODE_USE_BEDROCK": "0"}}, {})
        check_conflicts(plan, {}, {"ANTHROPIC_MODEL": "my-deployment"})
        for document, env in (
            ({}, {"ANTHROPIC_MODEL": "different"}),
            ({}, {"CLAUDE_CODE_USE_FOUNDRY": "0"}),
            ({"env": {"CLAUDE_CODE_USE_VERTEX": "1"}}, {}),
            ({"env": {"AZURE_CLIENT_SECRET": "secret"}}, {}),
            ({"apiKeyHelper": "my-command"}, {}),
        ):
            with self.subTest(document=document), self.assertRaises(FoundryError):
                check_conflicts(plan, document, env)

    def test_owned_static_credential_and_route_conflicts(self):
        cases = [
            ("codex", {"profile": "selected"}),
            ("codex", {"model_providers": {"foundryconnect": {"env_key": "TOKEN"}}}),
            ("codex", {"model_providers": {"foundryconnect": {"http_headers": {"api-key": "secret"}}}}),
            ("hermes", {"model": {"api_key": "secret"}}),
            ("hermes", {"model": {"entra": {"exclude_interactive_browser": False}}}),
            ("opencode", {"provider": {"azure": {"options": {"apiKey": "secret"}}}}),
            ("opencode", {"provider": {"azure": {"options": {"useDeploymentBasedUrls": True}}}}),
            ("opencode", {"disabled_providers": ["azure"]}),
            ("opencode", {"enabled_providers": ["openai"]}),
            ("opencode", {"provider": {"azure": {"models": {"my-deployment": {
                "provider": {"npm": "@ai-sdk/anthropic"}}}}}}),
        ]
        for agent, document in cases:
            with self.subTest(agent=agent, document=document), self.assertRaises(FoundryError):
                check_conflicts(make_plan(agent, profile()), document, {})
        for agent in ("codex", "hermes", "opencode"):
            check_conflicts(make_plan(agent, profile()), {"unrelated": {"api_key": "leave alone"}}, {})

    def test_hermes_dotenv_is_readonly_and_checked(self):
        plan = make_plan("hermes", profile())
        with patch("pathlib.Path.exists", return_value=True), patch(
            "pathlib.Path.read_text", return_value="export AZURE_CLIENT_SECRET='SENSITIVE_VALUE'\n"
        ), patch("pathlib.Path.write_text") as write, self.assertRaises(FoundryError) as error:
            check_conflicts(plan, {}, {})
        write.assert_not_called()
        self.assertNotIn("SENSITIVE_VALUE", str(error.exception))

    def test_default_environment_is_honored(self):
        plan = make_plan("codex", profile())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            with self.assertRaises(FoundryError):
                check_conflicts(plan, {})
            check_conflicts(plan, {}, {})

    def test_generated_configs_are_conflict_free_when_reapplied(self):
        for agent in ("codex", "claude", "hermes", "opencode"):
            plan = make_plan(agent, profile("anthropic" if agent == "claude" else "responses"))
            document = {}
            for path, value in plan.changes.items():
                node = document
                for key in path[:-1]:
                    node = node.setdefault(key, {})
                node[path[-1]] = value
            with self.subTest(agent=agent):
                check_conflicts(plan, document, {})

    def test_native_status_is_only_structural(self):
        for agent in ("codex", "claude", "hermes"):
            self.assertTrue(native_login_status(agent)[0])
        with patch("pathlib.Path.stat", side_effect=FileNotFoundError):
            ready, message = native_login_status("opencode")
        self.assertFalse(ready)
        self.assertIn("/connect", message)
        for content, expected in [
            ({}, False), ([], False), ({"azure": {"type": "api", "key": "secret"}}, False),
            ({"azure": {"type": "oauth", "access": "secret", "accountId": "some-resource"}}, True),
        ]:
            with patch("pathlib.Path.stat", return_value=SimpleNamespace(st_size=100)), patch(
                "pathlib.Path.read_text", return_value=json.dumps(content)
            ):
                ready, message = native_login_status("opencode")
            self.assertEqual(ready, expected)
            self.assertNotIn("secret", message)
            if ready:
                self.assertIn("not verified", message)
        with patch("pathlib.Path.stat", return_value=SimpleNamespace(st_size=5)), patch(
            "pathlib.Path.read_text", return_value="{bad"
        ):
            self.assertFalse(native_login_status("opencode")[0])


class VersionTests(unittest.TestCase):
    def result(self, value, returncode=0):
        return subprocess.CompletedProcess([], returncode, value, "")

    def test_exact_minimum_floors_and_supported_banners(self):
        self.assertEqual(MIN_VERSIONS, {"codex": (0, 128, 0), "claude": (2, 0, 45),
                                        "hermes": (0, 15, 0), "opencode": (1, 18, 25)})
        for agent, banner, expected in (
            ("codex", "codex-cli 0.128.0", "0.128.0"),
            ("claude", "2.0.45 (Claude Code)", "2.0.45"),
            ("hermes", "Hermes Agent v0.15.0 (2026.5.28)\nProject: /home/user/hermes\nPython: 3.13",
             "0.15.0"),
            ("hermes", "hermes 0.21.3", "0.21.3"),
            ("opencode", "1.18.25", "1.18.25"),
        ):
            with self.subTest(agent=agent), patch(
                "foundryconnect.adapters.subprocess.run", return_value=self.result(banner)
            ) as run:
                self.assertEqual(check_version(agent), expected)
                run.assert_called_once_with([agent, "--version"], capture_output=True,
                                            text=True, timeout=10, check=False)

    def test_older_prerelease_and_unknown_versions_refused(self):
        for agent, banner in (
            ("codex", "codex-cli 0.127.0"), ("claude", "2.0.44 (Claude Code)"),
            ("hermes", "hermes 0.14.0"), ("opencode", "1.18.24"),
            ("codex", "codex-cli 0.154.0-alpha.1"), ("codex", "0.154.0+dev"),
            ("claude", "unknown 2.1.272"), ("hermes", "Hermes Agent v0.21.3-dev (2026.9.14)"),
            ("opencode", "failed: unrelated 8.0.0"), ("opencode", ""),
            ("codex", "claude 3.0.0"), ("codex", "0.154.0\nSENSITIVE_VALUE"),
        ):
            with self.subTest(banner=banner), patch(
                "foundryconnect.adapters.subprocess.run", return_value=self.result(banner)
            ), self.assertRaises(FoundryError) as error:
                check_version(agent)
            self.assertEqual(error.exception.category, "compatibility")
            self.assertNotIn("SENSITIVE_VALUE", str(error.exception))

    def test_execution_failures_are_sanitized(self):
        for failure, category in (
            (FileNotFoundError("SENSITIVE_VALUE"), "dependency"),
            (PermissionError("SENSITIVE_VALUE"), "dependency"),
            (subprocess.TimeoutExpired("SENSITIVE_VALUE", 10), "compatibility"),
        ):
            with patch("foundryconnect.adapters.subprocess.run", side_effect=failure):
                with self.assertRaises(FoundryError) as error:
                    check_version("codex")
            self.assertEqual(error.exception.category, category)
            self.assertNotIn("SENSITIVE_VALUE", str(error.exception))
        with patch("foundryconnect.adapters.subprocess.run", return_value=self.result("SENSITIVE_VALUE", 1)):
            with self.assertRaises(FoundryError) as error:
                check_version("codex")
        self.assertNotIn("SENSITIVE_VALUE", str(error.exception))

    def test_unknown_agent_is_a_domain_error(self):
        for call in (lambda: make_plan("unknown", profile()), lambda: check_version("unknown"),
                     lambda: native_login_status("unknown"), lambda: scope_for("unknown", profile())):
            with self.assertRaises(FoundryError):
                call()


if __name__ == "__main__":
    unittest.main()
