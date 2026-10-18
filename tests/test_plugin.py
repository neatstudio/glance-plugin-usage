"""Runs the glance plugin checker against this repository.

CI downloads `check_plugin.py` and `plugin.schema.json` into `.glance/`;
locally run `make check` or the commands in the README first.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / ".glance"


def test_plugin_conforms():
    checker = TOOLS / "check_plugin.py"
    schema = TOOLS / "plugin.schema.json"
    assert checker.exists() and schema.exists(), "download the checker first (see README)"
    result = subprocess.run([sys.executable, str(checker), str(ROOT), "--schema", str(schema)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
