#!/bin/bash
# After agent stop: deploy daemon if needed, then commit+push changes.
# Serialized via flock so parallel stop hooks / auto-push don't race on main.
set -u
input=$(cat || true)

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || exit 0

LOCKFILE="$ROOT/.git/auto-deploy.lock"
mkdir -p "$ROOT/.git"
exec 200>"$LOCKFILE"
if ! flock -w 120 200; then
  echo "auto-deploy: lock timeout" >&2
  echo '{}'
  exit 0
fi

SRC="$ROOT/daemon.py"
DST="$HOME/.cursor-daemon/daemon.py"
STATUS_MSG=""

# Deploy + restart when installed copy differs (no-op if SRC==DST)
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

_push_with_retry() {
  local tries=3
  local i=1
  local err=""
  local branch ahead behind

  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)

  while (( i <= tries )); do
    git fetch origin 2>/dev/null || true

    if git rev-parse --verify "origin/${branch}" >/dev/null 2>&1; then
      behind=$(git rev-list --count "HEAD..origin/${branch}" 2>/dev/null || echo 0)
      if [[ "$behind" != "0" ]]; then
        if ! git pull --rebase --autostash origin "$branch" >/dev/null 2>&1; then
          git rebase --abort >/dev/null 2>&1 || true
          echo "auto-deploy: rebase failed (attempt $i)" >&2
          return 1
        fi
      fi
    fi

    ahead=0
    if git rev-parse --verify "origin/${branch}" >/dev/null 2>&1; then
      ahead=$(git rev-list --count "origin/${branch}..HEAD" 2>/dev/null || echo 0)
    else
      ahead=1
    fi
    if [[ "$ahead" == "0" ]]; then
      return 0
    fi

    if err=$(git push -u origin "HEAD:${branch}" 2>&1); then
      return 0
    fi
    echo "auto-deploy: push attempt $i failed: $err" >&2
    sleep "$i"
    i=$((i + 1))
  done
  return 1
}

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
  branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
  if git rev-parse --verify "origin/${branch}" >/dev/null 2>&1; then
    ahead=$(git rev-list --count "origin/${branch}..HEAD" 2>/dev/null || echo 0)
  elif git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
  else
    ahead=1
  fi

  if [[ "$ahead" != "0" ]]; then
    if _push_with_retry; then
      STATUS_MSG="${STATUS_MSG}; pushed"
    else
      STATUS_MSG="${STATUS_MSG}; push failed"
    fi
  fi
fi

echo "auto-deploy: ${STATUS_MSG:-noop}" >&2
echo '{}'
exit 0
