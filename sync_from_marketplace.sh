#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────
# Script 1: Sync plugins from Dify Marketplace
# Uses a GitHub release as baseline, then
# crawls marketplace for updates/new plugins.
# ─────────────────────────────────────────────

# ─── Config ─────────────────────────────────
GITHUB_RELEASE_URL=""  # 👈 set your GitHub release zip URL here
API="https://marketplace.dify.ai/api/v1"

declare -A SORT_MAP=(
  ["model"]="install_count"
  ["tool"]="created_at"
  ["datasource"]="created_at"
  ["agent-strategy"]="created_at"
  ["trigger"]="created_at"
  ["extension"]="created_at"
)
declare -A DIR_MAP=(
  ["model"]="plugins/models"
  ["tool"]="plugins/tools"
  ["datasource"]="plugins/datasources"
  ["agent-strategy"]="plugins/agent-strategies"
  ["trigger"]="plugins/triggers"
  ["extension"]="plugins/extensions"
)
CATEGORIES=("model" "tool" "datasource" "agent-strategy" "trigger" "extension")

CHANGES=0

# ─── Helpers ────────────────────────────────
post_json() {
  local payload="$1"
  for attempt in {1..5}; do
    RESPONSE=$(curl -s -X POST "$API/plugins/search/advanced" \
      -H "Content-Type: application/json" \
      -d "$payload")
    if [[ "$(echo "$RESPONSE" | jq -r '.code')" == "0" ]]; then
      echo "$RESPONSE"
      return 0
    fi
    echo "⚠️ API retry $attempt..."
    sleep $((attempt * 2))
  done
  echo "❌ API failed after 5 attempts"
  exit 1
}

download_plugin() {
  local org="$1" name="$2" version="$3" out_dir="$4" category="$5"
  local dir="$out_dir/${org}-${name}"
  mkdir -p "$dir"

  local file="$dir/${name}.difypkg"
  local tmp="$dir/${name}.tmp"
  local ver_file="$dir/version"
  local url="$API/plugins/$org/$name/$version/download"

  for attempt in {1..5}; do
    local code
    code=$(curl -s -L -w "%{http_code}" -o "$tmp" "$url")
    if [[ "$code" == "200" && -s "$tmp" ]]; then
      mv "$tmp" "$file"
      printf '%s' "$version" >"$ver_file"
      return 0
    elif [[ "$code" == "429" ]]; then
      echo "⚠️ Rate limited, retry $attempt..."
      sleep $((attempt * 2))
    else
      echo "❌ retry $attempt (HTTP $code)"
      sleep $((attempt * 2))
    fi
  done
  rm -f "$tmp" || true
  return 1
}

