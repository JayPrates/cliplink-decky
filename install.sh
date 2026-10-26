#!/usr/bin/env bash
# Installs or updates ClipLink in Decky Loader (Steam Deck, Bazzite, other Linux).
# Usage: curl -fsSL https://raw.githubusercontent.com/JayPrates/cliplink-decky/main/install.sh | bash
set -euo pipefail

REPO="JayPrates/cliplink-decky"
PLUGINS="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user (it asks for sudo itself), not with sudo." >&2
  exit 1
fi
if [ ! -d "$PLUGINS" ]; then
  echo "Decky Loader isn't installed ($PLUGINS not found). Install it first: https://decky.xyz" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "Downloading the latest release..."
curl -fsSL "https://github.com/$REPO/releases/latest/download/ClipLink.zip" -o "$tmp/plugin.zip"
if command -v bsdtar >/dev/null 2>&1; then
  bsdtar -xf "$tmp/plugin.zip" -C "$tmp"
else
  unzip -q "$tmp/plugin.zip" -d "$tmp"
fi

echo "Installing to $PLUGINS/ClipLink (needs sudo)..."
sudo rm -rf "$PLUGINS/ClipLink"
sudo cp -r "$tmp/ClipLink" "$PLUGINS/"
sudo systemctl restart plugin_loader
echo "Done. Open the Quick Access menu (...) -> Decky -> ClipLink."
