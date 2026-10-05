"""Structural guarantee: the replay path cannot reach the LLM.

1. Static: no module under cua.replay - nor anything it transitively imports from cua -
   imports anthropic or cua.agent.
2. Dynamic: importing and constructing the engine in a fresh interpreter loads neither.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
FORBIDDEN = ("anthropic", "cua.agent")


def imports_of(path: Path) -> set[str]:
    mods = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def module_file(mod: str) -> Path | None:
    p = SRC / Path(*mod.split("."))
    if (p / "__init__.py").exists():
        return p / "__init__.py"
    if p.with_suffix(".py").exists():
        return p.with_suffix(".py")
    return None


def test_replay_import_closure_excludes_llm():
    todo = [f"cua.replay.{p.stem}" for p in (SRC / "cua/replay").glob("*.py")]
    seen: set[str] = set()
    while todo:
        mod = todo.pop()
        if mod in seen:
            continue
        seen.add(mod)
        f = module_file(mod)
        if f is None:
            continue
        for imp in imports_of(f):
            assert not imp.startswith(FORBIDDEN), f"{mod} imports {imp}"
            if imp.startswith("cua"):
                todo.append(imp)
    assert "cua.replay.engine" in seen and "cua.surface.playwright_web" in seen


def test_runtime_engine_does_not_load_llm():
    code = (
        "import sys; from cua.replay.engine import ReplayEngine; from cua.cli import replay; "
        "bad=[m for m in sys.modules if m.startswith(('anthropic','cua.agent'))]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