# ─── Step 1: Download & extract GitHub release ──
if [[ -n "$GITHUB_RELEASE_URL" ]]; then
  echo "📦 Downloading GitHub release baseline..."
  WORK_TMPDIR=$(mktemp -d)
  trap 'rm -rf "$WORK_TMPDIR"' EXIT

  curl -s -L -o "$WORK_TMPDIR/release.zip" "$GITHUB_RELEASE_URL"
  unzip -o "$WORK_TMPDIR/release.zip" -d "$WORK_TMPDIR/extracted" >/dev/null

  # Merge extracted plugin contents into plugins/
  for category in "${CATEGORIES[@]}"; do
    src_dir="$WORK_TMPDIR/extracted/${DIR_MAP[$category]}"
    dst_dir="${DIR_MAP[$category]}"
    if [[ -d "$src_dir" ]]; then
      mkdir -p "$dst_dir"
      cp -rn "$src_dir"/* "$dst_dir"/ 2>/dev/null || true
    fi
  done

  # Merge any other top-level dirs (templates, etc.)
  for item in "$WORK_TMPDIR/extracted"/*/; do
    itemname=$(basename "$item")
    if [[ "$itemname" != "plugins" ]] && [[ ! " ${CATEGORIES[*]} " =~ $itemname ]]; then
      if [[ -d "$itemname" ]]; then
        cp -rn "$item"/* "$itemname"/ 2>/dev/null || true
      else
        cp -r "$item" "$itemname"
      fi
    fi
  done
  echo "✅ Baseline extracted"
else
  echo "⚠️ GITHUB_RELEASE_URL not set — skipping baseline, working with existing dirs"
fi

# ─── Step 2: Crawl marketplace & sync ───────
for CATEGORY in "${CATEGORIES[@]}"; do
  SORT_BY="${SORT_MAP[$CATEGORY]}"
  OUT_DIR="${DIR_MAP[$CATEGORY]}"
  mkdir -p "$OUT_DIR"

  echo ""
  echo "👉 CATEGORY: $CATEGORY"

  TOTAL=$(post_json "{
    \"page\":1,
    \"page_size\":1,
    \"query\":\"\",
    \"sort_by\":\"$SORT_BY\",
    \"sort_order\":\"DESC\",
    \"category\":\"$CATEGORY\",
    \"tags\":[]
  }" | jq '.data.total')

  echo "Total: $TOTAL"

  if [[ "$TOTAL" == "0" ]]; then
    echo "⚠️ WARNING: No plugins found for category '$CATEGORY'"
    continue
  fi

  ALL_RESPONSE=$(post_json "{
    \"page\":1,
    \"page_size\":$TOTAL,
    \"query\":\"\",
    \"sort_by\":\"$SORT_BY\",
    \"sort_order\":\"DESC\",
    \"category\":\"$CATEGORY\",
    \"tags\":[]
  }")

  PLUGINS=$(echo "$ALL_RESPONSE" | jq -c '.data.plugins[]? // .data.items[]?')

  if [[ -z "$PLUGINS" ]]; then
    echo "⚠️ WARNING: No plugins found for '$CATEGORY' (unexpected structure)"
    echo "$ALL_RESPONSE" | jq '.data | keys'
    continue
  fi

  echo "Processing plugins..."

  while read -r plugin; do
    PLUGIN_ID=$(echo "$plugin" | jq -r '.plugin_id // .id // empty')
    VERSION=$(echo "$plugin" | jq -r '.latest_version // .version // empty')

    if [[ -z "$PLUGIN_ID" || -z "$VERSION" ]]; then
      echo "⚠️ Skipping invalid entry:"
      echo "$plugin" | jq
      continue
    fi

    ORG="${PLUGIN_ID%%/*}"
    NAME="${PLUGIN_ID##*/}"
    DIR="$OUT_DIR/${ORG}-${NAME}"
    FILE="$DIR/${NAME}.difypkg"
    VERSION_FILE="$DIR/version"

    if [[ ! -f "$FILE" || ! -f "$VERSION_FILE" ]]; then
      echo "→ NEW: $PLUGIN_ID ($VERSION)"
    else
      EXISTING=$(tr -d '[:space:]' <"$VERSION_FILE")
      CURRENT=$(echo "$VERSION" | tr -d '[:space:]')
      if [[ "$EXISTING" != "$CURRENT" ]]; then
        echo "→ UPDATE: $PLUGIN_ID ($EXISTING → $VERSION)"
      else
        echo "⏭️ Up to date: $PLUGIN_ID ($VERSION)"
        continue
      fi
    fi

    if download_plugin "$ORG" "$NAME" "$VERSION" "$OUT_DIR" "$CATEGORY"; then
      echo "✅ Saved: $PLUGIN_ID"
      CHANGES=$((CHANGES + 1))
    else
      echo "❌ Failed to download: $PLUGIN_ID"
    fi

    sleep 1
  done <<<"$PLUGINS"
done

# ─── Step 3: Update global VERSION ──────────
CURRENT_VERSION=0
if [[ -f "VERSION" ]]; then
  CURRENT_VERSION=$(tr -d '[:space:]' <"VERSION" || echo 0)
  if ! [[ "$CURRENT_VERSION" =~ ^[0-9]+$ ]]; then
    CURRENT_VERSION=0
  fi
fi

if [[ $CHANGES -gt 0 ]]; then
  NEW_VERSION=$((CURRENT_VERSION + 1))
  printf '%s' "$NEW_VERSION" >"VERSION"
  echo ""
  echo "🔄 $CHANGES plugin(s) updated — VERSION: $CURRENT_VERSION → $NEW_VERSION"
else
  echo ""
  echo "No updates found — VERSION remains $CURRENT_VERSION"
fi

echo "🎉 Sync complete"
