"""Fails if any engine/ module imports api, store, security, control, time, random, os, or network libs,
or calls the clock, eval/exec, open(), float() or round()."""
import ast
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[2] / "src/monetize360/engine"
BANNED_MODULES = {
    "os", "sys", "time", "random", "secrets", "socket", "subprocess", "threading", "asyncio", "io", "pathlib",
    "shutil", "tempfile", "urllib", "http", "requests", "httpx", "redis", "psycopg", "sqlite3", "json",
    "holidays", "celpy", "monetize360.api", "monetize360.store", "monetize360.security", "monetize360.control",
    "monetize360.workers", "monetize360.config",
}
BANNED_BUILTINS = {"eval", "exec", "open", "float", "round", "__import__", "compile", "input"}
BANNED_METHODS = {"now", "utcnow", "today"}          # datetime.now() & co: the clock


def files():
    return sorted(ENGINE.glob("*.py"))


@pytest.mark.parametrize("path", files(), ids=lambda p: p.name)
def test_engine_module_is_pure(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods = [node.module or ""]
        else:
            mods = []
        for m in mods:
            if any(m == b or m.startswith(b + ".") for b in BANNED_MODULES):
                problems.append(f"line {node.lineno}: imports {m}")
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BANNED_BUILTINS | BANNED_METHODS:
                problems.append(f"line {node.lineno}: calls {f.id}()")
            if isinstance(f, ast.Attribute) and f.attr in BANNED_METHODS:
                problems.append(f"line {node.lineno}: calls .{f.attr}()")
    assert problems == []
