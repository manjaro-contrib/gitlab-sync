# gitlab-sync

Mirrors every non-empty public project from [gitlab.manjaro.org](https://gitlab.manjaro.org)
into the [`manjaro-contrib`](https://github.com/manjaro-contrib) GitHub organization.

## Scope

**619** of GitLab's 2039 projects are mirrored — the ones that are actually alive.
Excluded:

| excluded | count | why |
| --- | --- | --- |
| archived upstream | 575 | nobody maintains them |
| untouched > 2 years | 845 | dormant; costs creation quota and scan time the live packages need |
| empty | 10 | advertise no refs, so a push fails and the mirror is noise |

Emptiness is detected per run by ref digest, not by GitLab's `empty_repo` flag, so a
project that gains its first commit is picked up automatically. Likewise a dormant project
that receives a push re-enters scope on the next run by itself.

Override the cutoff with `--max-age-days N` (`0` disables the age check), or mirror
everything including archived projects with `--include-stale`.

Nothing is ever deleted on GitHub. A project that falls out of scope — archived, gone
dormant, or removed upstream — keeps its mirror as-is.

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

Every mirror carries the **`gitlab-mirror`** topic, so the repos this action owns can be
told apart from the org's hand-made ones:

```
https://github.com/orgs/manjaro-contrib/repositories?q=topic:gitlab-mirror
```

The hierarchy is preserved as **topics** — one per namespace segment, in path order,
after the marker.
Browse a group in the UI:

```
https://github.com/orgs/manjaro-contrib/repositories?q=topic:extra
```

or via the API:

```
https://api.github.com/search/repositories?q=org:manjaro-contrib+topic:extra
```

Note: an org automation stamps freshly created `manjaro-contrib` repos with its own
topics (`arch`, `package`) a second or two after creation, which silently clobbers a
single `PUT`. `set_topics()` therefore re-reads and re-applies until the intended topics
stick — without that, every newly mirrored repo ends up mistagged.

What topics do *not* restore: nesting (a repo tagged `packages` + `extra` does not encode
that `extra` lives inside `packages` — list order is the only hint) and per-group
permissions or teams.

## Incrementality

Each run does one `git ls-remote --heads --tags` per project and hashes the sorted refs.
That digest is compared against `state/refs.json`, committed in this repo. Only projects
whose digest changed are cloned and pushed. Git sync, topic sync and archive sync are
three independent flags, so a project that merely flipped its archived bit costs two API
calls and no clone.

### Skipping the scan entirely

GitLab bumps `last_activity_at` on push, and it is already present in the enumeration
response. If it has not moved since the last *fully successful* sync, the refs cannot have
changed, so the `ls-remote` is skipped outright. Most Manjaro projects are dormant, so a
steady-state run scans a small fraction of the 2039 and finishes in minutes rather than
the ~85 min a full scan costs.

The skip is deliberately conservative — it only applies when the stored topics and
archived bit also match, so a project whose previous run failed is always retried even
though upstream has been quiet since. Entries written before this existed have no
`last_activity_at` and are re-scanned once.

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

The practical consequence: a full scan of all 2039 projects took ~85 min end to end
(measured: `pending=2029 empty=10 failed=0`). Scoping to the 619 live projects cuts that to
roughly 25 min, and `last_activity_at` skipping removes most of what remains. Effective
throughput lands near 25/min rather than the nominal 55 — GitLab's budget refills more
slowly than a naive reading of the headers suggests. Worker counts are not a tuning knob
here; only the budget is.

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

## GitHub's creation ceiling

Repo creation is bound by GitHub's secondary rate limit of **500 content-creating requests
per hour**. A run that tries to create more than that gets `You have exceeded a secondary
rate limit`, which arrives with neither `Retry-After` nor an exhausted primary quota — so
it looks like a hard error unless specifically recognised.

The client detects it, backs off, and then stops attempting further creations for the rest
of the run. Those projects are reported as `deferred`, not `failed`, and the next run picks
them up: partial progress is already recorded in `state/refs.json`, so nothing is redone.
A deferred project does not fail the run's exit code.

## Bootstrap

The first population creates ~619 repos, and GitHub's secondary limit is 500
content-creating requests per hour. Dispatch the workflow with `limit: 450`, wait an hour,
then run it again — **two runs** cover the whole set. Afterwards scheduled runs create
almost nothing and `limit: 0` is correct.

Budget the time: a first run pays a ~25 min ref scan for the 619 in-scope projects, and
the clones share the same GitLab budget. Later runs skip most of that scan via
`last_activity_at` and finish in minutes.

## Running locally

Managed with [uv](https://docs.astral.sh/uv/). `uv run` installs the interpreter pinned in
`.python-version` on first use, so there is nothing to set up by hand.

The tool itself is **standard library only** — `dependencies = []` in `pyproject.toml` is
deliberate, not an oversight. A sync run needs no resolution step and cannot break because
of a dependency release.

```
export GH_MIRROR_TOKEN=github_pat_…

uv run python -m sync --dry-run                          # plan only; writes and pushes nothing
uv run python -m sync --only packages/extra/element.io   # one project, for debugging
uv run python -m sync --limit 450                        # bootstrap slice
uv run python -m sync                                    # everything that changed
```

`uv run gitlab-sync` works too, via the console script.

Both long phases report progress every 25 projects with a rate and ETA, so a run is never
silent for more than a minute or two:

```
enumerating GitLab projects...
enumerated 2039 projects
scanning refs: 500/2039 (24/min, eta 64m)
syncing: 25/450 (12/min, eta 35m)
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
