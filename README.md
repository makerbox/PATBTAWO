# PATBTAWO

Provider-Agnostic Task-Board-To-Agent Workflow Orchestrator.

PATBTAWO works with Asana, Trello, ClickUp, Jira, monday.com, and custom systems.
Each task attempt runs isolated builder, verifier, and deployer stages; with
objective verification enabled, the verifier gets a fresh worktree derived from
the builder checkpoint instead of inheriting the builder's runtime context.

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

Install locally:

```sh
python -m pip install .
```

Run from inside the git repository that should receive fresh per-task worktrees:

```sh
patbtawo --validate-config --print-config
patbtawo --once
```

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
