# PATBTAWO

Provider-Agnostic Task-Board-To-Agent Workflow Orchestrator.

PATBTAWO works with Asana, Trello, ClickUp, Jira, monday.com, and custom systems.
Each task attempt runs isolated builder, verifier, and deployer stages; with
objective verification enabled, the verifier gets a fresh worktree derived from
the builder checkpoint instead of inheriting the builder's runtime context.
The Ready queue is consumed top-down using provider-native position or rank data
where the provider exposes it.

## Prerequisites

- [Python 3.10+](https://www.python.org/downloads/) for installing and running
  the package.
- [Git](https://git-scm.com/downloads) with worktree support.
- A task board in Asana, Trello, ClickUp, Jira, monday.com, or a custom command
  provider, with lifecycle states for `Ready`, `Building`, `Verifying`,
  `Deploying`, `Done`, `Failed`, and `Blocked`.
- Provider credentials:
  [Asana PAT](https://developers.asana.com/docs/personal-access-token),
  [Trello API key/token](https://support.atlassian.com/trello/docs/getting-started-with-trello-rest-api/),
  [ClickUp personal token](https://developer.clickup.com/docs/authentication),
  [Jira API token](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/),
  or [monday.com API token](https://developer.monday.com/api-reference/docs/authentication).
- At least one stage runner command, such as Codex, Claude Code, BMAD,
  LangGraph, a shell script, or a CI wrapper. Each stage command must write the
  required JSON report to `ORCHESTRATOR_REPORT_PATH`.

You can run PATBTAWO directly from a checkout without installing it:

```sh
python -m patbtawo --help
```

That is the safest path on Windows because it avoids generated console-command
launcher `.exe` files, which can trigger antivirus scans even when clean.

Optional local install:

```sh
python -m pip install .
```

The package intentionally does not install console scripts; use
`python -m patbtawo` after installation too.

If you installed an older version that created `patbtawo.exe`,
`task-orchestrator.exe`, or `asana-orchestrator.exe`, remove those launchers with:

```sh
python -m pip uninstall patbtawo
```

Run from inside the git repository that should receive fresh per-task worktrees:

```sh
python -m patbtawo --validate-config --print-config
python -m patbtawo
```

Use `python -m patbtawo --once` when you want a diagnostic run that processes
at most one Ready task and exits. Without `--once`, PATBTAWO keeps selecting
the next Ready task until the queue is empty, even when an earlier task finishes
as Failed or Blocked.

Before starting a real board run, make sure the builder and verifier commands in
`.env` point at real agent commands or scripts in the target repository. The
commands must write JSON to `ORCHESTRATOR_REPORT_PATH`; validation catches
obvious missing local paths before tasks are moved.

Deployment is opt-in. Set `ORCHESTRATOR_DEPLOY_ENABLED=true`, provide
`ORCHESTRATOR_DEPLOYER_AGENT_COMMAND` or `ORCHESTRATOR_DEPLOY_COMMAND`, and pass
target details such as `ORCHESTRATOR_DEPLOY_HOST` through `.env`.

Configuration and provider setup are documented in
[docs/task-orchestrator.md](docs/task-orchestrator.md), with a starter
environment file in [.env.example](.env.example).

## Setup Prompt

Paste this into your coding agent from inside the repository you want PATBTAWO to
operate on:

```text
Set up PATBTAWO for this repository. Inspect the existing build, test, and
deploy commands. Create or update local builder, verifier, and optional deployer
stage scripts so each one writes the required JSON report to
ORCHESTRATOR_REPORT_PATH. The verifier must be objective and read-only. Prepare
a .env from .env.example for my chosen provider, leaving secrets blank and
documenting exactly which IDs/tokens I need to fill in. Do not commit secrets.
Run the tests or closest available sanity checks and summarize the final
commands I should use.
```

## Naming

PATBTAWO is intentionally descriptive and admittedly not final. If you fork this
project and have a better name, open an issue or include the proposal in a pull
request with the suggested rename, rationale, and any docs/package updates needed
to make the change reviewable.
