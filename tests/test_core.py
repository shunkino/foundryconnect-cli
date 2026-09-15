import io
import json
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from foundryconnect import auth, configuration as config, diagnostics, profiles
from foundryconnect.models import COGNITIVE_SCOPE, FoundryError, Plan, Profile

TENANT = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"


def profile(name="work", protocol="responses", endpoint="https://demo.openai.azure.com"):
    return profiles.validate(Profile(name, endpoint, "my-deployment", TENANT, SUBSCRIPTION, protocol))


class ProfilesTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(profile().endpoint, "https://demo.openai.azure.com/openai/v1")
        self.assertEqual(profile(protocol="anthropic", endpoint="https://demo.services.ai.azure.com/").endpoint,
                         "https://demo.services.ai.azure.com/anthropic")

    def test_reject_invalid_endpoint(self):
        for endpoint in ("http://demo.openai.azure.com", "https://evil.example",
                         "https://demo.openai.azure.com.evil.example", "https://x@y.openai.azure.com",
                         "https://demo.openai.azure.com:443", "https://demo.openai.azure.com/?key=secret",
                         "https://demo.openai.azure.com/#fragment",
                         "https://demo.services.ai.azure.com/api/projects/work"):
            with self.subTest(endpoint=endpoint), self.assertRaises(FoundryError):
                profile(endpoint=endpoint)

    def test_reject_protocol_and_name(self):
        with self.assertRaises(FoundryError):
            profile(protocol="anthropic")
        with self.assertRaises(FoundryError):
            profile(name="../escape")

    def test_store_is_private_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "config"
            profiles.add(root, profile())
            self.assertEqual(profiles.get(root, None), profile())
            self.assertEqual(stat.S_IMODE((root / "profiles.json").stat().st_mode), 0o600)
            with self.assertRaises(FoundryError):
                profiles.add(root, profile())
            self.assertNotIn("accessToken", (root / "profiles.json").read_text())

    def test_corrupted_store_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profiles.json").write_text('{"profiles": []}')
            with self.assertRaises(FoundryError):
                profiles.add(root, profile())
            self.assertEqual((root / "profiles.json").read_text(), '{"profiles": []}')


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.target = Path(self.temp.name) / "agent" / "config.json"
        self.plan = Plan("codex", self.target, "json",
                         {("model",): "deployment", ("provider", "foundry", "auth", "command"): "helper"},
                         "command-backed")

    def write(self, value):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_text(value)

    def install(self):
        return config.install(self.root, config.prepare(self.plan), "work")

    def test_new_file_round_trip_and_idempotency(self):
        self.assertTrue(self.install())
        self.assertFalse(self.install())
        self.assertEqual(config.drift(config.receipt(self.root, "codex")), [])
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertTrue(config.uninstall(self.root, "codex"))
        self.assertFalse(self.target.exists())
        self.assertFalse(config.uninstall(self.root, "codex"))

    def test_exact_existing_file_restored(self):
        original = '{ "model": "old", "unrelated": {"key": "secret"} }\n'
        self.write(original)
        self.install()
        config.uninstall(self.root, "codex")
        self.assertEqual(self.target.read_text(), original)

    def test_crlf_bytes_restored(self):
        self.target.parent.mkdir(parents=True)
        original = b'{\r\n  "model": "old"\r\n}\r\n'
        self.target.write_bytes(original)
        self.install()
        config.uninstall(self.root, "codex")
        self.assertEqual(self.target.read_bytes(), original)

    def test_yaml_aliases_refused(self):
        with self.assertRaisesRegex(FoundryError, "anchors"):
            config.parse("model: &shared\n  provider: custom\nother: *shared\n", "yaml")

    def test_unrelated_edits_preserved_and_empty_created_parents_removed(self):
        self.write('{"model":"old","keep":true}')
        self.install()
        data = json.loads(self.target.read_text())
        data["later"] = "user edit"
        self.target.write_text(json.dumps(data))
        config.uninstall(self.root, "codex")
        self.assertEqual(json.loads(self.target.read_text()), {"model": "old", "keep": True, "later": "user edit"})

    def test_user_setting_under_owned_parent_survives(self):
        self.install()
        data = json.loads(self.target.read_text())
        data["provider"]["foundry"]["custom"] = 42
        self.target.write_text(json.dumps(data))
        config.uninstall(self.root, "codex")
        self.assertEqual(json.loads(self.target.read_text()), {"provider": {"foundry": {"custom": 42}}})

    def test_owned_drift_blocks_all_removal(self):
        self.install()
        data = json.loads(self.target.read_text())
        data["model"] = "user-model"
        self.target.write_text(json.dumps(data))
        before = self.target.read_text()
        with self.assertRaisesRegex(FoundryError, "Owned settings changed"):
            config.uninstall(self.root, "codex")
        self.assertEqual(self.target.read_text(), before)
        self.assertIsNotNone(config.receipt(self.root, "codex"))

    def test_scalar_parent_is_not_destroyed(self):
        self.write('{"provider":"custom"}')
        with self.assertRaises(FoundryError):
            config.prepare(self.plan)
        self.assertEqual(self.target.read_text(), '{"provider":"custom"}')

    def test_preview_does_not_write_or_reveal_old_values(self):
        self.write('{"model":"old-secret","unrelated":"another-secret"}')
        output = config.preview(config.prepare(self.plan))
        self.assertNotIn("old-secret", output)
        self.assertNotIn("another-secret", output)
        self.assertFalse(self.root.exists())

    def test_changed_since_preview_refused(self):
        prepared = config.prepare(self.plan)
        self.write('{"new":"edit"}')
        with self.assertRaisesRegex(FoundryError, "changed after the preview"):
            config.install(self.root, prepared, "work")
        self.assertEqual(self.target.read_text(), '{"new":"edit"}')

    def test_dry_off_has_no_side_effects(self):
        self.install()
        before = self.target.read_bytes()
        config.uninstall(self.root, "codex", dry_run=True)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertIsNotNone(config.receipt(self.root, "codex"))

    def test_journal_recovers_interrupted_install(self):
        self.write('{"model":"old"}')
        prepared = config.prepare(self.plan)
        real_write = config.atomic_write

        def failing_write(path, text):
            if path == self.target:
                raise OSError("interrupted")
            real_write(path, text)

        with patch.object(config, "atomic_write", side_effect=failing_write), self.assertRaises(OSError):
            config.install(self.root, prepared, "work")
        self.assertIsNotNone(config.receipt(self.root, "codex"))
        config.uninstall(self.root, "codex")
        self.assertEqual(self.target.read_text(), '{"model":"old"}')

    def test_symlink_refused(self):
        other = Path(self.temp.name) / "other"
        other.write_text("{}")
        self.target.parent.mkdir()
        self.target.symlink_to(other)
        with self.assertRaises(FoundryError):
            self.install()
        self.assertEqual(other.read_text(), "{}")

    def test_json_duplicate_keys_refused(self):
        self.write('{"model":"a","model":"b"}')
        with self.assertRaises(FoundryError):
            self.install()

    def test_toml_and_yaml_comments_preserved(self):
        for format, original in (("toml", '# user comment\nmodel = "old"\n'),
                                 ("yaml", '# user comment\nmodel: old\n')):
            with self.subTest(format=format):
                target = self.target.with_suffix("." + format)
                target.parent.mkdir(exist_ok=True)
                target.write_text(original)
                plan = Plan("hermes", target, format, {("model",): "new"}, "native")
                prepared = config.prepare(plan)
                self.assertIn("# user comment", prepared.after)
                config.install(self.root, prepared, "work")
                config.uninstall(self.root, "hermes")
                self.assertEqual(target.read_text(), original)


