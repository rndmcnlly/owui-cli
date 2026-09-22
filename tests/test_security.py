"""Exercise the real dispatcher and HTTP serialization with hostile responses."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from owui_cli import cli


SUBMITTED = "submitted-secret-marker"
SIBLING = "sibling-secret-marker"
CLIENT = httpx.Client


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.body = self.root / "body.json"
        self.body.write_text(json.dumps({"id": "target", "name": SUBMITTED,
                                         "value": SUBMITTED, "meta": {}}))
        self.source = self.root / "source.py"
        self.source.write_text('"""title: Test"""\nclass Tools: pass\n')
        self.requests = []

    def run_cli(self, args, *, status=200, stdin="", env=None, handler=None):
        def respond(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            return httpx.Response(status, json={
                "id": "target", "name": SIBLING, "value": SUBMITTED,
                "sibling": SIBLING, "meta": {}, "access_grants": [],
                "is_active": True, "is_global": True})
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {"OWUI_URL": "https://example.test", "OWUI_TOKEN": "token"}
        if env is not None:
            environment = env
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(cli.httpx, "Client", side_effect=lambda **kw: CLIENT(
                    transport=httpx.MockTransport(respond), **kw)), \
                patch.object(cli.sys, "argv", ["owui-cli", *args]), \
                patch.object(cli.sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = 0
            try:
                cli.main()
            except SystemExit as e:
                code = e.code
        return code, stdout.getvalue(), stderr.getvalue()

    def mutation_cases(self):
        body = str(self.body)
        cases = {}
        for resource in ("tools", "functions", "skills"):
            cases[resource, "deploy"] = [str(self.source), "target"]
            cases[resource, "delete"] = ["target"]
        for resource in ("tools", "functions"):
            for prefix in ("valves", "valves-user"):
                cases[resource, prefix + "-set"] = ["target", body]
                cases[resource, prefix + "-set-field"] = ["target", "value", SUBMITTED]
                cases[resource, prefix + "-set-field-file"] = ["target", "value", "-"]
                cases[resource, prefix + "-unset-field"] = ["target", "value"]
        for resource, operations in {
            "functions": {"toggle": ["target"], "toggle-global": ["target"]},
            "skills": {"toggle": ["target"], "grant": ["target", "user", "principal", "read"], "revoke": ["target"]},
            "models": {"create": [body], "update": [body], "delete": ["target"], "set-tools": ["target", SUBMITTED], "set-filters": ["target", SUBMITTED]},
            "knowledge": {"create": [SUBMITTED, SUBMITTED], "delete": ["target"], "add-file": ["target", "file"], "remove-file": ["target", "file"]},
            "files": {"upload": [body], "delete": ["target"]},
            "groups": {"create": [SUBMITTED], "delete": ["target"], "add-user": ["target", "user"], "remove-user": ["target", "user"], "update": ["target", body]},
            "users": {"add": [SUBMITTED, SUBMITTED], "delete": ["target"], "update": ["target", body]},
            "chats": {"delete": ["target"]},
            "configs": {"set": ["section", body], "admin-set": [body]},
            "prompts": {"create": [body], "delete": ["target"]},
        }.items():
            for operation, args in operations.items():
                cases[resource, operation] = args
        return cases

    def test_every_mutation_receipt(self):
        cases = self.mutation_cases()
        # Explicit classification makes a new command require an audit decision.
        reads = {"list", "show", "pull", "pull-all", "valves", "valves-spec",
                 "valves-user", "valves-user-spec", "files", "members", "find",
                 "search", "all", "stats", "get", "admin"}
        self.assertEqual(set(cli.COMMANDS) - set(cases),
                         {key for key in cli.COMMANDS if key[1] in reads})
        for (resource, operation), args in cases.items():
            for flags in ([], ["--json"]):
                with self.subTest(resource=resource, operation=operation, flags=flags):
                    code, out, err = self.run_cli([*flags, resource, operation, *args], stdin=SUBMITTED)
                    self.assertEqual((code, err), (0, ""))
                    receipt = json.loads(out)
                    self.assertTrue(receipt["ok"])
                    self.assertEqual(receipt["resource"], resource)
                    self.assertNotIn(SUBMITTED, out + err)
                    self.assertNotIn(SIBLING, out + err)

    def test_http_errors_withhold_body_in_both_modes(self):
        for flags in ([], ["--json"]):
            for status in (401, 403, 422, 500):
                code, out, err = self.run_cli([*flags, "configs", "admin-set", str(self.body)], status=status)
                self.assertEqual((code, out), (1, ""))
                self.assertIn(str(status), err)
                self.assertNotIn(SUBMITTED, err)
                self.assertNotIn(SIBLING, err)

    def test_input_and_transport_exceptions_withhold_details(self):
        for exception in (httpx.ConnectError(SIBLING), ValueError(SUBMITTED)):
            def fail(request):
                raise exception
            code, out, err = self.run_cli(["--json", "configs", "show"], handler=fail)
            self.assertEqual((code, out), (1, ""))
            self.assertFalse(json.loads(err)["ok"])
            self.assertNotIn(SIBLING, err)
            self.assertNotIn(SUBMITTED, err)
        code, out, err = self.run_cli(["configs", "admin-set", "-"], stdin=SUBMITTED)
        self.assertEqual(code, 1)
        self.assertNotIn(SUBMITTED, out + err)

    def test_secret_inputs_and_option_looking_values(self):
        for value in (SUBMITTED, "--json", "--version"):
            code, out, err = self.run_cli(["tools", "valves-set-field", "target", "value", value])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(self.requests[-1].content)["value"], value)
        code, out, err = self.run_cli(["tools", "valves-set-field-file", "target", "value", "-"], stdin=json.dumps(SUBMITTED))
        self.assertEqual(code, 0)
        payload = json.loads(self.requests[-1].content)
        self.assertEqual(payload["value"], SUBMITTED)
        self.assertEqual(payload["sibling"], SIBLING)
        code, _, _ = self.run_cli(["configs", "admin-set", "-"], stdin=self.body.read_text())
        self.assertEqual(code, 0)

    def test_token_file_and_url_validation(self):
        token = self.root / "token"
        token.write_text(SUBMITTED + "\n")
        env = {"OWUI_URL": "https://example.test", "OWUI_TOKEN_FILE": str(token)}
        code, _, _ = self.run_cli(["groups", "delete", "target"], env=env)
        self.assertEqual(code, 0)
        self.assertEqual(self.requests[-1].headers["Authorization"], "Bearer " + SUBMITTED)
        for url in ("http://example.test", "https://user:secret@example.test", "https://example.test/?key=secret"):
            code, _, err = self.run_cli(["groups", "delete", "target"], env={**env, "OWUI_URL": url})
            self.assertEqual(code, 1)
            self.assertNotIn("secret", err)

    def test_deploy_only_creates_on_404(self):
        def handler(request):
            return httpx.Response(404 if request.method == "GET" else 200, json={})
        code, out, _ = self.run_cli(["tools", "deploy", str(self.source), "target"], handler=handler)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["action"], "created")
        self.requests.clear()
        code, _, _ = self.run_cli(["tools", "deploy", str(self.source), "target"], status=403)
        self.assertEqual(code, 1)
        self.assertEqual([r.method for r in self.requests], ["GET"])

    def test_reads_remain_explicit_disclosures(self):
        code, out, _ = self.run_cli(["--json", "tools", "valves", "target"])
        self.assertEqual(code, 0)
        self.assertIn(SIBLING, out)

    def test_missing_function_401_requires_successful_absence_check(self):
        for listing in ([], [{"id": "target"}], {"unexpected": True}):
            self.requests.clear()
            def handler(request):
                if request.url.path == "/api/v1/functions/list":
                    return httpx.Response(200, json=listing)
                return httpx.Response(401 if request.method == "GET" else 200, json={})
            code, _, _ = self.run_cli(["functions", "deploy", str(self.source), "target"], handler=handler)
            self.assertEqual(code, 0 if listing == [] else 1)
            self.assertEqual(any(r.method == "POST" for r in self.requests), listing == [])

    def test_user_password_is_random_and_not_output(self):
        passwords = []
        for _ in range(2):
            code, out, _ = self.run_cli(["users", "add", "test@example.test", "Test"])
            self.assertEqual(code, 0)
            password = json.loads(self.requests[-1].content)["password"]
            passwords.append(password)
            self.assertGreaterEqual(len(password), 48)
            self.assertNotIn(password, out)
        self.assertNotEqual(*passwords)

    def test_receipt_rejects_structured_identifier(self):
        def handler(request):
            return httpx.Response(200, json={"id": {"secret": SIBLING}})
        code, out, err = self.run_cli(["groups", "create", "Test"], handler=handler)
        self.assertEqual((code, out), (1, ""))
        self.assertNotIn(SIBLING, err)

    def test_exports_and_sibling_images(self):
        self.assertEqual(cli._export_name("../org/model"), "..%2Forg%2Fmodel")
        target = self.root / "export" / "data.json"
        cli._write_json(str(target), {"secret": SIBLING})
        self.assertIn(SIBLING, target.read_text())
        image = self.root / "image.png"
        cli._write_file(str(image), b"\x00\x0a\x0d\x1a\xff")
        self.assertEqual(image.read_bytes(), b"\x00\x0a\x0d\x1a\xff")
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        for source, expected in (("CON", "%43ON"), ("nul.txt", "%6Eul.txt"),
                                 ("trailing.", "trailing%2E"), ("a%2Fb", "a%252Fb")):
            self.assertEqual(cli._export_name(source), expected)
        link = self.root / "link"
        link.symlink_to(target)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli._write_file(str(link), "overwrite")
        hard = self.root / "hard"
        os.link(target, hard)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli._write_file(str(hard), "overwrite")
        self.assertIn(SIBLING, target.read_text())
        symlink_dir = self.root / "symlink-dir"
        symlink_dir.symlink_to(target.parent, target_is_directory=True)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli._write_file(str(symlink_dir / "new"), "overwrite")
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo"
            os.mkfifo(fifo)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli._write_file(str(fifo), "overwrite")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli._inline_sibling_images(str(self.body), {"meta": {"profile_image_url": "../private.png"}})
        self.assertIsNone(cli._extract_data_uri("data:image/../../bad;base64,eA=="))


if __name__ == "__main__":
    unittest.main()
