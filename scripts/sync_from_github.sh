#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# Sync from GitHub release
# Checks release version, downloads & replaces
# all plugin directories if version changed.
# ─────────────────────────────────────────────

# ─── Config ─────────────────────────────────
GITHUB_REPO="Prince240102/studio-marketplace"
LOCAL_VERSION_FILE="plugins/VERSION"
SYNC_DIRS=("plugins/models" "plugins/tools" "plugins/datasources" "plugins/agent-strategies" "plugins/triggers" "plugins/extensions" "plugins/templates")

# ─── Get latest release tag ─────────────────
echo "📡 Checking GitHub release version..."
REMOTE_VERSION=$(curl -s "https://api.github.com/repos/$GITHUB_REPO/releases/latest" | jq -r '.tag_name')

if [[ -z "$REMOTE_VERSION" || "$REMOTE_VERSION" == "null" ]]; then
  echo "❌ Could not determine release version"
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

curl -s -L -o "$WORK_TMPDIR/plugins.zip" "https://github.com/$GITHUB_REPO/releases/download/$REMOTE_VERSION/plugins.zip"

if [[ ! -s "$WORK_TMPDIR/plugins.zip" ]]; then
  echo "❌ Failed to download release"
  exit 1
fi

unzip -o "$WORK_TMPDIR/plugins.zip" -d "$WORK_TMPDIR/extracted" >/dev/null

EXTRACT_ROOT="$WORK_TMPDIR/extracted"

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

echo "📦 Extracting .difypkg files..."
find plugins -type f -name "*.difypkg" | while IFS= read -r file; do
  target_dir="${file%.difypkg}"
  mkdir -p "$target_dir"
  unzip -o "$file" -d "$target_dir" >/dev/null
  echo "  → ${file%.difypkg}/"
done

# Update version tracker
printf '%s' "$REMOTE_VERSION" >"$LOCAL_VERSION_FILE"

echo ""
echo "✅ Updated to release $REMOTE_VERSION"
echo "🎉 Sync complete"
