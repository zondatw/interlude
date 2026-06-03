# Release process

Interlude publishes to PyPI via the branch-push pattern used by
`zondatw/bot-cmder` and `zondatw/remote-cmder`. There are two branches:

| Branch    | Trigger workflow                | Index             |
|-----------|---------------------------------|-------------------|
| `beta`    | `.github/workflows/beta.yml`    | test.pypi.org     |
| `release` | `.github/workflows/release.yml` | pypi.org (prod)   |

Auth is **PyPI Trusted Publishing (OIDC)** — no long-lived
`TWINE_PASSWORD` lives in repo secrets. GitHub mints a short-lived
token at publish time, PyPI validates it against a pending publisher
configured on each index.

## One-time setup

Do this once per index, before the first publish.

### 1. Configure the pending publisher on TestPyPI

1. Log in to <https://test.pypi.org/> as the project owner.
2. Account → **Publishing** → *Add a new pending publisher*.
3. Fill in:
   - PyPI project name: `interlude`
   - Owner: `zondatw`
   - Repository: `interlude`
   - Workflow filename: `beta.yml`
   - Environment: `testpypi`
4. Save.

### 2. Same again on PyPI (prod)

Repeat at <https://pypi.org/> with:

- Workflow filename: `release.yml`
- Environment: `release`

### 3. GitHub Environments

In repo Settings → Environments, create:

| Name        | Required reviewers      | Why                                                  |
|-------------|-------------------------|------------------------------------------------------|
| `testpypi`  | (none)                  | TestPyPI mistakes are cheap — auto-publish is fine.  |
| `release`   | yourself (at least one) | Forces a manual click before any push to pypi.org.   |

The required-reviewer rule on `release` means the publish job pauses in
the Actions UI and waits for "Approve" — protects against an accidental
`git push origin release` shipping a bad cut.

## Per-release flow

```bash
# 0. cut a version bump on main first
#    Edit pyproject.toml [project].version → e.g. 0.2.0
#    Commit + push to main, get it merged via PR

# 1. forward to beta → triggers test.pypi.org publish
git checkout beta
git merge main --ff-only
git push
# wait for green CI; eyeball https://test.pypi.org/project/interlude/

# 2. verify the test install actually works on a fresh shell
pipx install \
  --index-url https://test.pypi.org/simple/ \
  --pip-args="--extra-index-url https://pypi.org/simple/" \
  interlude
interlude --help

# 3. only when satisfied → forward to release → triggers pypi.org publish
git checkout release
git merge beta --ff-only
git push
# Actions run pauses for your approve — click it once you're sure.
# wait for green; verify https://pypi.org/project/interlude/

# 4. (optional, nice for the GitHub release page)
git tag v0.2.0
git push --tags
gh release create v0.2.0 --generate-notes
```

## Why branches, not tags

`git log beta` answers "what's currently on test.pypi.org?" without
needing the GitHub UI; same for `release` and prod. Tag-based releases
make "what shipped last?" require an API roundtrip. We pay one branch's
worth of bookkeeping in exchange.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ERROR: HTTPError: 403 ... Trusted publisher invalid` | Pending publisher not yet set up on the index (see *One-time setup*), or workflow filename / environment name in the publisher config doesn't match the actual workflow. |
| `interlude` not on PATH after `pipx install` | pipx's bin dir isn't on PATH → run `pipx ensurepath` once, then open a new shell. |
| Test install fails with "No matching distribution" | The TestPyPI publish step succeeded but the index is eventually consistent — wait ~30s and retry. |
| Version bump forgotten → push to `beta` re-publishes the same version | Test/prod PyPI both reject re-uploading an existing version. Bump `pyproject.toml [project].version`, force-push beta if you haven't shared the branch. |

## What's NOT in this flow (deliberately)

- **Auto-version-bump from commits** — explicit bumps force you to
  think about semver impact (breaking change vs feature vs fix).
- **CHANGELOG generation** — interlude's history lives in PR titles +
  git log; a CHANGELOG file would duplicate that.
- **conda-forge / homebrew submission** — interlude is a 5-file
  zero-dep tool; PyPI + pipx is the right surface.