class AuthTests(unittest.TestCase):
    def test_helper_requests_each_time_and_pins_tenant(self):
        results = [{"accessToken": f"token-{i}", "expires_on": str(int(time.time()) + 3600)} for i in (1, 2)]
        with patch.object(auth, "azure", side_effect=results) as azure:
            self.assertEqual(auth.token(profile(), COGNITIVE_SCOPE).value, "token-1")
            self.assertEqual(auth.token(profile(), COGNITIVE_SCOPE).value, "token-2")
        self.assertEqual(azure.call_count, 2)
        args = azure.call_args.args[0]
        self.assertIn(TENANT, args)
        self.assertIn(SUBSCRIPTION, args)
        self.assertIn(COGNITIVE_SCOPE, args)

    def test_reject_bad_token_or_expiry(self):
        for data in ({"accessToken": "", "expires_on": int(time.time()) + 3600},
                     {"accessToken": "secret\ninjected", "expires_on": int(time.time()) + 3600},
                     {"accessToken": "secret", "expires_on": int(time.time()) + 60},
                     {"accessToken": "secret", "expiresOn": "2026-10-01"},
                     {"accessToken": "secret", "expires_on": True}):
            with self.subTest(data=data), patch.object(auth, "azure", return_value=data):
                with self.assertRaises(FoundryError) as raised:
                    auth.token(profile(), COGNITIVE_SCOPE)
                self.assertNotIn("secret", str(raised.exception))

    def test_wrong_context(self):
        with patch.object(auth, "azure", return_value={"tenantId": "other", "id": SUBSCRIPTION}):
            with self.assertRaisesRegex(FoundryError, "does not match"):
                auth.account(profile())

    def test_azure_error_does_not_leak_output(self):
        result = subprocess.CompletedProcess([], 1, "secret", "secret")
        with patch("shutil.which", return_value="/usr/bin/az"), patch("subprocess.run", return_value=result):
            with self.assertRaises(FoundryError) as raised:
                auth.azure(["account", "show"])
            self.assertNotIn("secret", str(raised.exception))

    def test_login_check_never_signs_in(self):
        with patch.object(auth, "account", return_value={}), patch("subprocess.run") as run:
            auth.login(None, False)
            run.assert_not_called()


