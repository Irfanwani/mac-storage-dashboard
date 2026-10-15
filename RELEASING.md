# Releasing

Two distribution channels, both hosted off the GitHub repo
(`Irfanwani/mac-storage-dashboard`). Neither is automated yet — this file is the
checklist.

## 1. PyPI (the `pipx install mac-storage-dashboard` route)

The package ships the scanner, server and CLI; `mac-storage-dashboard install` builds
the app locally and drops it in `/Applications`, so no signing/notarization is
involved for users.

```bash
# once: create an API token at https://pypi.org/manage/account/token/
python3 -m pip install --user --upgrade build twine

# every release
python3 -m build                      # writes dist/*.whl and dist/*.tar.gz
python3 -m twine upload dist/*        # or: python3 -m twine upload --repository testpypi dist/*  (rehearse first)
```

Test the result in a clean environment:

```bash
pipx install mac-storage-dashboard
mac-storage-dashboard install
mac-storage-dashboard uninstall   # clean up afterwards if you were just testing
```

## 2. Homebrew cask (the `brew install --cask` route)

The cask distributes the *prebuilt* app zip from GitHub Releases. Gatekeeper is fine
with ad-hoc signing here because Homebrew removes the quarantine flag on install.

```bash
./release.command                 # builds the app, writes dist/Mac-Storage-Dashboard.zip, prints sha256
git tag v1.0.0 && git push origin v1.0.0
gh release create v1.0.0 dist/Mac-Storage-Dashboard.zip --title v1.0.0 --notes "…"
```

Then, in your tap repo (e.g. `Irfanwani/homebrew-tap`, create it once):

1. Copy `packaging/homebrew/mac-storage-dashboard.rb` to `Casks/mac-storage-dashboard.rb`.
2. Set `version` to the tag (e.g. `1.0.0`) and `sha256` to the hash printed by
   `release.command`.
3. Commit and push the tap.

Users install with:

```bash
brew install --cask irfanwani/tap/mac-storage-dashboard   # app lands in /Applications
```

## Versioning

`mac_storage_dashboard/__init__.py` (`__version__`) is the single source of truth —
the wheel metadata, the built app's `CFBundleShortVersionString` and
`mac-storage-dashboard --version` all read it. Bump it, commit, then follow either
channel above with a matching tag.

## Notes

- The repo is not signed with an Apple Developer certificate. Locally built apps
  (PyPI path) and Homebrew-installed apps both avoid the "unidentified developer"
  dialog; a *downloaded* zip from anywhere else would not.
- Full Disk Access is per-app: after reinstalling the app, macOS may ask for the
  permission again (System Settings → Privacy & Security → Full Disk Access).
