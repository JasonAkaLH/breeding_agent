#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.integrations.agent_skills.bundle_digest import (  # noqa: E402
    ProjectSkillBundleDigestError,
    compute_project_skill_bundle_digest,
    validate_project_skill_bundle_digest,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute or validate a Project Skill bundle digest."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--expected")
    args = parser.parse_args(argv)
    try:
        if args.expected is None:
            result = compute_project_skill_bundle_digest(args.root)
            status = "reported"
        else:
            result = validate_project_skill_bundle_digest(args.root, args.expected)
            status = "valid"
    except ProjectSkillBundleDigestError as exc:
        print(
            json.dumps(
                {"code": exc.code, "reason": exc.reason, "status": "failed"},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "digest": result.digest,
                "duration_ms": result.duration_ms,
                "file_count": result.file_count,
                "status": status,
                "total_bytes": result.total_bytes,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
