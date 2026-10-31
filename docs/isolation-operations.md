# Codex container operation

## Purpose

The controller retains GitHub API credentials and the configured SSH key on the host. Codex receives a temporary target worktree, read-only Git metadata, an optional result directory, and its own file-backed Codex login. No Docker socket or host home directory is mounted.

## First-time setup on macOS or Linux

1. Install and start Docker. Confirm `docker info` succeeds for the account that runs swfactory and its cron entry.
2. Sign in to the local Codex CLI with ChatGPT. Confirm `codex login status` reports a login and that `auth.json` exists under `CODEX_HOME` (normally `~/.codex`). If the login is stored only in the OS keychain, select file-backed storage and sign in again. Do not put an OpenAI API key into swfactory's `env` file.
3. From the swfactory source checkout, build `docker build -f docker/Codex.Dockerfile -t swfactory-codex:0.166.0 .`. The Dockerfile pins the Codex CLI version. For a target project that needs extra test tools, extend this image and set `CODEX_CONTAINER_IMAGE` in `env` to the resulting tag.
4. Run `SWFACTORY_DOCKER_INTEGRATION=1 python3 -m unittest discover -s tests -q`. This verifies fake GitHub and SSH secrets outside the mounts stay inaccessible and exercises the real Codex CLI with the local ChatGPT login. The ordinary test suite does not require Docker or a model call.
5. Run the normal one-shot trigger. The first Codex call creates a private auth cache under `~/.config/swfactory/codex-auth/` for that target's Git repository. It copies only `auth.json`; Codex refreshes that isolated cache on later calls.

## What the boundary permits

The container root filesystem is read-only. The worktree is read-only for rephrase, scan, and plan calls, and writable for implementation and verification. Git metadata is always mounted read-only. Only the Codex login and result directory have separate writable mounts. The controller's `GH_CONFIG_DIR`, GitHub tokens, SSH key, and SSH agent are absent from the container environment and mounts.

Codex runs with its inner `danger-full-access` mode because its Linux sandbox cannot initialize reliably inside this container. The Docker mounts above enforce the read-only and writable boundaries. A scan must return a source file path and SHA-256 digest matching the snapshot; otherwise the controller fails the scan and leaves its commit eligible for retry.

For writable calls, Codex uses its `danger-full-access` inner mode because the nested `workspace-write` mode rejected file writes in the tested Mac Docker runtime. The outer Docker container still limits host file access to the explicit mounts. The Codex login is available to code running in the container; use a dedicated Codex account or a narrower authentication method if that credential also needs protection from generated code.

## Failures and alternatives

- Missing Docker engine or selected image: build/start it first. swfactory does not launch Codex on the host as a fallback.
- Missing file-backed Codex login: use `codex login` with file storage under `CODEX_HOME`. Copying an existing host login is performed once into the private per-target cache; a keychain-only login is not automatically exported.
- Target tests require extra packages: build a custom image from the provided Dockerfile and set `CODEX_CONTAINER_IMAGE`.
- A project needs files outside its worktree or external service credentials: explicitly revise the isolation policy before adding any mount or credential; the current implementation intentionally excludes them.
- A separate OS account can replace Docker as a host file boundary, but requires account provisioning and a Codex login for that identity on both operating systems.

Official OpenAI documentation: [authentication](https://learn.chatgpt.com/docs/auth?surface=cli), [non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode), [sandbox security principles](https://developers.openai.com/api/docs/guides/agents-api/environments/security).
