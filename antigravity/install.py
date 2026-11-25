#!/usr/bin/env python3
"""
将 Codex-MCP 安装为 Antigravity 全局插件（~/.gemini/config/plugins/codex-sub/）。

只写入清单、MCP/hook 指针配置和规则文件，server.py 与 antigravity/hook.py 仍直接从本仓库运行，
不复制业务代码。卸载：python antigravity/install.py --uninstall
"""

import json
import shutil
import sys
from pathlib import Path

AGY_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGY_DIR.parent
PLUGIN_DIR = Path.home() / ".gemini" / "config" / "plugins" / "codex-sub"


def install() -> None:
    rules_dir = PLUGIN_DIR / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(AGY_DIR / "plugin.json", PLUGIN_DIR / "plugin.json")
    shutil.copyfile(AGY_DIR / "AGENTS.md", rules_dir / "AGENTS.md")

    server_py = str(REPO_ROOT / "server.py")
    hook_py = str(AGY_DIR / "hook.py")

    mcp_config = {
        "mcpServers": {
            "codex": {
                "command": "python",
                "args": [server_py, "--client", "antigravity"],
            }
        }
    }
    (PLUGIN_DIR / "mcp_config.json").write_text(
        json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    hooks_config = {
        "codex-sub": {
            "PreInvocation": [
                {"type": "command", "command": f'python "{hook_py}" PreInvocation', "timeout": 10}
            ],
            "Stop": [
                {"type": "command", "command": f'python "{hook_py}" Stop', "timeout": 10}
            ],
        }
    }
    (PLUGIN_DIR / "hooks.json").write_text(
        json.dumps(hooks_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Installed Antigravity plugin to {PLUGIN_DIR}")
    print(f"  server: {server_py} --client antigravity")
    print(f"  hook:   {hook_py}")


def uninstall() -> None:
    files = [
        PLUGIN_DIR / "plugin.json",
        PLUGIN_DIR / "mcp_config.json",
        PLUGIN_DIR / "hooks.json",
        PLUGIN_DIR / "rules" / "AGENTS.md",
    ]
    for f in files:
        if f.exists():
            f.unlink()
    for d in (PLUGIN_DIR / "rules", PLUGIN_DIR):
        if d.exists():
            try:
                d.rmdir()
            except OSError:
                pass
    print(f"Uninstalled Antigravity plugin from {PLUGIN_DIR}")


if __name__ == "__main__":
    if "--uninstall" in sys.argv[1:]:
        uninstall()
    else:
        install()
