#!/usr/bin/env bash
# Install tracked git hooks into .git/hooks/
# Run once after cloning or whenever hooks/scripts/pre-commit is updated.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_SRC="$REPO_ROOT/scripts/pre-commit"
HOOKS_DST="$REPO_ROOT/.git/hooks/pre-commit"

cp "$HOOKS_SRC" "$HOOKS_DST"
chmod +x "$HOOKS_DST"
echo "✅ Installed pre-commit hook → .git/hooks/pre-commit"

POST_SRC="$REPO_ROOT/scripts/post-commit"
POST_DST="$REPO_ROOT/.git/hooks/post-commit"
cp "$POST_SRC" "$POST_DST"
chmod +x "$POST_DST"
echo "✅ Installed post-commit hook → .git/hooks/post-commit"
