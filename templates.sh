#!/usr/bin/env bash
set -uo pipefail # removed -e: let us handle errors manually

API="https://marketplace.dify.ai/api/v1"
OUT_DIR="templates"
mkdir -p "$OUT_DIR"

post_json() {
	local payload="$1"
	local response
	for attempt in {1..5}; do
		response=$(curl -s -X POST "$API/templates/search/advanced" \
			-H "Content-Type: application/json" \
			-d "$payload") || true

		if echo "$response" | jq -e '.data.templates' >/dev/null 2>&1; then
			echo "$response"
			return 0
		fi
		echo "⚠️  retry $attempt (no .templates in response)..." >&2
		sleep $((attempt * 2))
	done
	echo "❌ API failed after 5 attempts" >&2
	return 1
}

echo "📡 Fetching template list..."
ALL=$(post_json '{
  "page":1,
  "page_size":1000,
  "query":"",
  "sort_by":"usage_count",
  "sort_order":"DESC",
  "categories":[]
}') || {
	echo "❌ Could not fetch template list. Exiting."
	exit 1
}

TOTAL=$(echo "$ALL" | jq '.data.templates | length')
echo "📦 Found $TOTAL templates"

# Use a temp fifo instead of process substitution to avoid set -e pitfalls
TEMPLATES=$(echo "$ALL" | jq -c '.data.templates[]')

while IFS= read -r tpl; do
	ID=$(echo "$tpl" | jq -r '.id')
	NAME=$(echo "$tpl" | jq -r '.template_name')
	VERSION=$(echo "$tpl" | jq -r '.updated_at')
	SAFE=$(echo "$NAME" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')

	DIR="$OUT_DIR/$SAFE"
	mkdir -p "$DIR"
	FILE="$DIR/template.yaml"
	TMP="$DIR/template.tmp"
	VERSION_FILE="$DIR/version"

	# ---------- cache check ----------
	if [[ -f "$FILE" && -f "$VERSION_FILE" ]]; then
		EXISTING=$(tr -d '[:space:]' <"$VERSION_FILE")
		CURRENT=$(echo "$VERSION" | tr -d '[:space:]')
		if [[ "$EXISTING" == "$CURRENT" && -s "$FILE" ]]; then
			echo "⏭️  cached → $NAME"
			continue
		fi
	fi

	URL="$API/templates/$ID/file"
	echo "⬇️  $NAME"
	SUCCESS=0
	for attempt in {1..5}; do
		CODE=$(curl -s -L -w "%{http_code}" -o "$TMP" "$URL") || true
		if [[ "$CODE" == "200" && -s "$TMP" ]]; then
			mv "$TMP" "$FILE"
			printf '%s' "$VERSION" >"$VERSION_FILE"
			echo "✅ Saved → $SAFE"
			SUCCESS=1
			break
		elif [[ "$CODE" == "429" ]]; then
			echo "⏳ Rate limited, waiting $((attempt * 2))s..."
			sleep $((attempt * 2))
		else
			echo "⚠️  retry $attempt (HTTP $CODE)"
			sleep $((attempt * 2))
		fi
	done

	[[ $SUCCESS -eq 0 ]] && echo "❌ Failed to download: $NAME" >&2
	rm -f "$TMP" 2>/dev/null || true
	sleep 1
done <<<"$TEMPLATES"

echo "🎉 Templates done"
