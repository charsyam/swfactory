# swfactory

`swfactory` is a controller for **other GitHub repositories**. It takes a target repository's open issues labeled `accept` in issue-number order. It uses the installed `codex exec` CLI to turn each issue into small GitHub sub-issues, implements them sequentially in a temporary worktree, runs task and integration tests, pushes a branch to the target repository, and opens a PR there. It closes completed sub-issues and adds `done` to the parent when the PR is ready. **It never merges the PR.**

## Setup

Requirements: Python 3, Git, SSH, `gh`, and an authenticated `codex` CLI. The Codex CLI is used through the console; no OpenAI API key is read by this program. GitHub API access needs a repository-specific `gh` login, set up below.

Create an untracked `env` file next to `swfactory.py`:

```text
GITHUB_SSH_KEY_PATH=~/.ssh/your-github-key
GITHUB_REPOGITORY=git@github.com:OWNER/TARGET-PROJECT.git
PROJECT_PATH=/absolute/path/to/TARGET-PROJECT
WORK_DIR=/absolute/path/for/issue-worktrees
OWNER_GITHUB_HANDLE=YOUR-GITHUB-USERNAME
```

Clone the target project once yourself, then set `PROJECT_PATH` to that checkout. `GITHUB_REPOGITORY` and `PROJECT_PATH` name the **target project**, not the `swfactory` source repository. `PROJECT_PATH` must be the root of an existing local Git checkout whose `origin` points to the same GitHub repository. `WORK_DIR` holds temporary worktrees and run files; it defaults to `/tmp` when omitted and must be outside `PROJECT_PATH`. `OWNER_GITHUB_HANDLE` is the GitHub username to mention for questions; if omitted, the repository owner's login is used. For organization-owned repositories, set it explicitly to a person. The SSH key is used for Git fetch and push. GitHub's issue, label, comment, and PR APIs require a separate authenticated `gh` account. Apply `accept` only to issues you authorize the local agent to implement.

Run `python3 swfactory.py --login` once in a terminal for each target repository and sign in to a GitHub account with issue and PR write access. This login is stored in a per-repository `GH_CONFIG_DIR` under `~/.config/swfactory/gh/`; it does not change the global `gh` login. The selected SSH key remains the Git fetch/push identity.

## Run

```sh
python3 swfactory.py                # process current accepted issues once
python3 swfactory.py --watch        # poll every 60 seconds
python3 swfactory.py --watch --interval 120
python3 swfactory.py --scan         # propose bug/refactor issues, then process accepted issues
python3 swfactory.py --project-path /path/to/ANOTHER-PROJECT --repository git@github.com:OWNER/ANOTHER-PROJECT.git
```

The tool uses the existing local target checkout and creates only an issue-specific temporary Git worktree under `WORK_DIR`. `--repository` and `--project-path` override the target settings from `env` for one run. The `swfactory` source checkout is never used as the target workspace unless explicitly configured as the target. `--scan` reads the local target checkout and proposes concrete bug/refactor issues there. It does not add `accept`; review each proposal and apply that label yourself. Existing issue titles are skipped to limit duplicate proposals.

## Rewrite an initial issue

Every open issue without `rephased` is rewritten on the next run, without requiring an initial label. Codex reads the target repository and rewrites the issue body with its understanding, rationale, implementation policy, tests, and verified references. The original body remains verbatim under `==== original ====` at the bottom. The tool then adds `rephased` and does not rewrite it again. This happens before implementation; only an `accept` label applied by the configured owner authorizes implementation. The tool checks the label event actor, not just the current label list. If essential details are missing, it asks the owner in an issue comment and waits for a reply. Other users' comments are excluded from Codex's requirement context.

The tool creates `accept`, `done`, `rephased`, `swfactory:running`, `swfactory:waiting`, `swfactory:failed`, `swfactory:subtask`, and `refactor` labels if needed. If information is missing, it comments with an `@` mention and adds `swfactory:waiting`. Reply **on that issue as `OWNER_GITHUB_HANDLE`**; the next run detects the answer, removes the waiting label, and replans. It posts a complete plan on the issue before implementation, creates GitHub sub-issues, and adds direct links to them in the parent issue body above `==== original ====`. If a run fails, it comments on the issue and adds `swfactory:failed`; remove that label after fixing the cause to retry. If the process is interrupted, remove a stale `swfactory:running` label to retry. Review and merge the PR yourself.

## Periodic execution

Use the one-shot command from cron. The local lock prevents overlapping runs of the same target repository. On this machine, a crontab entry can use:

```cron
# Once per hour
0 * * * * /bin/zsh -lc 'cd /Users/daemyung/develop/charsyam/swfactory && python3 swfactory.py >> /tmp/swfactory-cron.log 2>&1'

# Or once every two hours (use one entry, not both)
0 */2 * * * /bin/zsh -lc 'cd /Users/daemyung/develop/charsyam/swfactory && python3 swfactory.py >> /tmp/swfactory-cron.log 2>&1'
```

Install the chosen line with `crontab -e`. The scheduled job runs only when the machine is available; an always-on runner is needed if missed periods must be processed immediately. `--watch` is available for a continuously running process instead of cron.

Run local checks with `python3 -m unittest discover -s tests -v`.
