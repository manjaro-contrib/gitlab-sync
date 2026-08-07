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
everything including archived projects with `--include-stale`. Both are also
`workflow_dispatch` inputs, so a one-off wider sync needs no code change:

| dispatch input | scope (measured 2026-08-07) |
| --- | --- |
| defaults | 619 |
| `max_age_days: 365` | 447 |
| `max_age_days: 0` (age check off) | 1464 |
| `include_stale: true` | 2039 |

The scheduled run always uses the defaults — dispatch inputs are `null` on a `schedule`
trigger, so the fallbacks in the workflow apply.

Nothing is ever deleted on GitHub. A project that falls out of scope — archived, gone
dormant, or removed upstream — keeps its mirror as-is.

Mirrors have issues, wikis, projects **and Actions** disabled — they are backups, and
discussion belongs on GitLab. Actions matters most: upstream repos carry their own
`.github/workflows`, and GitHub would otherwise schedule them here, running Manjaro's CI
out of the mirror org on every push and cron.

### The token needs `Workflows: Read and write`

Some projects keep `.github/workflows` files on non-default branches (`applications/calamares`
has 5-8 of them on its `*-stable` branches). Pushing any commit that touches those paths is
rejected pre-receive unless the token carries the workflow permission:

```
! [remote rejected] 3.2.x-stable -> 3.2.x-stable (refusing to allow a Personal Access
  Token to create or update workflow `.github/workflows/issues.yml` without `workflow` scope)
```

This is enforced on the **token**, not the repository — disabling Actions on the mirror does
not lift it (verified). Without the permission those projects fail every run, and they fail
*after* the clone, so the cost is paid before the rejection.

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

### Capped runs stop scanning early

With `--limit N`, the scan runs in most-recently-active order and stops as soon as N
projects are found needing work — there is no point paying ~1.7 s per `ls-remote` for
projects the run cannot get to anyway. Measured: a `--limit 5` run scanned **5 of 310**
instead of all 310.

This makes the hourly schedule cheap even while a backlog drains: the run finds its 450
and stops, rather than scanning every project first.

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

No manual procedure needed: the hourly schedule drains the backlog by itself. The first
population is ~619 repos against GitHub's 500 content-creations/hour, so early runs create
what they can and report the rest as `deferred`; each following hour picks those up until
the count reaches zero.

Observed on the first real run: `synced=207 deferred=192 skipped=117 failed=1` — the
deferrals are the rate limiter working, not errors, and they cost nothing because progress
is already recorded in `state/refs.json`.

A first run pays a ~25 min ref scan for the 619 in-scope projects; later runs skip most of
it via `last_activity_at`.

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

Hourly, alternating scope:

| hours | cron | scope |
| --- | --- | --- |
| odd | `17 1-23/2 * * *` | live only — 619 projects |
| even | `17 0-22/2 * * *` | `--include-stale` — all 2039, archived included |

The odd-hour runs keep active packages close to upstream. The even-hour runs pick up
archived and long-dormant projects, which is also what keeps the **archived bit tracking
GitLab**: a project archived upstream drops out of the live scope entirely, so without the
stale pass nothing would ever notice the change.

Between them every hour is covered exactly once — the two crons do not overlap.

`timeout-minutes: 55` is deliberately under the interval: runs are serialized by a
`concurrency` group, so a job that hung for hours would block every trigger behind it.
Better to lose one run than stall the schedule.

The scheduled `--limit 450` sits under GitHub's 500 content-creations/hour. That pairs
with the hourly cadence: while a backlog of new repos is draining, each run creates what
it can and defers the rest, and the next hour picks them up — no manual bootstrap loop.

The state commit runs with `always()`, so a run killed by the timeout still records its
partial progress.
