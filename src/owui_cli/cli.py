"""Open WebUI admin CLI. Designed for agent consumption — terse output by default.

Usage: OWUI_URL=... OWUI_TOKEN=... owui-cli [--json] <command> [args...]

Commands:
    help                        Show all resources and commands
    schema [resource] [method]  Show API schema from bundled reference
    <resource> <command> [args] Run an admin operation

Use --json for machine-readable output on any command.
"""

import base64
import json
import os
import re
import sys
from importlib.resources import files

import httpx

import owui_cli

# ── globals ───────────────────────────────────────────────────────────

TIMEOUT = 60.0
JSON_OUTPUT = False
SCHEMA_PATH = files("owui_cli.data").joinpath("api-schema.json")

def _env():
    url = os.environ.get("OWUI_URL", "")
    token = os.environ.get("OWUI_TOKEN", "")
    if not url or not token:
        die("OWUI_URL and OWUI_TOKEN env vars required")
    return url.rstrip("/"), token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _api(url: str, path: str) -> str:
    return f"{url}{path}"


def _get(c: httpx.Client, url: str, path: str, token: str) -> httpx.Response:
    r = c.get(_api(url, path), headers=_headers(token))
    r.raise_for_status()
    return r


def _post(c: httpx.Client, url: str, path: str, token: str, body=None) -> httpx.Response:
    r = c.post(_api(url, path), headers=_headers(token), json=body or {})
    r.raise_for_status()
    return r


def _delete(c: httpx.Client, url: str, path: str, token: str) -> httpx.Response:
    r = c.delete(_api(url, path), headers=_headers(token))
    r.raise_for_status()
    return r


def die(msg: str, code: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(code)

def out(data, fmt_fn=None):
    """Print data. JSON mode emits raw JSON; otherwise use fmt_fn or default."""
    if JSON_OUTPUT:
        print(json.dumps(data, default=str))
    elif fmt_fn:
        fmt_fn(data)
    elif isinstance(data, str):
        print(data)
    elif isinstance(data, list):
        for item in data:
            print(json.dumps(item, default=str))
    else:
        print(json.dumps(data, indent=2, default=str))


def out_table(rows: list[dict], cols: list[tuple[str, str, int]]):
    """Terse aligned table. cols: [(header, key, min_width), ...]"""
    if JSON_OUTPUT:
        print(json.dumps(rows, default=str))
        return
    if not rows:
        print("(none)")
        return
    widths = [max(mw, len(h), max((len(str(r.get(k, ""))) for r in rows), default=0))
              for h, k, mw in cols]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*[h for h, _, _ in cols]))
    print(fmt.format(*["-" * w for w in widths]))
    for row in rows:
        print(fmt.format(*[str(row.get(k, "")) for _, k, _ in cols]))


def out_kv(pairs: list[tuple[str, str]]):
    """Key-value display."""
    if JSON_OUTPUT:
        print(json.dumps(dict(pairs), default=str))
        return
    w = max(len(k) for k, _ in pairs) if pairs else 0
    for k, v in pairs:
        print(f"{k:<{w}}  {v}")

def _parse_docstring_meta(source: str) -> dict[str, str]:
    """Extract key: value pairs from module-level triple-quoted docstring."""
    m = re.match(r'^"""(.*?)"""', source, re.DOTALL) or re.match(r"^'''(.*?)'''", source, re.DOTALL)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = value.strip()
    return meta


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Parse optional YAML-style front-matter. Returns (meta, content)."""
    fm = re.match(r"^---\n(.*?)\n---\n?", raw, re.DOTALL)
    if not fm:
        return {}, raw
    meta = {}
    for line in fm.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower()] = v.strip()
    return meta, raw[fm.end():]


def _write_file(path: str, content: str | bytes):
    """Write content to a file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode) as f:
        f.write(content)


