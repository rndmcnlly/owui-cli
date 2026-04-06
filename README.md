# owui-cli

Admin CLI for [Open WebUI](https://github.com/open-webui/open-webui) instances.

Manage tools, functions, skills, models, users, groups, knowledge bases, files, configs, prompts, and chats from the command line. Designed for both human operators and AI agents.

```
uvx owui-cli users list
uvx owui-cli tools deploy my_toolkit.py
uvx owui-cli models show gpt-4o
uvx owui-cli schema knowledge
```

## Status

Under construction. Coming soon to PyPI.

## Auth

```bash
export OWUI_URL=https://your-open-webui-instance.example.com
export OWUI_TOKEN=your-admin-jwt
owui-cli help
```

## License

MIT
