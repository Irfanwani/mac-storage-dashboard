# Releasing

Releases are **automated** by [`.github/workflows/release.yml`](.github/workflows/release.yml).
The only thing you do is bump the version.

## The normal flow (2 steps)

1. Bump the version — the single source of truth:

   ```python
   # mac_storage_dashboard/__init__.py
   __version__ = "1.0.1"
   ```

   (The wheel metadata, `mac-storage-dashboard --version` and the app's
   `CFBundleShortVersionString` all read this value.)

2. Commit and push/merge to `main`.

The **Release** workflow then, if tag `v<version>` doesn't exist yet:

| Job | What it does |
|---|---|
| `version` | reads `__version__`, checks the tag; skips everything when the version was already released (docs pushes are no-ops) |
| `app-release` | runs the cleanup regression test, builds the app on macOS, smoke-tests that it boots and serves, zips it, creates the GitHub release (tag `v<version>` + auto-generated notes) and uploads `Mac-Storage-Dashboard.zip` |
| `pypi` | builds sdist + wheel, publishes to PyPI (skips if the version is already there, so retries are safe) |
| `homebrew` | writes `Casks/mac-storage-dashboard.rb` (version + sha256) into the tap repo and pushes |

## One-time setup

- **PyPI** — via Trusted Publishing (no secrets stored):

  pypi.org → *Your projects* → **Publishing** → *Add a pending publisher* with:

  | Field | Value |
  |---|---|
  | PyPI project name | `mac-storage-dashboard` |
  | Owner | `Irfanwani` |
  | Repository name | `mac-storage-dashboard` |
  | Workflow name | `release.yml` |
  | Environment name | *(leave blank)* |

  Prefer an API token? Create one, add it as the `PYPI_API_TOKEN` secret, and
  uncomment the `with: password:` line in the `pypi` job.

- **Homebrew tap** — the release workflow pushes the updated cask to
  `Irfanwani/homebrew-tap`, so it needs one repository secret in **this** repo
  (`mac-storage-dashboard`), named `HOMEBREW_TAP_TOKEN`. Two ways to set it up:

  **Option 1 — let the workflow create the tap (simplest)**

  1. Create a *classic* PAT: GitHub → avatar → **Settings → Developer settings →
     Personal access tokens → Tokens (classic) → Generate new token (classic)**.
     - Note: `mac-storage-dashboard releases`
     - Expiration: anything (e.g. 90 days). If it expires, the tap job skips
       with a warning until you rotate it — the other channels never break.
     - Scopes: check **`repo`** (or just **`public_repo`** — the tap is public).
  2. Add it to this repo: **Settings → Secrets and variables → Actions → New
     repository secret**, name `HOMEBREW_TAP_TOKEN`, paste the token.
  3. Done — the first release creates `Irfanwani/homebrew-tap` (public) and
     pushes `Casks/mac-storage-dashboard.rb` into it.

  **Option 2 — pre-create the tap, least-privilege token**

  1. Create the tap: github.com/new → owner `Irfanwani`, name `homebrew-tap`,
     Public, "Add a README file" ✓.
  2. Create a *fine-grained* PAT: **Settings → Developer settings → Personal
     access tokens → Fine-grained tokens → Generate new token**:
     Resource owner `Irfanwani`, *Only select repositories* → `homebrew-tap`,
     permissions **Contents → Read and write**.
  3. Add it as `HOMEBREW_TAP_TOKEN`, same as above.

  (The tap must be named exactly `homebrew-tap` — otherwise edit `TAP=` in the
  `homebrew` job of the workflow. Without the secret at all, the tap job skips
  with a warning; everything else still releases.)

  **Optional — verify a classic token before merging:**

  ```bash
  curl -s -H "Authorization: token <TOKEN>" https://api.github.com/user | grep -o '"login": *"[^"]*"'
  curl -sI -H "Authorization: token <TOKEN>" https://api.github.com/user | grep -i x-oauth-scopes
  ```

  After the first release, `Irfanwani/homebrew-tap` should contain a commit
  (`mac-storage-dashboard <version>`) with `Casks/mac-storage-dashboard.rb`.
  If the `homebrew` job fails, check its log — usual causes: missing/mistyped
  secret, expired or under-scoped token (403), or branch protection on the tap.

- Nothing else — the GitHub release uses the built-in `GITHUB_TOKEN`.

After the first successful tap update, users install with:

```bash
brew install --cask irfanwani/tap/mac-storage-dashboard
```

## First run note

`v1.0.0` doesn't exist yet, so the first push (or merge) to `main` after this
workflow lands will release the current `__version__`. If PyPI/tap aren't
configured yet: the PyPI job fails (loudly) while GitHub Release + Homebrew
setup still run — configure the one-time setup above first, or temporarily
work on a branch.

## Retries

Every job is idempotent (release update, PyPI skip-if-exists, cask overwrite):

- Re-run a failed run from the Actions tab (**Re-run failed jobs**), or
- **Release → Run workflow** on `main` — a manual run always proceeds, even
  when the tag already exists.

## Manual fallback (no CI)

```bash
./release.command                     # builds + zips the app, prints the sha256
python3 -m build && python3 -m twine upload dist/*
gh release create v1.0.1 dist/Mac-Storage-Dashboard.zip --title v1.0.1 --generate-notes
# then hand-edit the cask in the tap with the new version + sha and push
```
