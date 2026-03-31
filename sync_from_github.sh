#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# Script 2: Sync from GitHub release
# Checks release version, downloads & replaces
# all directories if version changed.
# ─────────────────────────────────────────────

# ─── Config ─────────────────────────────────
GITHUB_RELEASE_URL=""  # 👈 set your GitHub release URL here

LOCAL_VERSION_FILE=".github_release_version"
SYNC_DIRS=("plugins/models" "plugins/tools" "plugins/datasources" "plugins/agent-strategies" "plugins/triggers" "plugins/extensions" "plugins/templates")

# ─── Extract version from GitHub release ────
get_release_version() {
  local url="$1"

  # Try to extract tag from URL patterns:
  # .../releases/tag/v1.2.3  or  .../releases/download/v1.2.3/...
  local tag
  tag=$(echo "$url" | grep -oP '(?:tag|download)/\K[^/]+')

  if [[ -n "$tag" ]]; then
    echo "$tag"
    return 0
  fi

  # Fallback: query GitHub API
  local api_url
  api_url=$(echo "$url" | sed -E 's|github\.com/([^/]+)/([^/]+)/releases.*|api.github.com/repos/\1/\2/releases/latest|')
  if [[ "$api_url" != "$url" ]]; then
    tag=$(curl -s "$api_url" | jq -r '.tag_name // empty')
    if [[ -n "$tag" ]]; then
      echo "$tag"
      return 0
    fi
  fi

  echo ""
  return 1
}

# ─── Main ───────────────────────────────────
if [[ -z "$GITHUB_RELEASE_URL" ]]; then
  echo "❌ GITHUB_RELEASE_URL is not set"
  exit 1
fi

echo "📡 Checking GitHub release version..."
REMOTE_VERSION=$(get_release_version "$GITHUB_RELEASE_URL")

if [[ -z "$REMOTE_VERSION" ]]; then
  echo "❌ Could not determine release version from URL"
  exit 1
fi

echo "Remote version: $REMOTE_VERSION"

# Check local version
LOCAL_VERSION=""
if [[ -f "$LOCAL_VERSION_FILE" ]]; then
  LOCAL_VERSION=$(tr -d '[:space:]' <"$LOCAL_VERSION_FILE")
fi

if [[ "$LOCAL_VERSION" == "$REMOTE_VERSION" ]]; then
  echo "✅ Already up to date with release $REMOTE_VERSION"
  exit 0
fi

echo "🔄 Version changed: ${LOCAL_VERSION:-none} → $REMOTE_VERSION"
echo "📦 Downloading release..."

WORK_TMPDIR=$(mktemp -d)
trap 'rm -rf "$WORK_TMPDIR"' EXIT

curl -s -L -o "$WORK_TMPDIR/release.zip" "$GITHUB_RELEASE_URL"

if [[ ! -s "$WORK_TMPDIR/release.zip" ]]; then
  echo "❌ Failed to download release"
  exit 1
fi

unzip -o "$WORK_TMPDIR/release.zip" -d "$WORK_TMPDIR/extracted" >/dev/null

# Find the actual root of extracted contents
EXTRACT_ROOT="$WORK_TMPDIR/extracted"
# If there's a single top-level dir, use that
if [[ $(find "$EXTRACT_ROOT" -mindepth 1 -maxdepth 1 -type d | wc -l) -eq 1 ]]; then
  EXTRACT_ROOT=$(find "$EXTRACT_ROOT" -mindepth 1 -maxdepth 1 -type d | head -1)
fi

echo "🔄 Syncing directories..."
for dir in "${SYNC_DIRS[@]}"; do
  if [[ -d "$EXTRACT_ROOT/$dir" ]]; then
    echo "  → $dir"
    mkdir -p "$dir"
    rsync -a --delete "$EXTRACT_ROOT/$dir/" "$dir/" 2>/dev/null || cp -rn "$EXTRACT_ROOT/$dir/"* "$dir"/ 2>/dev/null || true
  else
    echo "  ⚠️ $dir not found in release"
  fi
done

# Update version tracker
printf '%s' "$REMOTE_VERSION" >"$LOCAL_VERSION_FILE"

echo ""
echo "✅ Updated to release $REMOTE_VERSION"
echo "🎉 Sync complete"
