# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Fetch Open WebUI router sources and emit an LLM prompt to regenerate api-schema.json.

Usage:
    uv run --script update-schema.py [tag-or-branch]

Default branch: main. Pass a tag like "v0.8.12" to pin to a release.

This fetches all backend router files from GitHub, then prints a self-contained
prompt to stdout. Pipe it to an LLM (or paste into a session) to produce the
updated api-schema.json.

Example workflow:
    uv run --script update-schema.py v0.9.0 > /tmp/schema-prompt.txt
    # Feed /tmp/schema-prompt.txt to your preferred LLM
    # Save the JSON output to src/owui_cli/data/api-schema.json
    # Update _meta.source and _meta.extracted fields
"""

import sys
import httpx

ROUTERS = [
    "configs", "groups", "chats", "prompts", "users", "auths",
    "models", "tools", "functions", "knowledge", "files", "skills",
    "memories", "channels", "folders", "notes", "evaluations",
]

BASE_URL = "https://raw.githubusercontent.com/open-webui/open-webui"
ROUTER_PATH = "backend/open_webui/routers"

SCHEMA_FORMAT = """\
{
  "_meta": {
    "source": "open-webui/open-webui <REF>",
    "extracted": "<DATE>",
    "auth_levels": {"A": "admin", "V": "verified user", "C": "any authenticated", "N": "no auth"},
    "format": "[METHOD, path, auth, body_or_null, description]"
  },
  "<resource>": {
    "prefix": "/api/v1/<resource>",
    "note": "optional note about quirks",
    "endpoints": [
      ["GET", "/", "V", null, "List items"],
      ["POST", "/create", "A", "{name,description?}", "Create item"],
      ...
    ]
  },
  ...
}"""


def fetch_routers(ref: str) -> dict[str, str]:
    """Fetch all router source files. Returns {name: source_code}."""
    sources = {}
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for name in ROUTERS:
            url = f"{BASE_URL}/{ref}/{ROUTER_PATH}/{name}.py"
            print(f"  fetching {name}.py ...", file=sys.stderr)
            r = client.get(url)
            if r.status_code == 404:
                print(f"  WARNING: {name}.py not found at {ref}", file=sys.stderr)
                continue
            r.raise_for_status()
            sources[name] = r.text
    return sources


def emit_prompt(ref: str, sources: dict[str, str]):
    """Print the LLM prompt to stdout."""
    print(f"""\
I need you to produce a JSON file called api-schema.json by reading the Open WebUI
router source files below. This schema is used by a CLI tool for agent-driven API
introspection.

## Output format

Emit ONLY valid JSON matching this structure (no markdown fences, no commentary):

{SCHEMA_FORMAT}

## Rules

1. Every @router.get, @router.post, @router.delete, @router.put, @router.patch
   decorator becomes one entry in the endpoints array.
2. Path is relative to the router prefix (e.g. "/" not "/api/v1/tools/").
3. Auth level is determined by the dependency:
   - get_admin_user -> "A"
   - get_verified_user -> "V"
   - get_current_user -> "C"
   - no auth dependency -> "N"
4. Body description is a compact string like "{{name,description?}}" showing the
   Pydantic model fields. Use ? for Optional fields. null if no request body.
5. Include a "note" field on resources with path quirks (e.g. /id/{{id}} vs /{{id}},
   query params instead of path params, destructive operations).
6. Set _meta.source to "open-webui/open-webui {ref}"
7. Be exhaustive. Every endpoint. No omissions.

## Router source files ({len(sources)} files from {ref})
""")

    for name, source in sources.items():
        print(f"### {name}.py\n")
        print(f"```python\n{source}\n```\n")


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else "main"
    print(f"Fetching routers from {ref} ...", file=sys.stderr)
    sources = fetch_routers(ref)
    print(f"Fetched {len(sources)} router files.", file=sys.stderr)
    emit_prompt(ref, sources)
    print(f"\nPrompt written to stdout ({sum(len(s) for s in sources.values())} chars of source).", file=sys.stderr)
    print(f"Feed this to an LLM and save the JSON output to src/owui_cli/data/api-schema.json", file=sys.stderr)


if __name__ == "__main__":
    main()
