#!/bin/bash
# Builds a distributable zip of the self-contained app and prints its sha256.
#
# You normally don't need this: releases are automated on version bumps
# (.github/workflows/release.yml tags, builds, publishes to PyPI and updates
# the Homebrew tap). This script is the manual fallback — build the zip
# locally, e.g. when CI is unavailable or to verify a build before pushing.
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
Manual release steps:
  1. Publish to PyPI:
       python3 -m build && python3 -m twine upload dist/*
  2. Create a GitHub release and upload the zip:
       git tag v1.0.0 && git push origin v1.0.0
       gh release create v1.0.0 dist/Mac-Storage-Dashboard.zip \
         --title "v1.0.0" --generate-notes
  3. In your homebrew tap repo, update Casks/mac-storage-dashboard.rb
     (version + sha256 above).
EOF
