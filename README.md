# swfactory

`swfactory` is a controller for **other GitHub repositories**. It takes a target repository's open issues labeled `accept` in issue-number order. It uses the installed `codex exec` CLI to turn each issue into small GitHub sub-issues, implements them sequentially in a temporary worktree, runs task and integration tests, pushes a branch to the target repository, and opens a PR there. It adds `done` when implementation is ready and closes related issues after their PRs are merged. **It never merges the PR.**

## Setup

Requirements: Python 3, Git, SSH, `gh`, and an authenticated `codex` CLI. The Codex CLI is used through the console; no OpenAI API key is read by this program. GitHub API access needs a repository-specific `gh` login, set up below.

Codex runs inside a local Docker container so repository content and tests cannot read the controller's GitHub CLI settings or SSH key. Build the default image once on macOS or Linux with Docker running:

```sh
docker build -f docker/Codex.Dockerfile -t swfactory-codex:0.166.0 .
```

The image includes Codex CLI 0.166.0, Python, Git, ripgrep, `gh`, and `ssh`. The controller still keeps GitHub authentication and its SSH key outside the container. For another project's test dependencies, build an image with those tools and set `CODEX_CONTAINER_IMAGE` in `env`. Docker is required for every Codex call; if it or the selected image is unavailable, work stops without falling back to a host Codex process. The local Codex login must have a file-backed `auth.json` under `CODEX_HOME` (default `~/.codex`). On first use swfactory copies only that file to a private per-target Codex home, where token refreshes persist. [Official Codex authentication guidance](https://learn.chatgpt.com/docs/auth?surface=cli) describes file-backed login and its credential handling.

Create an untracked `env` file next to `swfactory.py`:

```text
GITHUB_SSH_KEY_PATH=~/.ssh/your-github-key
GITHUB_REPOGITORY=git@github.com:OWNER/TARGET-PROJECT.git
PROJECT_PATH=/absolute/path/to/TARGET-PROJECT
WORK_DIR=/absolute/path/for/issue-worktrees
OWNER_GITHUB_HANDLE=YOUR-GITHUB-USERNAME
CODEX_CONTAINER_IMAGE=swfactory-codex:0.166.0
```

Clone the target project once yourself, then set `PROJECT_PATH` to that checkout. `GITHUB_REPOGITORY` and `PROJECT_PATH` name the **target project**, not the `swfactory` source repository. `PROJECT_PATH` must be the root of an existing local Git checkout whose `origin` points to the same GitHub repository. `WORK_DIR` holds temporary worktrees and run files; it defaults to `/tmp` when omitted and must be outside `PROJECT_PATH`. `OWNER_GITHUB_HANDLE` is the GitHub username to mention for questions; if omitted, the repository owner's login is used. For organization-owned repositories, set it explicitly to a person. The SSH key is used for Git fetch and push. GitHub's issue, label, comment, and PR APIs require a separate authenticated `gh` account. Apply `accept` only to issues you authorize the local agent to implement.

Run `python3 swfactory.py --login` once in a terminal for each target repository and sign in to a GitHub account with issue and PR write access. This login is stored in a per-repository `GH_CONFIG_DIR` under `~/.config/swfactory/gh/`; it does not change the global `gh` login. The selected SSH key remains the Git fetch/push identity.

## Run

Run a manual, offline prerequisite check before starting the controller:

```sh
python3 swfactory.py doctor
python3 swfactory.py doctor --repository git@github.com:OWNER/ANOTHER-PROJECT.git --project-path /path/to/ANOTHER-PROJECT
```

Doctor prints configuration findings followed by Python, executable, SSH key, checkout root, origin, and work directory checks in a stable order. Each line shows PASS, FAIL, or UNKNOWN and an action or explanation. It exits with status 0 only when every required check passes; any FAIL or UNKNOWN gives status 1. It reads local configuration and Git metadata without creating a work directory or GitHub profile, contacting GitHub, or running Codex. PASS confirms local prerequisites only. It does not verify GitHub authentication, remote access, SSH push permission, or a successful `codex exec` run. `--repository` and `--project-path` override the corresponding `env` settings for this command too.