def _write_json(path: str, obj):
    """Write JSON to a file, creating parent directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _extract_data_uri(data_uri: str) -> tuple[str, bytes] | None:
    """Decode a data:image/*;base64,... URI. Returns (ext, bytes) or None."""
    if not data_uri or not data_uri.startswith("data:image/"):
        return None
    header, _, b64data = data_uri.partition(",")
    if not b64data:
        return None
    mime = header.split(";")[0].replace("data:", "")
    ext = mime.split("/")[1] if "/" in mime else "png"
    return ext, base64.b64decode(b64data)


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", title.lower(), "").strip("_") if not title else re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")

class Resource:
    """Generic CRUD resource. Subclass or configure to add resource-specific commands."""

    def __init__(self, name: str, prefix: str, id_pattern: str = "/id/{id}",
                 list_cols: list[tuple[str, str, int]] = None,
                 show_fields: list[tuple[str, str]] = None,
                 content_key: str = "content",
                 meta_parser: str = "docstring",  # "docstring" or "frontmatter"
                 deploy_ext: str = ".py",
                 workspace_path: str = None):
        self.name = name
        self.prefix = prefix
        self.id_pattern = id_pattern
        self.list_cols = list_cols or [("ID", "id", 10), ("NAME", "name", 20)]
        self.show_fields = show_fields or []
        self.content_key = content_key
        self.meta_parser = meta_parser
        self.deploy_ext = deploy_ext
        self.workspace_path = workspace_path or f"/workspace/{name}"
        self._commands: dict[str, tuple] = {}
        self._register_defaults()

    def _register_defaults(self):
        """Register standard CRUD commands."""
        self._commands["list"] = (self.cmd_list, "", (0, 0))
        self._commands["show"] = (self.cmd_show, "<id>", (1, 1))
        self._commands["deploy"] = (self.cmd_deploy, f"<source{self.deploy_ext}> [id]", (1, 2))
        self._commands["pull"] = (self.cmd_pull, "<id>", (1, 1))
        self._commands["pull-all"] = (self.cmd_pull_all, "[dir]", (0, 1))
        self._commands["delete"] = (self.cmd_delete, "<id>", (1, 1))

    def add_command(self, name, fn, arg_spec, arg_range):
        self._commands[name] = (fn, arg_spec, arg_range)

    def item_path(self, item_id: str) -> str:
        return f"{self.prefix}{self.id_pattern.replace('{id}', item_id)}"

    def cmd_list(self, url: str, token: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _get(c, url, f"{self.prefix}/", token).json()
        items = data if isinstance(data, list) else data.get("items", data.get("data", []))
        if not items:
            out("(none)")
            return
        rows = []
        for item in sorted(items, key=lambda x: x.get("id", "")):
            row = {}
            for _, key, _ in self.list_cols:
                if key == "ver":
                    row[key] = (item.get("meta") or {}).get("manifest", {}).get("version", "?")
                elif "." in key:
                    parts = key.split(".")
                    v = item
                    for p in parts:
                        v = (v or {}).get(p, "")
                    row[key] = str(v)[:60] if v else ""
                else:
                    row[key] = str(item.get(key, ""))[:60]
            rows.append(row)
        out_table(rows, self.list_cols)

    def cmd_show(self, url: str, token: str, item_id: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))
            if r.status_code == 404:
                die(f"{self.name} '{item_id}' not found")
            r.raise_for_status()
        item = r.json()
        if JSON_OUTPUT:
            # Strip content for --json show (it's huge), keep everything else
            slim = {k: v for k, v in item.items() if k != self.content_key}
            content = item.get(self.content_key, "")
            slim["content_length"] = len(content)
            out(slim)
            return
        pairs = [("id", item.get("id", "?")), ("name", item.get("name", "?"))]
        for label, key in self.show_fields:
            v = item
            for part in key.split("."):
                v = (v or {}).get(part, "")
            pairs.append((label, str(v) if v else "(none)"))
        content = item.get(self.content_key, "")
        pairs.append(("content", f"{len(content)} chars"))
        grants = item.get("access_grants") or []
        if grants:
            pairs.append(("grants", str(len(grants))))
        out_kv(pairs)

    def cmd_deploy(self, url: str, token: str, source_path: str, item_id: str = ""):
        with open(source_path) as f:
            content = f.read()

        if self.meta_parser == "frontmatter":
            meta, content_body = _parse_frontmatter(content)
        else:
            meta = _parse_docstring_meta(content)
            content_body = content  # deploy full file for tools/functions

        title = meta.get("title", meta.get("name",
                 source_path.rsplit("/", 1)[-1].removesuffix(self.deploy_ext)))
        version = meta.get("version", "")
        if not item_id:
            item_id = meta.get("id", _slugify(title))

        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))

            if r.status_code == 200:
                existing = r.json()
                payload = self._build_update_payload(existing, item_id, content_body if self.meta_parser == "frontmatter" else content, meta)
                r = _post(c, url, f"{self.item_path(item_id)}/update", token, payload)
                old_ver = (existing.get("meta") or {}).get("manifest", {}).get("version", "")
                label = f"updated {item_id}"
                if old_ver and version:
                    label += f" {old_ver} -> {version}"
                out(label)
            else:
                payload = self._build_create_payload(item_id, title, content_body if self.meta_parser == "frontmatter" else content, meta)
                r = _post(c, url, f"{self.prefix}/create", token, payload)
                out(f"created {item_id}" + (f" v{version}" if version else ""))

    def _build_update_payload(self, existing, item_id, content, meta):
        return {
            "id": item_id,
            "name": existing.get("name", meta.get("title", item_id)),
            "meta": existing.get("meta", {"description": meta.get("description", ""), "manifest": {}}),
            self.content_key: content,
        }

    def _build_create_payload(self, item_id, title, content, meta):
        return {
            "id": item_id,
            "name": title,
            "meta": {"description": meta.get("description", ""), "manifest": {}},
            self.content_key: content,
        }

    def cmd_pull(self, url: str, token: str, item_id: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))
            if r.status_code == 404:
                die(f"{self.name} '{item_id}' not found")
            r.raise_for_status()
        content = r.json().get(self.content_key, "")
        sys.stdout.write(content)
        if content and not content.endswith("\n"):
            sys.stdout.write("\n")

    def cmd_pull_all(self, url: str, token: str, out_dir: str = "."):
        """Pull all items into <out_dir>/<id>/source + <out_dir>/<id>/meta.json."""
        src_name = {"tools": "tool.py", "functions": "function.py", "skills": "skill.md"}.get(self.name, "source")
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _get(c, url, f"{self.prefix}/", token).json()
        items = data if isinstance(data, list) else data.get("items", data.get("data", []))
        ids = sorted(item.get("id", "") for item in items)
        if not ids:
            out("(none)")
            return
        count = 0
        with httpx.Client(timeout=TIMEOUT) as c:
            for item_id in ids:
                r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))
                if r.status_code == 404:
                    print(f"  skip {item_id} (not found)", file=sys.stderr)
                    continue
                r.raise_for_status()
                item = r.json()
                item_dir = os.path.join(out_dir, item_id)
                # Write source
                content = item.pop(self.content_key, "")
                _write_file(os.path.join(item_dir, src_name), content if content.endswith("\n") else content + "\n")
                # Write metadata (everything except source content)
                _write_json(os.path.join(item_dir, "meta.json"), item)
                count += 1
                if not JSON_OUTPUT:
                    print(f"  {item_id}")
        out(f"pulled {count} {self.name}")

    def cmd_delete(self, url: str, token: str, item_id: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))
            if r.status_code == 404:
                die(f"{self.name} '{item_id}' not found")
            r.raise_for_status()
            name = r.json().get("name", item_id)
            _delete(c, url, f"{self.item_path(item_id)}/delete", token)
        out(f"deleted {name} ({item_id})")

    def get_commands(self) -> dict[str, tuple]:
        return self._commands

# ── resource instances ────────────────────────────────────────────────

tools_res = Resource("tools", "/api/v1/tools",
    list_cols=[("ID", "id", 10), ("NAME", "name", 20), ("VER", "ver", 5)],
    show_fields=[("version", "meta.manifest.version"), ("author", "meta.manifest.author"),
                 ("description", "meta.description")],
    workspace_path="/workspace/tools")

functions_res = Resource("functions", "/api/v1/functions",
    list_cols=[("ID", "id", 10), ("NAME", "name", 20), ("TYPE", "type", 6), ("VER", "ver", 5)],
    show_fields=[("type", "type"), ("version", "meta.manifest.version"),
                 ("active", "is_active"), ("global", "is_global")],
    workspace_path="/workspace/functions")


# ── valves (user valves for tools and functions) ─────────────────────

def _valves_get(url, token, kind, item_id):
    """GET valves for a tool or function. kind is 'tools' or 'functions'."""
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _get(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user", token)
    out(r.json())

def _valves_spec(url, token, kind, item_id):
    """GET the UserValves spec (schema) for a tool or function."""
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _get(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user/spec", token)
    out(r.json())

def _valves_set(url, token, kind, item_id, json_path):
    """POST updated user valves from a JSON file."""
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user/update", token, payload)
    out(r.json())

def _valves_set_field(url, token, kind, item_id, key, value):
    """Set a single field in the user valves. Value is parsed as JSON; falls back to string."""
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        parsed = value
    with httpx.Client(timeout=TIMEOUT) as c:
        current = _get(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user", token).json()
        current[key] = parsed
        r = _post(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user/update", token, current)
    out(r.json())

def _valves_unset_field(url, token, kind, item_id, key):
    """Remove a single field from the user valves."""
    with httpx.Client(timeout=TIMEOUT) as c:
        current = _get(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user", token).json()
        if key not in current:
            die(f"key '{key}' not found in valves")
        del current[key]
        r = _post(c, url, f"/api/v1/{kind}/id/{item_id}/valves/user/update", token, current)
    out(r.json())

# Wrappers that bind kind='tools'
def tools_valves_get(url, token, item_id):
    _valves_get(url, token, "tools", item_id)

def tools_valves_spec(url, token, item_id):
    _valves_spec(url, token, "tools", item_id)

def tools_valves_set(url, token, item_id, json_path):
    _valves_set(url, token, "tools", item_id, json_path)

def tools_valves_set_field(url, token, item_id, key, value):
    _valves_set_field(url, token, "tools", item_id, key, value)

def tools_valves_unset_field(url, token, item_id, key):
    _valves_unset_field(url, token, "tools", item_id, key)

# Wrappers that bind kind='functions'
def functions_valves_get(url, token, item_id):
    _valves_get(url, token, "functions", item_id)

def functions_valves_spec(url, token, item_id):
    _valves_spec(url, token, "functions", item_id)

def functions_valves_set(url, token, item_id, json_path):
    _valves_set(url, token, "functions", item_id, json_path)

def functions_valves_set_field(url, token, item_id, key, value):
    _valves_set_field(url, token, "functions", item_id, key, value)

def functions_valves_unset_field(url, token, item_id, key):
    _valves_unset_field(url, token, "functions", item_id, key)


class SkillsResource(Resource):
    """Skills use frontmatter and have grant/revoke commands."""

    def __init__(self):
        super().__init__("skills", "/api/v1/skills",
            list_cols=[("ID", "id", 20), ("NAME", "name", 20), ("ACTIVE", "is_active", 6)],
            show_fields=[("description", "description"), ("active", "is_active")],
            meta_parser="frontmatter", deploy_ext=".md",
            workspace_path="/workspace/skills")
        self.add_command("toggle", self.cmd_toggle, "<id>", (1, 1))
        self.add_command("grant", self.cmd_grant, "<id> <user|group> <principal_id> <read|write>", (4, 4))
        self.add_command("revoke", self.cmd_revoke, "<id>", (1, 1))

    def _build_update_payload(self, existing, item_id, content, meta):
        return {
            "id": item_id,
            "name": existing.get("name", meta.get("name", item_id)),
            "description": existing.get("description", meta.get("description", "")),
            "content": content,
            "meta": existing.get("meta") or {"tags": []},
            "is_active": existing.get("is_active", True),
        }

    def _build_create_payload(self, item_id, title, content, meta):
        return {
            "id": item_id,
            "name": title,
            "description": meta.get("description", ""),
            "content": content,
            "meta": {"tags": []},
            "is_active": True,
            "access_grants": [],
        }

    def cmd_pull(self, url: str, token: str, item_id: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))
            if r.status_code == 404:
                die(f"skill '{item_id}' not found")
            r.raise_for_status()
        s = r.json()
        sys.stdout.write(f"---\nid: {item_id}\nname: {s.get('name','')}\ndescription: {s.get('description','')}\n---\n")
        content = s.get("content", "")
        sys.stdout.write(content)
        if content and not content.endswith("\n"):
            sys.stdout.write("\n")

    def cmd_pull_all(self, url: str, token: str, out_dir: str = "."):
        """Pull all skills into <out_dir>/<id>/skill.md (with frontmatter) + meta.json."""
        with httpx.Client(timeout=TIMEOUT) as c:
            data = _get(c, url, f"{self.prefix}/", token).json()
        items = data if isinstance(data, list) else data.get("items", data.get("data", []))
        ids = sorted(item.get("id", "") for item in items)
        if not ids:
            out("(none)")
            return
        count = 0
        with httpx.Client(timeout=TIMEOUT) as c:
            for item_id in ids:
                r = c.get(_api(url, self.item_path(item_id)), headers=_headers(token))
                if r.status_code == 404:
                    print(f"  skip {item_id} (not found)", file=sys.stderr)
                    continue
                r.raise_for_status()
                item = r.json()
                item_dir = os.path.join(out_dir, item_id)
                # Write skill.md with frontmatter
                content = item.pop("content", "")
                fm = f"---\nid: {item_id}\nname: {item.get('name','')}\ndescription: {item.get('description','')}\n---\n"
                _write_file(os.path.join(item_dir, "skill.md"), fm + content + ("" if content.endswith("\n") else "\n"))
                # Write metadata
                _write_json(os.path.join(item_dir, "meta.json"), item)
                count += 1
                if not JSON_OUTPUT:
                    print(f"  {item_id}")
        out(f"pulled {count} skills")

    def cmd_toggle(self, url: str, token: str, skill_id: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            r = _post(c, url, f"{self.item_path(skill_id)}/toggle", token)
        s = r.json()
        out(f"{skill_id} {'active' if s.get('is_active') else 'inactive'}")

    def cmd_grant(self, url: str, token: str, skill_id: str, ptype: str, pid: str, perm: str):
        if ptype not in ("user", "group"):
            die("principal_type must be user or group")
        if perm not in ("read", "write"):
            die("permission must be read or write")
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(skill_id)), headers=_headers(token))
            if r.status_code == 404:
                die(f"skill '{skill_id}' not found")
            r.raise_for_status()
            grants = r.json().get("access_grants") or []
            grants.append({"resource_type": "skill", "resource_id": skill_id,
                          "principal_type": ptype, "principal_id": pid, "permission": perm})
            _post(c, url, f"{self.item_path(skill_id)}/access/update", token, {"access_grants": grants})
        out(f"granted {perm} on {skill_id} to {ptype}:{pid}")

    def cmd_revoke(self, url: str, token: str, skill_id: str):
        with httpx.Client(timeout=TIMEOUT) as c:
            r = c.get(_api(url, self.item_path(skill_id)), headers=_headers(token))
            if r.status_code == 404:
                die(f"skill '{skill_id}' not found")
            r.raise_for_status()
            n = len(r.json().get("access_grants") or [])
            _post(c, url, f"{self.item_path(skill_id)}/access/update", token, {"access_grants": []})
        out(f"revoked {n} grant(s) from {skill_id}")


skills_res = SkillsResource()

# ── models (special: uses /model?id= not /{id}, mutations via POST) ───

def models_list(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/models", token).json().get("data", [])
    rows = [{"id": m.get("id",""), "name": m.get("name",""),
             "base": (m.get("info") or {}).get("base_model_id", m.get("owned_by",""))}
            for m in sorted(data, key=lambda m: m.get("id",""))]
    out_table(rows, [("ID","id",10), ("NAME","name",20), ("BASE","base",15)])

def models_show(url, token, model_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _get(c, url, f"/api/v1/models/model?id={model_id}", token)
    m = r.json()
    if JSON_OUTPUT:
        out(m)
        return
    info = m.get("info") or {}
    meta = info.get("meta") or {}
    params = info.get("params") or {}
    pairs = [("id", m.get("id","")), ("name", m.get("name","")),
             ("base", info.get("base_model_id","(none)")),
             ("active", str(info.get("is_active","?"))),
             ("tools", ", ".join(meta.get("toolIds") or []) or "(none)"),
             ("filters", ", ".join(params.get("filter_ids") or []) or "(none)"),
             ("knowledge", ", ".join(k.get("name","?") for k in (meta.get("knowledge") or [])) or "(none)"),
             ("system", f"{len(params.get('system',''))} chars"),
             ("grants", str(len(info.get("access_grants") or [])))]
    out_kv(pairs)

def models_create(url, token, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, "/api/v1/models/create", token, payload)
    m = r.json()
    out(f"created {m.get('id')}")

def models_update(url, token, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, "/api/v1/models/model/update", token, payload)
    out(f"updated {r.json().get('id')}")

def models_delete(url, token, model_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, "/api/v1/models/model/delete", token, {"id": model_id})
    out(f"deleted {model_id}")

def _models_fetch(c, url, token, model_id):
    """Fetch a model by ID, returning the parsed JSON."""
    r = _get(c, url, f"/api/v1/models/model?id={model_id}", token)
    return r.json()

def models_set_tools(url, token, model_id, *tool_ids):
    """Set the tool bindings for a workspace model (pass no IDs to clear)."""
    with httpx.Client(timeout=TIMEOUT) as c:
        model = _models_fetch(c, url, token, model_id)
        info = model.get("info") or {}
        meta = info.setdefault("meta", {})
        params = info.setdefault("params", {})
        ids = list(tool_ids)
        meta["toolIds"] = ids
        # keep params.tool_ids in sync (used by some OWUI versions)
        params["tool_ids"] = ids
        model["info"] = info
        r = _post(c, url, "/api/v1/models/model/update", token, model)
    label = ", ".join(ids) if ids else "(none)"
    out(f"tools for {model_id}: {label}")

def models_set_filters(url, token, model_id, *filter_ids):
    """Set the filter bindings for a workspace model (pass no IDs to clear)."""
    with httpx.Client(timeout=TIMEOUT) as c:
        model = _models_fetch(c, url, token, model_id)
        info = model.get("info") or {}
        params = info.setdefault("params", {})
        ids = list(filter_ids)
        params["filter_ids"] = ids
        model["info"] = info
        r = _post(c, url, "/api/v1/models/model/update", token, model)
    label = ", ".join(ids) if ids else "(none)"
    out(f"filters for {model_id}: {label}")


def models_pull_all(url, token, out_dir="."):
    """Pull all workspace models into <out_dir>/<id>/model.json, extracting profile images."""
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/models", token).json().get("data", [])

    # Filter to workspace models: those with a base_model_id in their info
    # (raw connection proxies have no info or no base_model_id)
    workspace = []
    for m in data:
        info = m.get("info") or {}
        base = info.get("base_model_id", "")
        if base:
            workspace.append(m)

    if not workspace:
        out("(no workspace models)")
        return

    count = 0
    with httpx.Client(timeout=TIMEOUT) as c:
        for m in sorted(workspace, key=lambda m: m.get("id", "")):
            model_id = m.get("id", "")
            # Fetch full model data
            r = _get(c, url, f"/api/v1/models/model?id={model_id}", token)
            model = r.json()
            model_dir = os.path.join(out_dir, model_id)

            # Extract profile image from data URI (top-level meta, not info.meta)
            top_meta = model.get("meta") or {}
            img_url = top_meta.get("profile_image_url", "")
            extracted = _extract_data_uri(img_url)
            if extracted:
                ext, img_bytes = extracted
                _write_file(os.path.join(model_dir, f"profile.{ext}"), img_bytes)
                top_meta["profile_image_url"] = f"profile.{ext}"

            _write_json(os.path.join(model_dir, "model.json"), model)
            count += 1
            if not JSON_OUTPUT:
                img_note = f" +profile.{ext}" if extracted else ""
                print(f"  {model_id}{img_note}")

    out(f"pulled {count} models")


# ── knowledge (special: files subresource, file/remove is destructive) ─

def knowledge_list(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/knowledge/", token).json()
    kbs = data if isinstance(data, list) else data.get("items", [])
    rows = [{"id": k.get("id",""), "name": k.get("name",""),
             "desc": (k.get("description") or "")[:50]}
            for k in sorted(kbs, key=lambda k: k.get("name",""))]
    out_table(rows, [("ID","id",36), ("NAME","name",20), ("DESC","desc",20)])

def knowledge_show(url, token, kb_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/knowledge/{kb_id}"), headers=_headers(token))
        if r.status_code == 404: die(f"kb '{kb_id}' not found")
        r.raise_for_status()
    kb = r.json()
    if JSON_OUTPUT: out(kb); return
    out_kv([("id", kb.get("id","")), ("name", kb.get("name","")),
            ("description", kb.get("description") or "(none)"),
            ("grants", str(len(kb.get("access_grants") or [])))])

def knowledge_files(url, token, kb_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/knowledge/{kb_id}/files"), headers=_headers(token))
        if r.status_code == 404: die(f"kb '{kb_id}' not found")
        r.raise_for_status()
    items = r.json().get("items", [])
    rows = [{"id": f.get("id",""), "name": (f.get("meta") or {}).get("name") or f.get("filename","")}
            for f in items]
    out_table(rows, [("FILE_ID","id",36), ("NAME","name",30)])

def knowledge_create(url, token, name, description=""):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, "/api/v1/knowledge/create", token, {"name": name, "description": description})
    out(f"created {r.json().get('id')}")

def knowledge_delete(url, token, kb_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _delete(c, url, f"/api/v1/knowledge/{kb_id}/delete", token)
    out(f"deleted {kb_id}")

def knowledge_add_file(url, token, kb_id, file_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/knowledge/{kb_id}/file/add", token, {"file_id": file_id})
    out(f"added {file_id} to {kb_id}")

def knowledge_remove_file(url, token, kb_id, file_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/knowledge/{kb_id}/file/remove", token, {"file_id": file_id})
    out(f"removed {file_id} from {kb_id} (file destroyed)")


# ── files ─────────────────────────────────────────────────────────────

def files_list(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/files/", token).json()
    rows = [{"id": f.get("id",""),
             "name": (f.get("meta") or {}).get("name") or f.get("filename",""),
             "size": str((f.get("meta") or {}).get("size","?"))}
            for f in data]
    out_table(rows, [("FILE_ID","id",36), ("NAME","name",30), ("SIZE","size",8)])

def files_show(url, token, file_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/files/{file_id}"), headers=_headers(token))
        if r.status_code == 404: die(f"file '{file_id}' not found")
        r.raise_for_status()
    f = r.json()
    if JSON_OUTPUT: out(f); return
    meta = f.get("meta") or {}
    out_kv([("id", f.get("id","")), ("name", meta.get("name") or f.get("filename","")),
            ("size", f"{meta.get('size','?')} bytes"), ("type", meta.get("content_type","?"))])

def files_upload(url, token, path, mime_type=""):
    import mimetypes
    if not mime_type:
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = path.rsplit("/", 1)[-1]
    with open(path, "rb") as f:
        data = f.read()
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(_api(url, "/api/v1/files/"), headers={"Authorization": f"Bearer {token}"},
                   files={"file": (filename, data, mime_type)})
        r.raise_for_status()
    out(f"uploaded {filename} -> {r.json().get('id')}")

def files_delete(url, token, file_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _delete(c, url, f"/api/v1/files/{file_id}", token)
    out(f"deleted {file_id}")


# ── groups (special: /id/{id} pattern, members subresource) ──────────

def groups_list(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/groups/", token).json()
    rows = [{"id": g.get("id",""), "name": g.get("name",""),
             "members": str(g.get("member_count","?"))}
            for g in sorted(data, key=lambda g: g.get("name",""))]
    out_table(rows, [("ID","id",36), ("NAME","name",20), ("MEMBERS","members",7)])

def groups_show(url, token, group_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/groups/id/{group_id}"), headers=_headers(token))
        if r.status_code == 404: die(f"group '{group_id}' not found")
        r.raise_for_status()
    g = r.json()
    if JSON_OUTPUT: out(g); return
    out_kv([("id", g.get("id","")), ("name", g.get("name","")),
            ("description", g.get("description") or "(none)"),
            ("members", str(g.get("member_count","?")))])

def groups_create(url, token, name, description=""):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, "/api/v1/groups/create", token, {"name": name, "description": description})
    out(f"created {r.json().get('id')}")

def groups_delete(url, token, group_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _delete(c, url, f"/api/v1/groups/id/{group_id}/delete", token)
    out(f"deleted {group_id}")

def groups_members(url, token, group_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        users = _post(c, url, f"/api/v1/groups/id/{group_id}/users", token).json()
    rows = [{"id": u.get("id",""), "name": u.get("name",""), "email": u.get("email","")}
            for u in sorted(users, key=lambda u: u.get("name",""))]
    out_table(rows, [("USER_ID","id",36), ("NAME","name",20), ("EMAIL","email",30)])

def groups_add_user(url, token, group_id, user_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/groups/id/{group_id}/users/add", token, {"user_ids": [user_id]})
    out(f"added {user_id} to {group_id}")

def groups_remove_user(url, token, group_id, user_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/groups/id/{group_id}/users/remove", token, {"user_ids": [user_id]})
    out(f"removed {user_id} from {group_id}")

def groups_update(url, token, group_id, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/groups/id/{group_id}/update", token, payload)
    out(f"updated {group_id}")


# ── users ─────────────────────────────────────────────────────────────

def _users_all_pages(url, token):
    users, page = [], 1
    with httpx.Client(timeout=TIMEOUT) as c:
        while True:
            data = _get(c, url, f"/api/v1/users/?page={page}", token).json()
            batch = data.get("users", [])
            users.extend(batch)
            if len(users) >= data.get("total", 0) or not batch:
                break
            page += 1
    return users

def users_list(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/users/?page=1", token).json()
    users = data.get("users", [])
    total = data.get("total", 0)
    rows = [{"id": u.get("id",""), "name": u.get("name",""),
             "email": u.get("email",""), "role": u.get("role","")}
            for u in users]
    out_table(rows, [("USER_ID","id",36), ("NAME","name",20), ("EMAIL","email",30), ("ROLE","role",5)])
    if not JSON_OUTPUT and total > len(users):
        print(f"({len(users)}/{total} — use 'users find' for all)")

def users_find(url, token, query):
    all_users = _users_all_pages(url, token)
    q = query.lower()
    matches = [u for u in all_users if q in u.get("email","").lower() or q in u.get("name","").lower()]
    rows = [{"id": u.get("id",""), "name": u.get("name",""), "email": u.get("email",""), "role": u.get("role","")}
            for u in matches]
    out_table(rows, [("USER_ID","id",36), ("NAME","name",20), ("EMAIL","email",30), ("ROLE","role",5)])
    if not JSON_OUTPUT:
        print(f"({len(matches)}/{len(all_users)})")

def users_show(url, token, id_or_email):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/users/{id_or_email}"), headers=_headers(token))
        if r.status_code == 404 or not r.json().get("id"):
            all_u = _users_all_pages(url, token)
            user = next((u for u in all_u if u.get("email","").lower() == id_or_email.lower()), None)
            if not user: die(f"user '{id_or_email}' not found")
        else:
            r.raise_for_status()
            user = r.json()
    if JSON_OUTPUT: out(user); return
    out_kv([("id", user.get("id","")), ("name", user.get("name","")),
            ("email", user.get("email","")), ("role", user.get("role",""))])

def users_add(url, token, email, name, role="user"):
    if role not in ("user", "admin"): die("role must be user or admin")
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, "/api/v1/auths/add", token, {"email": email, "name": name, "role": role, "password": "placeholder-no-login"})
    out(f"created {r.json().get('id')} {email}")

def users_delete(url, token, user_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _delete(c, url, f"/api/v1/users/{user_id}", token)
    out(f"deleted {user_id}")

def users_update(url, token, user_id, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/users/{user_id}/update", token, payload)
    out(f"updated {user_id}")


# ── chats (special: tree structure) ──────────────────────────────────

def chats_list(url, token, page="1"):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, f"/api/v1/chats/?page={page}", token).json()
    items = data if isinstance(data, list) else data.get("data", data.get("items", []))
    rows = [{"id": ch.get("id",""), "title": (ch.get("chat",{}).get("title","") or ch.get("title",""))[:50],
             "updated": str(ch.get("updated_at",""))[:10]}
            for ch in items]
    out_table(rows, [("CHAT_ID","id",36), ("TITLE","title",40), ("UPDATED","updated",10)])

def chats_search(url, token, query, page="1"):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, f"/api/v1/chats/search?text={query}&page={page}", token).json()
    items = data if isinstance(data, list) else data.get("data", data.get("items", []))
    rows = [{"id": ch.get("id",""), "title": (ch.get("chat",{}).get("title","") or ch.get("title",""))[:50]}
            for ch in items]
    out_table(rows, [("CHAT_ID","id",36), ("TITLE","title",50)])

def chats_show(url, token, chat_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/chats/{chat_id}"), headers=_headers(token))
        if r.status_code == 404: die(f"chat '{chat_id}' not found")
        r.raise_for_status()
    chat = r.json()
    if JSON_OUTPUT:
        out(chat)
        return
    inner = chat.get("chat", {})
    history = inner.get("history", {})
    msgs = history.get("messages", {})
    current = history.get("currentId", "")

    print(f"chat: {inner.get('title','(untitled)')}")
    print(f"id: {chat_id}")
    print(f"models: {', '.join(inner.get('models',[])) or '?'}")
    print(f"nodes: {len(msgs)}")
    if not msgs:
        return

    children_of = {}
    roots = []
    for mid, msg in msgs.items():
        p = msg.get("parentId")
        if not p:
            roots.append(mid)
        else:
            children_of.setdefault(p, []).append(mid)

    def walk(nid, depth=0):
        msg = msgs.get(nid, {})
        role = msg.get("role", "?")
        content = msg.get("content", "")
        kids = children_of.get(nid, [])
        # Strip tool call details blocks
        display = re.sub(r"<details[^>]*>.*?</details>", "", content, flags=re.DOTALL).strip()
        display = re.sub(r"\n{3,}", "\n\n", display)
        indent = "  " * depth
        marker = " *" if nid == current else ""
        branch = f" [{len(kids)}br]" if len(kids) > 1 else ""
        print(f"{indent}--- {role.upper()}{marker}{branch} ({nid[:8]})")
        if display:
            for line in display[:300].splitlines():
                print(f"{indent}  {line}")
            if len(display) > 300:
                print(f"{indent}  ...({len(display)} chars)")
        for kid in kids:
            walk(kid, depth + (1 if len(kids) > 1 else 0))

    for root in roots:
        walk(root)

def chats_delete(url, token, chat_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _delete(c, url, f"/api/v1/chats/{chat_id}", token)
    out(f"deleted {chat_id}")


# ── configs ──────────────────────────────────────────────────────────

def configs_show(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _get(c, url, "/api/v1/configs/export", token)
    out(r.json())

def configs_get(url, token, section):
    """Get a specific config section (connections, code_execution, models, tool_servers, etc.)."""
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _get(c, url, f"/api/v1/configs/{section}", token)
    out(r.json())

def configs_set(url, token, section, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, f"/api/v1/configs/{section}", token, payload)
    out(f"updated {section}")

def configs_admin(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _get(c, url, "/api/v1/auths/admin/config", token)
    out(r.json())

def configs_admin_set(url, token, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        _post(c, url, "/api/v1/auths/admin/config", token, payload)
    out("updated admin config")


# ── prompts ──────────────────────────────────────────────────────────

def prompts_list(url, token):
    with httpx.Client(timeout=TIMEOUT) as c:
        data = _get(c, url, "/api/v1/prompts/", token).json()
    rows = [{"command": p.get("command",""), "title": p.get("title","")[:40],
             "id": p.get("id","")}
            for p in sorted(data, key=lambda p: p.get("command",""))]
    out_table(rows, [("COMMAND","command",15), ("TITLE","title",30), ("ID","id",36)])

def prompts_show(url, token, prompt_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(_api(url, f"/api/v1/prompts/id/{prompt_id}"), headers=_headers(token))
        if r.status_code == 404: die(f"prompt '{prompt_id}' not found")
        r.raise_for_status()
    p = r.json()
    if JSON_OUTPUT: out(p); return
    out_kv([("id", p.get("id","")), ("command", p.get("command","")),
            ("title", p.get("title","")), ("content", f"{len(p.get('content',''))} chars")])

def prompts_create(url, token, json_path):
    with open(json_path) as f:
        payload = json.load(f)
    with httpx.Client(timeout=TIMEOUT) as c:
        r = _post(c, url, "/api/v1/prompts/create", token, payload)
    out(f"created {r.json().get('id')}")

def prompts_delete(url, token, prompt_id):
    with httpx.Client(timeout=TIMEOUT) as c:
        _delete(c, url, f"/api/v1/prompts/id/{prompt_id}/delete", token)
    out(f"deleted {prompt_id}")


# ── schema introspection ─────────────────────────────────────────────

def cmd_schema(args):
    """Query the bundled API schema reference."""
    if not SCHEMA_PATH.is_file():
        die(f"schema not found at {SCHEMA_PATH}")
    with SCHEMA_PATH.open() as f:
        schema = json.load(f)

    if not args:
        # List all resources
        for key, val in schema.items():
            if key == "_meta":
                continue
            prefix = val.get("prefix", "")
            n = len(val.get("endpoints", []))
            print(f"  {key:<14} {prefix:<30} {n} endpoints")
        return

    resource = args[0]
    if resource not in schema or resource == "_meta":
        die(f"unknown resource '{resource}'. Run 'schema' for list.")

    res = schema[resource]
    note = res.get("note", "")
    if note:
        print(f"# {note}")

    if len(args) == 1:
        # Show all endpoints for resource
        print(f"# {res['prefix']}")
        for ep in res["endpoints"]:
            method, path, auth, body, desc = ep[0], ep[1], ep[2], ep[3], ep[4]
            body_str = f"  body: {body}" if body else ""
            print(f"  {method:<6} {path:<45} [{auth}] {desc}{body_str}")
        return

    # Filter to matching method/path
    query = args[1].lower()
    matches = [ep for ep in res["endpoints"]
               if query in ep[1].lower() or query in ep[4].lower()]
    if not matches:
        die(f"no endpoints matching '{query}' in {resource}")
    print(f"# {res['prefix']}")
    for ep in matches:
        method, path, auth, body, desc = ep[0], ep[1], ep[2], ep[3], ep[4]
        print(f"  {method:<6} {path}")
        print(f"         auth: {auth}  {desc}")
        if body:
            print(f"         body: {body}")

# ── dispatch ──────────────────────────────────────────────────────────

# Build command table: {(resource, command): (fn, arg_spec, (min, max))}
COMMANDS: dict[tuple[str, str], tuple] = {}

# Register generic resources
for res in [tools_res, functions_res, skills_res]:
    for cmd_name, (fn, arg_spec, arg_range) in res.get_commands().items():
        COMMANDS[(res.name, cmd_name)] = (fn, arg_spec, arg_range)

# Register special resources
COMMANDS.update({
    ("tools",     "valves"):      (tools_valves_get,        "<id>",                  (1, 1)),
    ("tools",     "valves-spec"): (tools_valves_spec,       "<id>",                  (1, 1)),
    ("tools",     "valves-set"):  (tools_valves_set,        "<id> <valves.json>",    (2, 2)),
    ("tools",     "valves-set-field"):  (tools_valves_set_field,   "<id> <key> <value>",  (3, 3)),
    ("tools",     "valves-unset-field"):(tools_valves_unset_field, "<id> <key>",          (2, 2)),
    ("functions", "valves"):      (functions_valves_get,    "<id>",                  (1, 1)),
    ("functions", "valves-spec"): (functions_valves_spec,   "<id>",                  (1, 1)),
    ("functions", "valves-set"):  (functions_valves_set,    "<id> <valves.json>",    (2, 2)),
    ("functions", "valves-set-field"):  (functions_valves_set_field,   "<id> <key> <value>",  (3, 3)),
    ("functions", "valves-unset-field"):(functions_valves_unset_field, "<id> <key>",          (2, 2)),
    ("models",    "list"):        (models_list,          "",                    (0, 0)),
    ("models",    "show"):        (models_show,          "<id>",                (1, 1)),
    ("models",    "create"):      (models_create,        "<model.json>",        (1, 1)),
    ("models",    "update"):      (models_update,        "<model.json>",        (1, 1)),
    ("models",    "delete"):      (models_delete,        "<id>",                (1, 1)),
    ("models",    "set-tools"):   (models_set_tools,     "<id> [tool-id]...",   (1, 999)),
    ("models",    "set-filters"): (models_set_filters,   "<id> [filter-id]...", (1, 999)),
    ("models",    "pull-all"):    (models_pull_all,      "[dir]",               (0, 1)),
    ("knowledge", "list"):        (knowledge_list,       "",                    (0, 0)),
    ("knowledge", "show"):        (knowledge_show,       "<id>",                (1, 1)),
    ("knowledge", "files"):       (knowledge_files,      "<id>",                (1, 1)),
    ("knowledge", "create"):      (knowledge_create,     "<name> [desc]",       (1, 2)),
    ("knowledge", "delete"):      (knowledge_delete,     "<id>",                (1, 1)),
    ("knowledge", "add-file"):    (knowledge_add_file,   "<id> <file-id>",      (2, 2)),
    ("knowledge", "remove-file"): (knowledge_remove_file,"<id> <file-id>",      (2, 2)),
    ("files",     "list"):        (files_list,           "",                    (0, 0)),
    ("files",     "show"):        (files_show,           "<id>",                (1, 1)),
    ("files",     "upload"):      (files_upload,         "<path> [mime]",       (1, 2)),
    ("files",     "delete"):      (files_delete,         "<id>",                (1, 1)),
    ("groups",    "list"):        (groups_list,          "",                    (0, 0)),
    ("groups",    "show"):        (groups_show,          "<id>",                (1, 1)),
    ("groups",    "create"):      (groups_create,        "<name> [desc]",       (1, 2)),
    ("groups",    "delete"):      (groups_delete,        "<id>",                (1, 1)),
    ("groups",    "members"):     (groups_members,       "<id>",                (1, 1)),
    ("groups",    "add-user"):    (groups_add_user,      "<id> <user-id>",      (2, 2)),
    ("groups",    "remove-user"): (groups_remove_user,   "<id> <user-id>",      (2, 2)),
    ("groups",    "update"):      (groups_update,        "<id> <group.json>",   (2, 2)),
    ("chats",     "list"):        (chats_list,           "[page]",              (0, 1)),
    ("chats",     "search"):      (chats_search,         "<query> [page]",      (1, 2)),
    ("chats",     "show"):        (chats_show,           "<id>",                (1, 1)),
    ("chats",     "delete"):      (chats_delete,         "<id>",                (1, 1)),
    ("users",     "list"):        (users_list,           "",                    (0, 0)),
    ("users",     "find"):        (users_find,           "<query>",             (1, 1)),
    ("users",     "show"):        (users_show,           "<id-or-email>",       (1, 1)),
    ("users",     "add"):         (users_add,            "<email> <name> [role]",(2, 3)),
    ("users",     "delete"):      (users_delete,         "<id>",                (1, 1)),
    ("users",     "update"):      (users_update,         "<id> <user.json>",    (2, 2)),
    ("configs",   "show"):        (configs_show,         "",                    (0, 0)),
    ("configs",   "get"):         (configs_get,          "<section>",           (1, 1)),
    ("configs",   "set"):         (configs_set,          "<section> <data.json>",(2, 2)),
    ("configs",   "admin"):       (configs_admin,        "",                    (0, 0)),
    ("configs",   "admin-set"):   (configs_admin_set,    "<data.json>",         (1, 1)),
    ("prompts",   "list"):        (prompts_list,         "",                    (0, 0)),
    ("prompts",   "show"):        (prompts_show,         "<id>",                (1, 1)),
    ("prompts",   "create"):      (prompts_create,       "<prompt.json>",       (1, 1)),
    ("prompts",   "delete"):      (prompts_delete,       "<id>",                (1, 1)),
})


def cmd_help():
    """Print all commands grouped by resource."""
    # Group by resource preserving insertion order
    resources = {}
    for (res, cmd), (_, arg_spec, _) in COMMANDS.items():
        resources.setdefault(res, []).append((cmd, arg_spec))
    for res, cmds in resources.items():
        for cmd, arg_spec in cmds:
            print(f"  {res:<12} {cmd:<15} {arg_spec}")


def main():
    global JSON_OUTPUT
    args = sys.argv[1:]

    # Extract --json flag
    if "--json" in args:
        JSON_OUTPUT = True
        args.remove("--json")

    if "--version" in args:
        print(f"owui-cli {owui_cli.__version__}")
        return

    if not args:
        cmd_help()
        sys.exit(1)

    # Top-level commands
    if args[0] == "help":
        cmd_help()
        return
    if args[0] == "schema":
        cmd_schema(args[1:])
        return

    if len(args) < 2:
        cmd_help()
        sys.exit(1)

    url, token = _env()
    resource, command = args[0], args[1]
    key = (resource, command)

    # Subcommands are plural (chats, models, etc.) to mirror the OWUI API
    # paths (/api/v1/chats/, /api/v1/models, ...), not typical CLI convention.
    if key not in COMMANDS:
        plural = resource + "s"
        if (plural, command) in COMMANDS:
            die(f"resources are plural: use '{plural} {command}' not '{resource} {command}'")
        die(f"unknown: {resource} {command}")

    fn, arg_spec, (min_args, max_args) = COMMANDS[key]
    rest = args[2:]

    if not (min_args <= len(rest) <= max_args):
        die(f"usage: owui-cli {resource} {command} {arg_spec}")

    try:
        fn(url, token, *rest)
    except httpx.HTTPStatusError as e:
        die(f"HTTP {e.response.status_code}: {e.response.text[:200]}")
    except FileNotFoundError as e:
        die(f"file not found: {e}")


if __name__ == "__main__":
    main()
