"""Opt-in production verification: OWUI_URL/OWUI_TOKEN, uv run python tests/live_smoke.py.

Creates disposable plugins, verifies valve preservation, deletes them in finally.
Subprocess output and server state are checked in memory, never dumped on failure.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

import httpx


def check(condition, label):
    if not condition:
        raise RuntimeError(label)


def run(*args, stdin=None):
    result = subprocess.run(["owui-cli", "--json", *args], input=stdin,
                            text=True, capture_output=True)
    check(result.returncode == 0, "CLI command failed; output withheld")
    check("smoke-secret" not in result.stdout + result.stderr, "secret output detected")
    data = json.loads(result.stdout)
    check(data.get("ok") is True, "missing success receipt")
    return data


def main():
    url, token = os.environ["OWUI_URL"], os.environ["OWUI_TOKEN"]
    plugin_id = "cli_security_smoke_" + uuid.uuid4().hex[:12]
    created = []
    source_prefix = '"""\ntitle: CLI security smoke\nversion: 1.0.0\n"""\nfrom pydantic import BaseModel\n'
    schemas = '''
    class Valves(BaseModel):
        value: str = ""
        sibling: str = ""
    class UserValves(BaseModel):
        value: str = ""
        sibling: str = ""
    def __init__(self):
        self.valves = self.Valves()
'''
    with tempfile.TemporaryDirectory() as directory, httpx.Client(
            base_url=url, headers={"Authorization": "Bearer " + token}, timeout=60) as client:
        try:
            for kind, cls in (("tools", "Tools"), ("functions", "Filter")):
                path = Path(directory) / (kind + ".py")
                method = ('    def ping(self) -> str:\n        return "pong"\n' if kind == "tools" else
                          '    def inlet(self, body: dict) -> dict:\n        return body\n')
                path.write_text(source_prefix + f"class {cls}:\n" + schemas + method)
                # Register before deploy so cleanup is attempted after an ambiguous failure.
                created.append(kind)
                run(kind, "deploy", str(path), plugin_id)
                if kind == "functions":
                    run(kind, "toggle", plugin_id)
                for scope in ("", "-user"):
                    prefix = "valves" + scope
                    run(kind, prefix + "-set", plugin_id, "-", stdin=json.dumps({
                        "value": "smoke-secret-original", "sibling": "smoke-secret-sibling"}))
                    run(kind, prefix + "-set-field-file", plugin_id, "value", "-",
                        stdin='"smoke-secret-replacement"')
                    endpoint = f"/api/v1/{kind}/id/{plugin_id}/valves" + ("/user" if scope else "")
                    response = client.get(endpoint)
                    check(response.status_code == 200, "read-back failed")
                    data = response.json()
                    check(data["value"] == "smoke-secret-replacement", "changed field mismatch")
                    check(data["sibling"] == "smoke-secret-sibling", "sibling not preserved")
                    run(kind, prefix + "-unset-field", plugin_id, "value")
                print(f"PASS {kind}: deploy, admin/user valve set, field-file, unset, read-back")
        finally:
            cleanup_failed = False
            for kind in reversed(created):
                response = client.get(f"/api/v1/{kind}/")
                if response.status_code == 200 and not any(
                        item["id"] == plugin_id for item in response.json()):
                    continue
                try:
                    run(kind, "delete", plugin_id)
                    print(f"PASS {kind}: disposable plugin deleted")
                except RuntimeError:
                    cleanup_failed = True
                    print(f"FAIL cleanup {kind}: {plugin_id}")
            check(not cleanup_failed, "disposable plugin cleanup failed")


if __name__ == "__main__":
    main()