```sh
python3 swfactory.py                # process current accepted issues once
python3 swfactory.py --watch        # poll every 60 seconds
python3 swfactory.py --watch --interval 120
python3 swfactory.py --scan         # propose bug/refactor issues, then process accepted issues
python3 swfactory.py --scan-if-changed  # scan the latest default-branch commit once, if changed
python3 swfactory.py --project-path /path/to/ANOTHER-PROJECT --repository git@github.com:OWNER/ANOTHER-PROJECT.git
```

## Local dashboard

Run `python3 swfactory.py dashboard` and open `http://127.0.0.1:8765`. Use `--port` to choose another local port. The server listens only on `127.0.0.1`, has read-only HTTP endpoints, queries GitHub for current work state, and does not call Codex. It shows recent controller runs, active Codex calls, issue/task handoffs, test verification, PR creation, and failures. The browser refreshes every fifteen seconds; GitHub queries are cached for one minute. Current Codex work is sequential; each call gets a distinct ID, and the event model can represent more agents later without claiming they run in parallel today.

Select an issue from the decision or automation list to open its detailed flow. Each Codex call, when recorded, shows the categories and references of its inputs, its result record, status, and elapsed time. Plan hashes, sub-issue numbers, test categories, changed-entry counts, and PR links are recorded as output references. Failure reasons are safe categories with a link to the GitHub issue for the full owner-visible diagnostic. Long local timelines load older events on request. A performance-experiment section appears when measurement events exist; the optimizer that produces those events is separate work.

The dashboard also reads the selected repository's current GitHub issues, native sub-issue relationships, and PRs through its isolated `gh` login. The first screen is organized by **owner decisions** (answer, recover, review/merge, approve), then **automation** (running and accepted work), then unlinked PRs and completed work. Each row states the next action, child issue progress, and linked PR state. An issue page shows its delivery path from parent to child issue to PR, with recorded test evidence and agent handoffs when available. Missing local evidence is stated plainly rather than inferred from labels. GitHub data is refreshed at most once per minute and remains visible with a stale indication if a later query fails. The server only makes read-only GitHub queries. Issue bodies and comments are not retained in the SQLite database or shown as instructions to Codex. GitHub status and locally observed agent stages are labeled separately; older agent activity cannot be reconstructed from GitHub.

