# gitlab-sync

Mirrors every non-empty public project from [gitlab.manjaro.org](https://gitlab.manjaro.org)
into the [`manjaro-contrib`](https://github.com/manjaro-contrib) GitHub organization.

## Scope

All **2029** non-empty GitLab projects are mirrored — **1454** active plus **575** archived.
The only exclusion is the **10** empty projects: they advertise no refs, so a push would
fail and an empty placeholder repo is noise. Emptiness is detected per run by ref digest,
not by GitLab's `empty_repo` flag, so a project that gains its first commit is picked up
automatically.

The archived bit is mirrored **both ways**: archiving a project on GitLab archives its
GitHub mirror on the next run, and un-archiving on GitLab un-archives it again.

Nothing is ever deleted on GitHub. A project removed upstream keeps its mirror.

Mirrors have issues, wikis and projects disabled — they are read-only copies, and
discussion belongs on GitLab.

## Naming: flattening, and topics for the hierarchy

GitHub has no subgroups; ownership is flat `/{owner}/{repo}`. The full GitLab namespace
path is therefore flattened into the repo name with `-`:

```
gitlab.manjaro.org/packages/extra/element.io
  → github.com/manjaro-contrib/packages-extra-element.io       topics: packages, extra
```

Verified across all GitLab paths: 0 collisions, 0 invalid characters, longest name 79
chars (GitHub's limit is 100). The tool asserts these invariants and fails loudly rather
than mangling a name.

The hierarchy is preserved as **topics** — one per namespace segment, in path order.
Browse a group in the UI:

```
https://github.com/orgs/manjaro-contrib/repositories?q=topic:extra
```

or via the API:

```
https://api.github.com/search/repositories?q=org:manjaro-contrib+topic:extra
```

What topics do *not* restore: nesting (a repo tagged `packages` + `extra` does not encode
that `extra` lives inside `packages` — list order is the only hint) and per-group
permissions or teams.

## Incrementality

Each run does one `git ls-remote --heads --tags` per project and hashes the sorted refs.
That digest is compared against `state/refs.json`, committed in this repo. Only projects
whose digest changed are cloned and pushed. Git sync, topic sync and archive sync are
three independent flags, so a project that merely flipped its archived bit costs two API
calls and no clone.

### GitLab's rate budget sets the floor

`gitlab.manjaro.org` enforces `throttle_unauthenticated_git_http` at **60 anonymous git
requests per minute** (visible in the `ratelimit-*` headers on `info/refs`). It is a rate
budget, not a concurrency cap — bursts of any width pass until the budget is spent, then
every fetch returns `HTTP 429 / remote: Retry later`. Without a limiter a full sweep loses
hundreds of projects to 429s.

The tool therefore funnels **every** anonymous GitLab fetch — `ls-remote` and
`clone --mirror` alike — through one process-wide limiter at 55/min, and a 429 penalises
the whole pool rather than just the calling thread. Measured: 150 consecutive digests,
0 throttled, 164 s.

The practical consequence: the ref-scan phase alone costs roughly **85 minutes** for all
2039 projects (measured end to end: `pending=2029 empty=10 failed=0`), and that floor
applies to every run, including ones with nothing to sync. Effective throughput lands near
25/min rather than the nominal 55 — GitLab's budget refills more slowly than a naive
reading of the headers suggests. Worker counts are not a tuning knob here; only the
budget is.

## The `GH_MIRROR_TOKEN` secret

One repository secret, `GH_MIRROR_TOKEN`: a GitHub **fine-grained personal access token**
whose resource owner is the `manjaro-contrib` organization, with repository permissions:

| Permission | Level | Why |
| --- | --- | --- |
| Administration | Read and write | creates repos, and **this is what permits archiving/un-archiving** |
| Contents | Read and write | `git push` of refs |
| Metadata | Read-only | mandatory prerequisite, auto-selected by GitHub |

No GitLab credential is needed — every group is public and all API calls and clones work
anonymously. The built-in `GITHUB_TOKEN` cannot do the mirroring (it cannot create repos
outside its own scope); it is used only for the one state-file commit back to this repo.

Rotate the token at <https://github.com/settings/personal-access-tokens> and re-run:

```
gh secret set GH_MIRROR_TOKEN --repo manjaro-contrib/gitlab-sync
```

## Bootstrap

The first population creates ~2029 repos, and GitHub's secondary limit is 500
content-creating requests per hour. Dispatch the workflow manually with `limit: 450`,
wait an hour, repeat — **five runs** cover the whole set. Afterwards scheduled runs
create almost nothing and `limit: 0` is correct.

Budget the time: each run pays the ~85 min ref scan before it syncs anything, and the
clones share the same GitLab budget (~20 min for a 450-repo slice). A bootstrap run still
lands inside the workflow's 330 min timeout, but the margin is smaller than the repo count
alone suggests.

## Running locally

Python 3.11+, standard library only — no dependencies, no install step.

```
export GH_MIRROR_TOKEN=github_pat_…

python -m sync --dry-run                          # plan only; writes and pushes nothing
python -m sync --only packages/extra/element.io   # one project, for debugging
python -m sync --limit 450                        # bootstrap slice
python -m sync                                    # everything that changed
```

Exit code is `1` if any project failed, `0` otherwise. Every run ends with a summary line:

```
synced=N topics_only=N archived_changed=N skipped=N empty=N failed=N
```

## Schedule

Every 6 hours (`cron: "17 */6 * * *"`), chosen because the busiest group (`packages`) sees
several pushes per day. Runs are serialized by a `concurrency` group so two jobs never
race on `state/refs.json`. The state commit runs with `always()`, so a run killed by the
job timeout still records its partial progress.
