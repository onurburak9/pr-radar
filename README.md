# PR radar

One local view of every open GitHub pull request you have a stake in, sorted by whose move it is. It catches the PRs that slip through the cracks:

- a PR that was **dropped from the merge queue**
- **CI went red** on a PR you care about
- a PR **assigned to you** (say, one a coding agent built for you) is finished and waiting for your review
- someone **requested your review** or **left a new comment**

A small Python server on your Mac polls GitHub and serves two views of the same data:

- a **menu-bar item** (via [SwiftBar](https://github.com/swiftbar/SwiftBar)) that shows `PR ● 3` when 3 PRs need you, and lists them on click
- a **page** at http://localhost:7777/ with filters (repo, your relation, status, free text) and column sorting

It shows things only. It sends no notifications and never writes to GitHub.

## Setup

Requirements: macOS, [uv](https://docs.astral.sh/uv/), the [GitHub CLI](https://cli.github.com/) logged in to the account you want to track, and Homebrew for SwiftBar.

```bash
brew install --cask swiftbar
gh auth login                      # if you haven't already

git clone <this repo> pr-radar && cd pr-radar
cp config.example.toml config.toml
$EDITOR config.toml                # set github_login and repos
make install
```

`make install`:

1. installs the Python environment with `uv sync`
2. writes a launchd agent at `~/Library/LaunchAgents/com.pr-radar.plist`, so the server starts at login and restarts if it crashes
3. writes the SwiftBar plugin to `./swiftbar/` and points SwiftBar at it, or links the plugin into your existing SwiftBar plugin folder
4. adds SwiftBar to your Login Items and restarts it

Within a minute the menu bar shows `PR ○`, `PR ● N` or `PR ⚠`. Re-running `make install` is safe.

To try it without installing anything: `make once` polls once and prints the counts, and `make run` runs the server in the foreground.

### Config

`config.toml` is gitignored. Copy it from [`config.example.toml`](config.example.toml), which documents every key. Unknown keys are rejected at startup, so typos fail loudly. Run `make restart` after editing.

| Key | What it does |
|---|---|
| `github_login` | The account whose token is used and whose involvement decides what's listed |
| `repos` | `owner/name` repos to watch |
| `poll_interval_seconds` | Default 120, minimum 30 |
| `port` | Default 7777 |
| `stale_days` | No activity for this many days moves a PR to Parked |
| `merged_visible_hours` | How long a merged PR stays visible |
| `assigned_ready_authors` | Limit "Assigned, ready for review" to these authors. Empty (default) means any author |
| `bot_logins_ignored_for_comments` | Logins whose comments never count as new |
| `ignore_checks` | Check names that never count as failed or running, e.g. a pending approval status |
| `include_team_review_requests` | Also count review requests sent to your teams |

### Day to day

| Command | |
|---|---|
| `make status` | Health, last poll cost, server memory and CPU |
| `make logs` | Follow `~/Library/Logs/pr-radar.log` (rotates at 1 MB, keeps 3) |
| `make restart` | Pick up `config.toml` edits |
| `make test` | Unit tests |
| `make uninstall` | Remove the login item and the plugin. Keeps your data |

## How it works

```
GitHub GraphQL ──poll every 2 min──▶ poller ──▶ SQLite (pr-radar.sqlite)
                                                    │
                                     classify each PR into a status
                                                    │
                          ┌─────────────────────────┴──────────────────┐
                 GET /swiftbar (menu bar)                    GET / and /api/state (page)
```

### Which PRs are listed

Each poll runs one GitHub search per relation, limited to your repos and open PRs: `author:`, `assignee:`, `reviewed-by:`, `commenter:` and `user-review-requested:`. The results are merged, and each PR records how you're involved, for example "you: author, reviewer". A second, batched query fetches the details for all of them: checks, merge-queue state and history, reviews, comments and commits.

A PR that was open last poll but dropped out of the search is fetched once more, so a merge or close shows up as **Merged** instead of the PR just disappearing.

### Status: first match wins

| Status | Group | Meaning |
|---|---|---|
| Merged | Merged | Merged within `merged_visible_hours` |
| Dropped from merge queue | Needs you | The latest merge-queue event is a removal you didn't do, with no new commit since. Shows GitHub's reason, e.g. failed checks or timed out |
| CI failed | Needs you | A check failed and none of its re-runs passed. Cancelled runs and `ignore_checks` don't count |
| In merge queue | In flight | Shows the queue position |
| Assigned, ready for review | Needs you | Assigned to you and written by someone else (limited to `assigned_ready_authors` if set), not a draft, checks finished, and you haven't reviewed the current head commit |
| Review requested | Needs you | Your review is requested |
| New comments | Needs you | Comments from other people since you last opened it from PR radar |
| Waiting on others | In flight | Everything else |

A PR is **Parked** if you clicked Hide on it, or if it has had no activity for `stale_days`. It comes back on its own once something new happens. Opening a PR through the page or the menu goes via `/open/...`, which marks it seen and then redirects to github.com. That's what clears "New comments".

### Keeping polls cheap

A PR can carry 100+ check runs. The poller first reads per-state check counts, which are cheap. It lists individual check names only when something is red or still running, caches that list per head commit, and skips it entirely for parked PRs. A typical poll of ~80 PRs costs about 10 GraphQL points, roughly 300 an hour at the default interval, out of GitHub's 5,000. The server uses about 30 MB of memory and a few seconds of CPU an hour.

### When things go wrong

The health state shows on the page and in the menu:

- **Grey "refreshing"**: just woke from sleep, or one network failure. It retries within 15 seconds. The loop checks the wall clock every 10 seconds, so it polls promptly after a wake.
- **Red `PR ⚠`**: an auth error (check `gh auth status`), two failures in a row, or a poll loop that has stopped. The last known data stays visible.
- **Server down**: the SwiftBar plugin shows `PR ⚠` with *Restart server* and *Open log*.

### Security

- The server listens on `127.0.0.1` only.
- State-changing requests (refresh, hide, unhide) need an `X-PR-Radar: 1` header. Other web pages can't send it without a CORS preflight, which the server doesn't grant.
- `/open/...` redirects only to a `github.com` URL the server builds itself.
- The GitHub token is read from `gh auth token` on each start and never written to disk.

### Layout

```
src/pr_radar/
  config.py    config.toml -> typed Config
  github.py    GraphQL client: searches, batched detail fetch, check listing
  status.py    pure classify(pr) -> status/group (most of the tests are here)
  poller.py    poll loop, health, snapshot for the UI
  store.py     SQLite: PRs, seen/hidden marks, poll metadata
  server.py    HTTP: page, JSON API, SwiftBar menu, /open redirects
  static/index.html  the page (vanilla JS, no build step)
scripts/install.sh / uninstall.sh
```

Python 3.11+, standard library only at runtime.

## License

MIT. See [LICENSE](LICENSE).
