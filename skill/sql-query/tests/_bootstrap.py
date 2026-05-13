from __future__ import annotations

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_ROOT.parents[1]
RUNTIME_PATH = SKILL_ROOT / "runtime"

for path in (REPO_ROOT, RUNTIME_PATH):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
