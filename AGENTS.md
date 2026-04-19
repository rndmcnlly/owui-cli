# AGENTS.md — owui-cli

## Version management

The version number appears in two places and **must be kept in sync**:

- `pyproject.toml` → `version = "X.Y.Z"`
- `src/owui_cli/__init__.py` → `__version__ = "X.Y.Z"`

Bump both when cutting a release.

## Publishing

Every push to `main` auto-publishes to PyPI via GitHub Actions trusted publishing. Bump the version before pushing, and don't push broken code to main.

## Local development install

After making changes, reinstall locally with `uv tool install --force .` so your modified `owui-cli` is the one on your PATH.

## Testing before committing

Always verify changes work against a **production Open WebUI server** before committing. Set `OWUI_URL` and `OWUI_TOKEN` and run the relevant commands to confirm they succeed with real API responses — don't rely on local-only reasoning.

## Architecture

Single-file CLI in `src/owui_cli/cli.py`. The `Resource` base class handles generic CRUD (list, show, deploy, pull, pull-all, delete) for tools, functions, and skills. Special resources (models, knowledge, files, groups, users, chats, configs, prompts) use standalone functions. All commands are registered in the `COMMANDS` dispatch table at the bottom of the file.

## API endpoint patterns

The Open WebUI API is not fully consistent across resources:

- **Tools/functions/skills:** `/api/v1/{resource}/id/{id}`
- **Models:** `/api/v1/models/model?id={id}`
- **Knowledge/users:** `/api/v1/{resource}/{id}`
- **Groups:** `/api/v1/groups/id/{id}`

Always verify the actual endpoint against a running instance when adding new commands.

## Output conventions

All commands support `--json` for machine-readable output. New commands should use the existing output helpers which handle JSON mode automatically:

- `out(data)` — generic output (JSON in `--json` mode, pretty-printed otherwise)
- `out_table(rows, cols)` — aligned columnar table
- `out_kv(pairs)` — key-value display

## Schema updates

`update-schema.py` fetches OWUI router sources from GitHub and generates a prompt to feed an LLM. The LLM output goes in `src/owui_cli/data/api-schema.json`. See the script header for the full workflow.
