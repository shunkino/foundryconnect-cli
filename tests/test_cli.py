import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foundryconnect import cli
from foundryconnect.configuration import parse
from tests.test_core import SUBSCRIPTION, TENANT


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / "foundryconnect"
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.log = self.home / "az-calls.jsonl"
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
               "FOUNDRYCONNECT_HOME": str(self.root), "AZ_TEST_LOG": str(self.log)}
        self.environment = patch.dict(os.environ, env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.executable("az", f"""
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["AZ_TEST_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["account", "show"]:
    print(json.dumps({{"tenantId": "{TENANT}", "id": "{SUBSCRIPTION}"}}))
elif args[:2] == ["account", "get-access-token"]:
    print(json.dumps({{"accessToken": "test-bearer-token", "expires_on": int(time.time()) + 3600}}))
else:
    sys.exit(2)
""")
        for agent, version in (("codex", "0.154.0"), ("claude", "2.1.272"),
                               ("hermes", "0.21.3"), ("opencode", "1.18.31")):
            self.executable(agent, f'import sys\nprint("{agent} {version}")\n')

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + source)
        path.chmod(0o700)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def add_profile(self, protocol="responses", name="work"):
        endpoint = ("https://demo.services.ai.azure.com" if protocol == "anthropic"
                    else "https://demo.openai.azure.com")
        code, _, error = self.invoke("profile", "add", name, "--endpoint", endpoint,
                                    "--deployment", "my-deployment", "--tenant", TENANT,
                                    "--subscription", SUBSCRIPTION, "--protocol", protocol)
        self.assertEqual(code, 0, error)

    def test_dry_run_never_authenticates_or_creates_agent_config(self):
        self.add_profile()
        before = sorted(str(path) for path in self.home.rglob("*"))
        code, output, error = self.invoke("codex", "on", "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertIn("Preview only", output)
        self.assertEqual(before, sorted(str(path) for path in self.home.rglob("*")))
        self.assertFalse(self.log.exists())

    def test_empty_xdg_home_uses_the_same_profile_root_as_helper(self):
        with patch.dict(os.environ, {"FOUNDRYCONNECT_HOME": "", "XDG_CONFIG_HOME": ""}):
            self.add_profile()
            code, _, error = self.invoke("codex", "on", "--yes")
            self.assertEqual(code, 0, error)
            document = parse((self.home / ".codex" / "config.toml").read_text(), "toml")
            args = document["model_providers"]["foundryconnect"]["auth"]["args"]
            root = Path(args[-1])
            self.assertTrue((root / "profiles.json").exists())
            self.assertEqual(root, self.home / ".config" / "foundryconnect")

    def test_codex_lifecycle_and_real_helper_contract(self):
        self.add_profile()
        code, output, error = self.invoke("codex", "on", "--yes")
        self.assertEqual(code, 0, error)
        self.assertIn("Configuration installed", output)
        self.assertNotIn("test-bearer-token", output)
        path = self.home / ".codex" / "config.toml"
        document = parse(path.read_text(), "toml")
        helper = document["model_providers"]["foundryconnect"]["auth"]
        environment = dict(os.environ)
        environment.pop("FOUNDRYCONNECT_HOME")
        result = subprocess.run([helper["command"], *helper["args"]], env=environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "test-bearer-token\n")
        self.assertEqual(result.stderr, "")
        self.assertNotIn("test-bearer-token", path.read_text())
        self.assertNotIn("test-bearer-token", (self.root / "receipts" / "codex.json").read_text())
        code, output, error = self.invoke("codex", "status")
        self.assertEqual(code, 0, error)
        self.assertIn("not verified", output)
        self.assertEqual(self.invoke("codex", "on", "--yes")[0], 0)
        code, output, error = self.invoke("codex", "off", "--yes")
        self.assertEqual(code, 0, error)
        self.assertFalse(path.exists())
        self.assertNotIn("logout", self.log.read_text())

    def test_all_native_agents_install_and_remove(self):
        for agent, protocol in (("claude", "anthropic"), ("hermes", "responses"), ("opencode", "responses")):
            with self.subTest(agent=agent):
                self.add_profile(protocol, agent)
                code, output, error = self.invoke(agent, "on", "--profile", agent, "--yes")
                self.assertEqual(code, 0, error)
                self.assertIn("Configuration installed", output)
                self.assertNotIn("test-bearer-token", output)
                if agent == "opencode":
                    self.assertIn("Setup incomplete", output)
                    self.assertEqual(self.invoke(agent, "status")[0], 1)
                self.assertEqual(self.invoke(agent, "off", "--yes")[0], 0)

    def test_unsupported_version_does_not_write(self):
        self.add_profile()
        self.executable("codex", 'print("codex 0.100.0")\n')
        code, _, _ = self.invoke("codex", "on", "--yes")
        self.assertEqual(code, 1)
        self.assertFalse((self.home / ".codex").exists())
        self.assertFalse((self.root / "receipts").exists())

    def test_authentication_failure_does_not_write_or_leak(self):
        self.add_profile()
        self.executable("az", 'import sys\nprint("secret", file=sys.stderr)\nsys.exit(1)\n')
        code, output, error = self.invoke("codex", "on", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("authentication:", error)
        self.assertNotIn("secret", output + error)
        self.assertFalse((self.home / ".codex").exists())

    def test_token_helper_failure_has_empty_stdout(self):
        self.add_profile()
        self.executable("az", 'import sys\nprint("secret")\nsys.exit(1)\n')
        code, output, error = self.invoke("auth", "token", "--profile", "work")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("authentication:", error)
        self.assertNotIn("secret", error)

    def test_profile_list_and_login_do_not_emit_credentials(self):
        self.add_profile()
        code, output, error = self.invoke("profile", "list")
        self.assertEqual(code, 0, error)
        self.assertIn("work", output)
        self.assertIn(TENANT, output)
        code, output, error = self.invoke("login", "--profile", "work")
        self.assertEqual(code, 0, error)
        self.assertIn("Agents detected", output)
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ["account", "show"])

    def test_wrong_context_refuses_native_install(self):
        self.add_profile("anthropic")
        self.executable("az", f'import json\nprint(json.dumps({{"tenantId":"other","id":"{SUBSCRIPTION}"}}))\n')
        code, output, error = self.invoke("claude", "on", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("does not match", error)
        self.assertFalse((self.home / ".claude").exists())

    def test_doctor_inference_requires_explicit_flag(self):
        self.add_profile()
        with patch("foundryconnect.diagnostics.smoke_test") as smoke:
            code, output, error = self.invoke("doctor", "--agent", "codex")
            self.assertEqual(code, 0, error)
            smoke.assert_not_called()
            self.assertIn("not verified", output)
            code, output, error = self.invoke("doctor", "--agent", "codex", "--smoke-test")
            self.assertEqual(code, 0, error)
            smoke.assert_called_once()
            self.assertIn("Inference successfully verified", output)

    def test_noninteractive_on_needs_approval(self):
        self.add_profile()
        with patch("sys.stdin.isatty", return_value=False):
            code, _, error = self.invoke("codex", "on")
        self.assertEqual(code, 1)
        self.assertIn("requires approval", error)
        self.assertFalse((self.home / ".codex").exists())

    def test_explicit_opencode_config_symlink_is_not_followed(self):
        self.add_profile()
        target = self.home / "original.json"
        target.write_text("{}")
        link = self.home / "linked.json"
        link.symlink_to(target)
        with patch.dict(os.environ, {"OPENCODE_CONFIG": str(link)}):
            code, _, error = self.invoke("opencode", "on", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("symlink", error)
        self.assertEqual(target.read_text(), "{}")
        self.assertFalse((self.root / "receipts").exists())

    def test_missing_profile_has_clean_error(self):
        code, _, error = self.invoke("codex", "on", "--profile", "missing", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", error)


if __name__ == "__main__":
    unittest.main()
