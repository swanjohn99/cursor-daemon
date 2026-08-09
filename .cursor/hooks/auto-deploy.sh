#!/bin/bash
# After agent stop: deploy daemon if needed, then commit+push changes.
set -u
input=$(cat || true)

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 0

SRC="$ROOT/daemon.py"
DST="$HOME/.cursor-daemon/daemon.py"
STATUS_MSG=""

# Deploy + restart when installed copy differs
if [[ -f "$SRC" ]]; then
  if [[ ! -f "$DST" ]] || ! cmp -s "$SRC" "$DST"; then
    mkdir -p "$(dirname "$DST")"
    cp "$SRC" "$DST"
    if systemctl --user restart cursor-daemon; then
      STATUS_MSG="daemon restarted"
    else
      STATUS_MSG="daemon restart failed"
    fi
  else
    STATUS_MSG="daemon up to date"
  fi
fi

# Commit + push if there is anything to publish
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git add -u
  git add .cursor 2>/dev/null || true
  git add -- ':(glob)*.py' ':(glob)*.md' ':(glob).gitignore' 2>/dev/null || true

  if ! git diff --cached --quiet 2>/dev/null; then
    NAME=$(git log -1 --format='%an' 2>/dev/null || echo Cursor)
    EMAIL=$(git log -1 --format='%ae' 2>/dev/null || echo cursor@localhost)
    if git -c user.name="$NAME" -c user.email="$EMAIL" commit -m "auto: sync agent changes" >/dev/null 2>&1; then
      STATUS_MSG="${STATUS_MSG}; committed"
    else
      STATUS_MSG="${STATUS_MSG}; commit failed"
    fi
  fi

  ahead=0
  if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
  else
    ahead=1
  fi
  if [[ "$ahead" != "0" ]]; then
    if git push -u origin HEAD >/dev/null 2>&1; then
      STATUS_MSG="${STATUS_MSG}; pushed"
    else
      STATUS_MSG="${STATUS_MSG}; push failed"
    fi
  fi
fi

echo "auto-deploy: ${STATUS_MSG:-noop}" >&2
echo '{}'
exit 0
