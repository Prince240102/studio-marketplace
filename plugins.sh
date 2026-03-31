#!/usr/bin/env bash

set -euo pipefail

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
	["model"]="models"
	["tool"]="tools"
	["datasource"]="datasources"
	["agent-strategy"]="agent-strategies"
	["trigger"]="triggers"
	["extension"]="extensions"
)

# 👉 adjust categories as needed
CATEGORIES=("datasource" "trigger")

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

	echo "❌ API failed"
	exit 1
}

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
	[[ "$TOTAL" == "0" ]] && continue

	ALL_RESPONSE=$(post_json "{
    \"page\":1,
    \"page_size\":$TOTAL,
    \"query\":\"\",
    \"sort_by\":\"$SORT_BY\",
    \"sort_order\":\"DESC\",
    \"category\":\"$CATEGORY\",
    \"tags\":[]
  }")

	# 👉 detect correct array (plugins OR items)
	PLUGINS=$(echo "$ALL_RESPONSE" | jq -c '.data.plugins[]? // .data.items[]?')

	if [[ -z "$PLUGINS" ]]; then
		echo "⚠️ No plugins found for $CATEGORY (unexpected structure)"
		echo "$ALL_RESPONSE" | jq '.data | keys'
		continue
	fi

	echo "Processing plugins..."

	while read -r plugin; do

		# 👉 flexible field extraction
		PLUGIN_ID=$(echo "$plugin" | jq -r '.plugin_id // .id // empty')
		VERSION=$(echo "$plugin" | jq -r '.latest_version // .version // empty')

		if [[ -z "$PLUGIN_ID" || -z "$VERSION" ]]; then
			echo "⚠️ Skipping invalid plugin entry:"
			echo "$plugin" | jq
			continue
		fi

		echo "→ Checking $PLUGIN_ID ($VERSION)"

		ORG=${PLUGIN_ID%%/*}
		NAME=${PLUGIN_ID##*/}

		DIR="$OUT_DIR/${ORG}-${NAME}"
		mkdir -p "$DIR"

		FILE="$DIR/${NAME}.difypkg"
		TMP="$DIR/${NAME}.tmp"
		VERSION_FILE="$DIR/version"

		# ---------- cache ----------
		if [[ -f "$FILE" && -f "$VERSION_FILE" ]]; then
			EXISTING=$(tr -d '[:space:]' <"$VERSION_FILE")
			CURRENT=$(echo "$VERSION" | tr -d '[:space:]')

			if [[ "$EXISTING" == "$CURRENT" && -s "$FILE" ]]; then
				echo "⏭️ [$CATEGORY] cached → $PLUGIN_ID ($VERSION)"
				continue
			fi
		fi

		URL="$API/plugins/$ORG/$NAME/$VERSION/download"
		echo "⬇️ [$CATEGORY] $PLUGIN_ID ($VERSION)"
		echo "   URL: $URL"

		for attempt in {1..5}; do
			CODE=$(curl -s -L -w "%{http_code}" -o "$TMP" "$URL")

			if [[ "$CODE" == "200" && -s "$TMP" ]]; then
				mv "$TMP" "$FILE"
				echo -n "$VERSION" >"$VERSION_FILE"
				echo "✅ Saved"
				break
			elif [[ "$CODE" == "429" ]]; then
				echo "⚠️ Rate limited, retry $attempt..."
				sleep $((attempt * 2))
			else
				echo "❌ retry $attempt (HTTP $CODE)"
				sleep $((attempt * 2))
			fi
		done

		rm -f "$TMP" || true
		sleep 1

	done <<<"$PLUGINS"

done

echo "🎉 Plugins done"
