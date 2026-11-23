#!/usr/bin/env bash
set -euo pipefail

ID="hermes-quota-live"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

BACKEND="$HOME_DIR/plugins/$ID"
DESKTOP="$HOME_DIR/desktop-plugins/$ID"

echo "Installing $ID from $REPO"

mkdir -p "$BACKEND/dashboard" "$DESKTOP"
cp "$REPO/plugin.yaml" "$REPO/__init__.py" "$BACKEND/"
cp "$REPO"/dashboard/* "$BACKEND/dashboard/"
cp "$REPO/desktop/plugin.js" "$DESKTOP/plugin.js"

if command -v hermes >/dev/null 2>&1; then
  hermes plugins enable "$ID" || true
else
  echo "hermes CLI not found: enable the plugin manually."
fi

echo "OK. In Hermes Desktop run ⌘K → Reload desktop plugins"
echo "If you replaced an already-installed backend, restart the gateway or use a new plugin id."
