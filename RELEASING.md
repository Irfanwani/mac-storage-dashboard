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

- **Homebrew tap** — add a repo secret `HOMEBREW_TAP_TOKEN`:

  - Classic PAT with the `repo` scope (also lets the workflow create
    `Irfanwani/homebrew-tap` on the first release), **or**
  - fine-grained PAT with *Contents: Read and write* on `homebrew-tap`
    (create the tap repo yourself first).

  Without the secret the tap job skips with a warning; everything else still
  releases.

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
