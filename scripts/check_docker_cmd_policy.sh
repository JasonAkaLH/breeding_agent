#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if git ls-files --error-unmatch -- docker_cmd.md >/dev/null 2>&1; then
  echo "ERROR: docker_cmd.md must remain local-only and must not be tracked by Git." >&2
  exit 1
fi

if ! grep -Fxq '/docker_cmd.md' .gitignore; then
  echo "ERROR: .gitignore must contain the root-only /docker_cmd.md rule." >&2
  exit 1
fi

if ! git check-ignore --no-index -q -- docker_cmd.md; then
  echo "ERROR: docker_cmd.md is not ignored by the effective Git rules." >&2
  exit 1
fi

echo "docker_cmd.md policy OK: local-only, ignored, and untracked."