The SQLite file is stored at `PROJECT_PATH`'s Git common directory under `.git/swfactory/telemetry.sqlite3`. This keeps it with the local target checkout and shared across that checkout's worktrees, without adding a tracked project file. `WORK_DIR` continues to hold temporary worktrees. SQLite WAL lets the controller append events while the dashboard reads on the same host; do not place the Git directory on a network filesystem for this use. The history begins when this version first runs. It stores roles, issue/task numbers, source categories, status, and result references. It does not store raw prompts, issue bodies, comments, authentication, or complete command output. The dashboard reports local observations, so check GitHub for authoritative current issue and PR state. [SQLite WAL documentation](https://www.sqlite.org/wal.html).

The tool uses the existing local target checkout and creates temporary Git worktrees under `WORK_DIR`. `--repository` and `--project-path` override the target settings from `env` for one run. The `swfactory` source checkout is never used as the target workspace unless explicitly configured as the target. `--scan` fetches and inspects a detached snapshot of the target's latest default branch. `--scan-if-changed` does the same only when the remote commit or scan policy differs from the last successful scan, recorded under `WORK_DIR`; it is intended for scheduled use. Scanned findings require a source path, code identifier, and verbatim evidence. The tool checks both open and closed issues for a stable finding key and matching titles before posting, so reworded proposals for the same code location do not recur. It does not add `accept`; review each proposal and apply that label yourself. The key groups findings by kind, path, and identifier, so distinct findings in the same function and category should be reviewed within one issue.

See [container operation](docs/isolation-operations.md) for Docker setup, live boundary tests, and recovery steps.

Each Codex call receives only a temporary worktree, read-only Git metadata, a result directory when needed, and its dedicated Codex login. Rephrasing and scanning use detached snapshots, so untracked files in your `PROJECT_PATH` checkout (such as `env`) are never mounted for Codex. Read-only operations have a read-only Docker workspace; writable operations let Codex edit the mounted worktree. The Codex inner sandbox uses `danger-full-access` for both because nested Linux sandbox setup failed inside Docker. Docker bind mounts and the read-only container root enforce the file boundary. This mode does not grant access to host paths that were not mounted, and the Docker socket is not mounted. Scans require a repository file path and matching SHA-256 digest even when no issues are found, so a failed inspection cannot be recorded as a successful empty scan. The Codex login remains readable inside the container; treat that login as a credential and use a dedicated account if it must also be hidden from generated code.

## Rewrite an initial issue

Every open issue without `rephased` is rewritten on the next run, without requiring an initial label. Codex reads the target repository and rewrites the issue body with its understanding, rationale, implementation policy, tests, and verified references. The original body remains verbatim under `==== original ====` at the bottom. The tool then adds `rephased` and does not rewrite it again. This happens before implementation; only an `accept` label applied by the configured owner authorizes implementation. The tool checks the label event actor, not just the current label list. If essential details are missing, it asks the owner in an issue comment and waits for a reply. Other users' comments are excluded from Codex's requirement context.

The tool creates `accept`, `done`, `rephased`, `swfactory:running`, `swfactory:waiting`, `swfactory:failed`, `swfactory:subtask`, and `refactor` labels if needed. If information is missing, it comments with an `@` mention and adds `swfactory:waiting`. Reply **on that issue as `OWNER_GITHUB_HANDLE`**; the next run detects the answer, removes the waiting label, and replans. It posts a complete plan on the issue before implementation, creates GitHub sub-issues, and adds direct links to them in the parent issue body above `==== original ====`. It creates one PR for each sub-issue. The first PR targets the default branch; each later PR targets the previous task branch, so each diff contains only one task. Review and merge these dependent PRs in task order. Every implemented sub-task must add or extend an automated test case; verification runs basic, functional, and integration checks and records their commands and results in that task's PR. The final task runs the full parent integration suite. Completed implementation receives `done` while its issues stay open. On each trigger, swfactory closes a sub-issue only after its matching PR is merged, then closes the parent after all task PRs are merged. If a run fails, it comments on the issue and adds `swfactory:failed`; remove that label after fixing the cause to retry. A retry reuses an open task PR when its branch, base, and sub-issue number match, even if a fresh plan phrases the task differently. If the process is interrupted, remove a stale `swfactory:running` label to retry. Merging remains yours.

When a target repository requires `work-items.md` and `docs/{work number}.md`, each task PR gets its own work number and document after GitHub assigns its PR number. Issue and sub-issue numbers remain separate task identifiers linked from that PR's work record.

## Periodic execution

Use the one-shot command from cron. The local lock prevents overlapping runs of the same target repository. On this machine, a crontab entry can use:

```cron
# Once per hour
0 * * * * /bin/zsh -lc 'cd /Users/daemyung/develop/charsyam/swfactory && python3 swfactory.py >> /tmp/swfactory-cron.log 2>&1'

# Or once every two hours (use one entry, not both)
0 */2 * * * /bin/zsh -lc 'cd /Users/daemyung/develop/charsyam/swfactory && python3 swfactory.py >> /tmp/swfactory-cron.log 2>&1'
```

Install the chosen line with `crontab -e`. The scheduled job runs only when the machine is available; an always-on runner is needed if missed periods must be processed immediately. `--watch` is available for a continuously running process instead of cron.

To propose code-derived issues once per day, add a second entry at a different time after the version containing `--scan-if-changed` is deployed:

```cron
0 2 * * * /bin/zsh -lc 'cd /Users/daemyung/develop/charsyam/swfactory && python3 swfactory.py --scan-if-changed >> /tmp/swfactory-scan.log 2>&1'
```

Run local checks with `python3 -m unittest discover -s tests -v`.
