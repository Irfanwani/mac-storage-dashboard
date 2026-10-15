#!/bin/bash
# Builds a distributable zip of the self-contained app and prints its sha256 —
# everything the Homebrew cask (packaging/homebrew/mac-storage-dashboard.rb)
# needs. See RELEASING.md for the full release checklist.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found on this Mac. Install it (or Xcode command line tools) first."
  exit 1
fi

python3 -m mac_storage_dashboard app --out dist

ZIP="dist/Mac-Storage-Dashboard.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "dist/Mac Storage Dashboard.app" "$ZIP"

SHA="$(shasum -a 256 "$ZIP" | awk '{print $1}')"
echo
echo "Packaged: $ZIP"
echo "sha256:   $SHA"
echo
cat <<'EOF'
Next steps (once, until automated):
  1. Tag and create a GitHub release:
       git tag v1.0.0 && git push origin v1.0.0
       gh release create v1.0.0 dist/Mac-Storage-Dashboard.zip \
         --title "v1.0.0" --notes "See README"
  2. In your homebrew tap repo, add/update Casks/mac-storage-dashboard.rb
     using packaging/homebrew/mac-storage-dashboard.rb as the template —
     set `version` and `sha256` to the values above.
  3. Users install with:
       brew install --cask <your-tap>/mac-storage-dashboard
EOF
