"""Prints git diff --stat for src/monetize360/engine/ - the zero-engine-change proof for the demo.

    python scripts/check_engine_frozen.py            # compared with origin/main
    python scripts/check_engine_frozen.py v1.0       # compared with any commit, tag or branch

Shows what changed in the engine since that point (nothing = frozen) next to what changed in packs/,
so "we added a domain and the engine diff is empty" is one command. Exit code 1 if the engine changed."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE, PACKS = "src/monetize360/engine", "packs/templates"


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)   # noqa: S603, S607
    if done.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def main(base: str) -> int:
    git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    # against the working tree, so uncommitted edits count too; plus files git doesn't track yet
    engine = git("diff", "--stat", base, "--", ENGINE)
    new_engine = git("ls-files", "--others", "--exclude-standard", "--", ENGINE)
    packs = git("diff", "--stat", base, "--", PACKS)
    new_packs = git("ls-files", "--others", "--exclude-standard", "--", PACKS)

    print(f"compared with {base} ({git('rev-parse', '--short', base)})\n")
    print(f"{ENGINE}/")
    changed = bool(engine or new_engine)
    shown = f"{engine}\n{new_engine}".strip().replace("\n", "\n   ")
    print("   " + (shown if changed else "no changes  <- the engine is frozen"))
    print(f"\n{PACKS}/")
    print("   " + (f"{packs}\n{new_packs}".strip().replace("\n", "\n   ") or "no changes"))
    return 1 if changed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "origin/main"))
