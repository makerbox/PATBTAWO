# PATBTAWO

PATBTAWO is a Provider-Agnostic Task-Board-To-Agent Workflow Orchestrator. It
moves one task at a time through a deterministic build lifecycle using a
pluggable task provider. It supports Asana, Trello, ClickUp, Jira, monday.com,
and a command provider for any other system.

Implementation note: this workspace did not contain project files, package
metadata, scripts, docs, or git metadata when the orchestrator was first added.
There were no existing build, test, or deploy entry points to reuse, so the
orchestrator is command-driven through environment variables. Run it from the
real git repository that should receive per-task worktrees.

Provider adapters were aligned to these official API surfaces:

- [Asana tasks from a section](https://developers.asana.com/reference/gettasksforsection),
  [add task to section](https://developers.asana.com/reference/addtaskforsection),
  and [create story/comment](https://developers.asana.com/reference/createstoryfortask)
- [Trello cards API](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)
  and [lists API](https://developer.atlassian.com/cloud/trello/rest/api-group-lists/)
- [ClickUp get tasks](https://developer.clickup.com/reference/gettasks),
  [update task](https://developer.clickup.com/reference/updatetask), and
  [create task comment](https://developer.clickup.com/reference/createtaskcomment)
- [Jira issue search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/),
  [issue transitions](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/),
  and [issue comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/)
- [monday.com items by column values](https://developer.monday.com/api-reference/reference/items-page-by-column-values),
  [change column values](https://developer.monday.com/api-reference/reference/columns), and
  [updates/comments](https://developer.monday.com/api-reference/reference/updates)

## Install

From this repository, you can run PATBTAWO without installing it:

```sh
python -m patbtawo --help
```

That is the safest path on Windows because it avoids generated console-command
launcher `.exe` files, which can trigger antivirus scans even when clean.

Optional local install:

```sh
python -m pip install .
```

For editable local development:

```sh
python -m pip install -e .
```

The package intentionally does not install console scripts. Use
`python -m patbtawo` after installation too.

If you installed an older version that created `patbtawo.exe`,
`task-orchestrator.exe`, or `asana-orchestrator.exe`, remove those launchers with:

```sh
python -m pip uninstall patbtawo
```

## Lifecycle

The orchestrator processes exactly one Ready task at a time:

1. Fetch the first task from the configured Ready state.
2. Read the full task contract from the provider.
3. Create a fresh builder worktree from `ORCHESTRATOR_BASE_BRANCH`.
4. Move the task to Building.
5. Run the builder agent command in the builder worktree.
6. Require the builder JSON report.
7. Checkpoint the builder's file changes.
8. Move passing builds to Verifying.
9. Create a fresh verifier worktree from the checkpoint.
10. Run the verifier agent command in that isolated verifier worktree.
11. Require the verifier JSON report and capture a verifier log.
12. Fail verification if the verifier modifies tracked files.
13. If deploy is enabled, create a fresh deployer worktree from the verified
    checkpoint, move the task to Deploying, and run deploy/smoke.
14. Move successful tasks to Done.
15. Move failed tasks to Failed.
16. Move externally blocked tasks to Blocked.
17. Comment concise status back to the provider.
18. Retry retryable/transient stage failures with a fresh worktree until the
    retry limit is exhausted.
19. Continue until the Ready queue is empty.

State is stored in `.orchestrator/artifacts/orchestrator_state.json` by default.
If the process is interrupted, the next run removes the incomplete worktree,
creates a fresh one for the same task, and continues.

## Common Configuration

Copy `.env.example` to `.env`, then set:

```sh
ORCHESTRATOR_PROVIDER=asana
ORCHESTRATOR_STATE_READY_ID=
ORCHESTRATOR_STATE_BUILDING_ID=
ORCHESTRATOR_STATE_VERIFYING_ID=
ORCHESTRATOR_STATE_FAILED_ID=
ORCHESTRATOR_STATE_DEPLOYING_ID=
ORCHESTRATOR_STATE_DONE_ID=
ORCHESTRATOR_STATE_BLOCKED_ID=
ORCHESTRATOR_BUILDER_AGENT_COMMAND=codex run ./agents/developer.md
ORCHESTRATOR_VERIFIER_AGENT_COMMAND=codex run ./agents/verifier.md
```

Provider-specific aliases are supported. For example, Asana can use
`ASANA_SECTION_READY_GID`, Trello can use `TRELLO_LIST_READY_ID`, ClickUp can use
`CLICKUP_STATUS_READY`, Jira can use `JIRA_TRANSITION_BUILDING_ID`, and monday.com
can use `MONDAY_STATUS_BUILDING`.

Optional local behavior:

- `ORCHESTRATOR_RETRY_LIMIT`, default `0`
- `ORCHESTRATOR_DRY_RUN`, default `false`
- `ORCHESTRATOR_BASE_BRANCH`, default current git branch or `HEAD`
- `ORCHESTRATOR_WORKTREE_ROOT`, default `../.<repo>-orchestrator-worktrees`
- `ORCHESTRATOR_ARTIFACT_ROOT`, default `.orchestrator/artifacts`
- `ORCHESTRATOR_STAGE_TIMEOUT_SECONDS`
- `ORCHESTRATOR_KEEP_WORKTREES`, default `false`
- `ORCHESTRATOR_OBJECTIVE_VERIFIER`, default `true`
- `ORCHESTRATOR_DEPLOY_ENABLED`
- `ORCHESTRATOR_BUILDER_AGENT_COMMAND`
- `ORCHESTRATOR_VERIFIER_AGENT_COMMAND`
- `ORCHESTRATOR_DEPLOYER_AGENT_COMMAND`
- `ORCHESTRATOR_BUILDER_COMMAND`, backward-compatible alias
- `ORCHESTRATOR_VERIFIER_COMMAND`, backward-compatible alias
- `ORCHESTRATOR_DEPLOY_COMMAND`
- `ORCHESTRATOR_SMOKE_COMMAND`

Deploy runs only when enabled or when a deploy/smoke command is set. If both
deploy and smoke commands are set, they run as one deployer stage joined with
`&&`; the stage must still write one deployer report.

The `*_AGENT_COMMAND` variables are plain shell commands. They may launch Codex,
Claude Code, BMAD, LangGraph, local scripts, CI wrappers, or any other executable
workflow. The orchestrator starts a fresh subprocess for each stage and passes a
fresh `ORCHESTRATOR_SUBAGENT_ID` plus the stage role through the environment.

Do not leave the backward-compatible aliases pointed at placeholder scripts such
as `python scripts/builder.py` unless those files exist in the target repository.
`python -m patbtawo --validate-config` checks obvious local path references before
any task is moved, so a missing stage script fails fast instead of sending every
Ready task to Failed.

## Providers

Asana:

```sh
ORCHESTRATOR_PROVIDER=asana
ASANA_ACCESS_TOKEN=
ASANA_PROJECT_GID=
ASANA_SECTION_READY_GID=
ASANA_SECTION_BUILDING_GID=
ASANA_SECTION_VERIFYING_GID=
ASANA_SECTION_FAILED_GID=
ASANA_SECTION_DEPLOYING_GID=
ASANA_SECTION_DONE_GID=
ASANA_SECTION_BLOCKED_GID=
```

Trello:

```sh
ORCHESTRATOR_PROVIDER=trello
TRELLO_API_KEY=
TRELLO_TOKEN=
TRELLO_BOARD_ID=
TRELLO_LIST_READY_ID=
TRELLO_LIST_BUILDING_ID=
TRELLO_LIST_VERIFYING_ID=
TRELLO_LIST_FAILED_ID=
TRELLO_LIST_DEPLOYING_ID=
TRELLO_LIST_DONE_ID=
TRELLO_LIST_BLOCKED_ID=
```

ClickUp:

```sh
ORCHESTRATOR_PROVIDER=clickup
CLICKUP_ACCESS_TOKEN=
CLICKUP_LIST_ID=
CLICKUP_STATUS_READY=Ready
CLICKUP_STATUS_BUILDING=Building
CLICKUP_STATUS_VERIFYING=Verifying
CLICKUP_STATUS_FAILED=Failed
CLICKUP_STATUS_DEPLOYING=Deploying
CLICKUP_STATUS_DONE=Done
CLICKUP_STATUS_BLOCKED=Blocked
```

ClickUp lifecycle values may be either status names (`Ready`) or status IDs
(`p901...`). PATBTAWO resolves status IDs through the List metadata before
filtering tasks or moving tasks.

Jira:

```sh
ORCHESTRATOR_PROVIDER=jira
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_EMAIL=
JIRA_API_TOKEN=
JIRA_PROJECT_KEY=
JIRA_STATUS_READY=Ready
JIRA_TRANSITION_BUILDING_ID=
JIRA_TRANSITION_VERIFYING_ID=
JIRA_TRANSITION_FAILED_ID=
JIRA_TRANSITION_DEPLOYING_ID=
JIRA_TRANSITION_DONE_ID=
JIRA_TRANSITION_BLOCKED_ID=
```

For Jira, lifecycle values after Ready may be transition IDs, transition names,
or destination status names. Set `JIRA_READY_JQL` to override the default Ready
query. The adapter tries Jira's enhanced `/search/jql` endpoint and falls back
to `/search`; set `JIRA_SEARCH_ENDPOINT` if your Jira site requires a specific
search path.

monday.com:

```sh
ORCHESTRATOR_PROVIDER=monday
MONDAY_API_TOKEN=
MONDAY_BOARD_ID=
MONDAY_STATUS_COLUMN_ID=status
MONDAY_STATUS_READY=Ready
MONDAY_STATUS_BUILDING=Building
MONDAY_STATUS_VERIFYING=Verifying
MONDAY_STATUS_FAILED=Failed
MONDAY_STATUS_DEPLOYING=Deploying
MONDAY_STATUS_DONE=Done
MONDAY_STATUS_BLOCKED=Blocked
```

The monday.com adapter treats lifecycle values as labels in a status column.

Command provider:

```sh
ORCHESTRATOR_PROVIDER=command
ORCHESTRATOR_COMMAND_NEXT_TASK=./provider-next
ORCHESTRATOR_COMMAND_GET_TASK=./provider-get
ORCHESTRATOR_COMMAND_MOVE_TASK=./provider-move
ORCHESTRATOR_COMMAND_COMMENT_TASK=./provider-comment
```

`NEXT_TASK` and `GET_TASK` commands must print a JSON object with at least an
`id`, `gid`, or `key`; optional fields include `name`, `title`, `summary`,
`notes`, `description`, and `url`. `MOVE_TASK` receives `ORCHESTRATOR_TASK_ID`,
`ORCHESTRATOR_STATE`, and `ORCHESTRATOR_STATE_VALUE`. `COMMENT_TASK` receives
`ORCHESTRATOR_TASK_ID` and `ORCHESTRATOR_COMMENT_TEXT`.

## Run

Validate configuration from inside the git repository:

```sh
python -m patbtawo --validate-config --print-config
```

Process a single task:

```sh
python -m patbtawo --once
```

`--once` is intentionally limited to one Ready task. It is useful for a first
board smoke test or debugging a single ticket.

Process until Ready is empty:

```sh
python -m patbtawo
```

In this mode, PATBTAWO continues to the next Ready task after a task reaches
Done, Failed, or Blocked. A stage failure should end that task attempt, not the
whole queue drain.

Use `--dry-run` or `ORCHESTRATOR_DRY_RUN=true` to skip provider moves/comments
while still exercising local worktree and stage execution.

## Stage Contract

Each stage agent command runs inside that stage's task worktree with these environment
variables:

- `ORCHESTRATOR_PROVIDER`
- `ORCHESTRATOR_STAGE`
- `ORCHESTRATOR_SUBAGENT_ID`
- `ORCHESTRATOR_SUBAGENT_ROLE`
- `ORCHESTRATOR_TASK_ID`
- `ORCHESTRATOR_TASK_NAME`
- `ORCHESTRATOR_TASK_CONTRACT_PATH`
- `ASANA_TASK_CONTRACT_PATH`
- `ORCHESTRATOR_REPORT_PATH`
- `ORCHESTRATOR_LOG_PATH`
- `ORCHESTRATOR_ARTIFACT_DIR`
- `ORCHESTRATOR_ATTEMPT`
- `ORCHESTRATOR_STAGE_WORKTREE_PATH`
- `ORCHESTRATOR_OBJECTIVE_VERIFIER`
- `ORCHESTRATOR_PROJECT_ID`
- `ORCHESTRATOR_PROJECT_GID`

The command must write a JSON object to `ORCHESTRATOR_REPORT_PATH`:

```json
{
  "task_id": "TASK-123",
  "status": "success",
  "summary": "Implemented the requested change and tests passed.",
  "changed_files": ["src/example.py", "tests/test_example.py"],
  "checks": [
    {"name": "unit tests", "status": "passed", "command": "python -m unittest"}
  ],
  "failures": [],
  "next_action": "Move to verification.",
  "artifact_paths": []
}
```

Accepted successful statuses include `success`, `passed`, `ok`, and `done`.
Accepted failure statuses include `failed`, `failure`, and `error`. Use
`blocked` or `external_blocker` for external blockers.

Set `retryable: true` or `transient: true` in a failed report, or exit with code
`75`, to request a retry when retry budget remains. Non-retryable failures move
the task to Failed immediately.

The verifier is treated as an objective gate. By default
`ORCHESTRATOR_OBJECTIVE_VERIFIER=true`, so the verifier does not run in the
builder's process or worktree. The orchestrator checkpoints the builder's file
changes, creates a fresh verifier worktree from that checkpoint, and starts a
fresh verifier subprocess/agent there. The verifier receives the task contract
and the code under test, not the builder's runtime context. A passing command
exit code alone is not enough: the verifier must produce a valid JSON report,
and the orchestrator fails the verifier if it modifies tracked files.

## Artifacts And Cleanup

Artifacts are written under:

```text
.orchestrator/artifacts/<task_id>/attempt-<n>-<timestamp>/
```

Each stage gets:

- `<stage>_report.json`
- `<stage>.log`
- `task_contract.json`

Worktrees are removed after terminal success, failure, or blocker unless
`ORCHESTRATOR_KEEP_WORKTREES=true`. Attempt branches are left in git so a builder
that committed useful work does not lose it during cleanup.

With objective verification enabled, each attempt can create up to three
worktrees: builder, verifier, and deployer. The verifier and deployer worktrees
are derived from the builder checkpoint, so every stage starts as a fresh
subprocess/agent in a clean checkout.

## Failure Modes

The orchestrator fails loudly before processing if required config is missing or
it is not launched from inside a git repository.

During a task attempt, missing/invalid reports are converted into structured
failure reports and the task is moved to Failed after retry budget is exhausted.
Provider HTTP 429 and 5xx responses are retried by the API client before
surfacing as orchestration errors.
