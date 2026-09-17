#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ -n "$(git ls-files -- docker_cmd.md docker_cmd/)" ]]; then
  echo "ERROR: deployment command files must remain local-only and must not be tracked by Git." >&2
  exit 1
fi

for rule in '/docker_cmd.md' '/docker_cmd/'; do
  if ! grep -Fxq "$rule" .gitignore; then
    echo "ERROR: .gitignore must contain the $rule rule." >&2
    exit 1
  fi
done

for deployment_file in docker_cmd.md docker_cmd/docker_cmd_dev.md docker_cmd/docker_cmd_prod.md; do
  if ! git check-ignore --no-index -q -- "$deployment_file"; then
    echo "ERROR: $deployment_file is not ignored by the effective Git rules." >&2
    exit 1
  fi
done

echo "Deployment command policy OK: local-only, ignored, and untracked."