class DiagnosticsTests(unittest.TestCase):
    def test_error_categories(self):
        for status, category in ((401, "authentication"), (403, "authorization"), (404, "endpoint"),
                                 (400, "protocol"), (429, "quota"), (503, "service"), (302, "endpoint")):
            self.assertEqual(diagnostics.classify_http(status).category, category)

    def test_redirects_refused(self):
        self.assertIsNone(diagnostics.NoRedirects().redirect_request(None, None, 302, "", {}, "https://evil"))

    def test_protocol_request_shapes_and_response_validation(self):
        from unittest.mock import MagicMock
        for protocol, endpoint, route, key in (
            ("responses", "https://demo.openai.azure.com", "/openai/v1/responses", "output"),
            ("chat", "https://demo.openai.azure.com", "/openai/v1/chat/completions", "choices"),
            ("anthropic", "https://demo.services.ai.azure.com", "/anthropic/v1/messages", "content"),
        ):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = 200
            response.read.return_value = json.dumps({key: []}).encode()
            with patch.object(diagnostics, "build_opener") as opener:
                opener.return_value.open.return_value = response
                diagnostics.smoke_test(profile(protocol=protocol, endpoint=endpoint), "sensitive")
                request = opener.return_value.open.call_args.args[0]
                self.assertTrue(request.full_url.endswith(route))
                self.assertEqual(request.get_header("Authorization"), "Bearer sensitive")
                self.assertEqual(json.loads(request.data)["model"], "my-deployment")
                response.read.return_value = b'{"unexpected": true}'
                with self.assertRaises(FoundryError):
                    diagnostics.smoke_test(profile(protocol=protocol, endpoint=endpoint), "sensitive")

    def test_http_body_not_disclosed(self):
        error = HTTPError("https://demo.openai.azure.com", 403, "secret", {}, io.BytesIO(b"secret"))
        with patch.object(diagnostics, "build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(FoundryError) as raised:
                diagnostics.smoke_test(profile(), "secret")
            self.assertNotIn("secret", str(raised.exception))

    def test_network_error_classification(self):
        with patch.object(diagnostics, "build_opener") as opener:
            opener.return_value.open.side_effect = URLError("secret")
            with self.assertRaises(FoundryError) as raised:
                diagnostics.smoke_test(profile(), "secret")
            self.assertEqual(raised.exception.category, "endpoint")


if __name__ == "__main__":
    unittest.main()
