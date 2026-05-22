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
  [create task](https://developers.asana.com/reference/createtask),
  [add task to section](https://developers.asana.com/reference/addtaskforsection),
  and [create story/comment](https://developers.asana.com/reference/createstoryfortask)
- [Trello cards API](https://developer.atlassian.com/cloud/trello/rest/api-group-cards/)
  and [lists API](https://developer.atlassian.com/cloud/trello/rest/api-group-lists/)
- [ClickUp get tasks](https://developer.clickup.com/reference/gettasks),
  [create task](https://developer.clickup.com/reference/createtask),
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
3. If the task is a planning task and a planner is configured, run the planner
   stage, validate its task plan, create each planned item in Ready, then move
   the original planning task to Done.
4. Create a fresh builder worktree from `ORCHESTRATOR_BASE_BRANCH`.
5. Move the task to Building.
6. Run the builder agent command in the builder worktree.
7. Require the builder JSON report.
8. Checkpoint the builder's file changes.
9. Move passing builds to Verifying.
10. Create a fresh verifier worktree from the checkpoint.
11. Run the verifier agent command in that isolated verifier worktree.
12. Require the verifier JSON report and capture a verifier log.
13. Fail verification if the verifier modifies tracked files.
14. If deploy is enabled, create a fresh deployer worktree from the verified
    checkpoint, move the task to Deploying, and run deploy/smoke.
15. If a context updater is configured, create a fresh context worktree from
    the verified checkpoint, refresh `context.md`, and keep the update in the
    same merge-back path as the task changes.
16. Fast-forward merge successful task changes back into
    `ORCHESTRATOR_BASE_BRANCH`.
17. Delete the temporary attempt branches after their worktrees are removed.
18. Move successful tasks to Done.
19. Move failed tasks to Failed.
20. Move externally blocked tasks to Blocked.
21. Comment concise status back to the provider.
22. Retry retryable/transient stage failures with a fresh worktree until the
    retry limit is exhausted.
23. Continue until the Ready queue is empty.

The Ready queue is consumed top-down. PATBTAWO uses each provider's native
list, view, position, or rank order where it is available, such as Trello `pos`,
ClickUp API/view order, and Jira `Rank ASC`. Providers that do not expose a
separate position field are consumed in the order their board/list endpoint
returns.

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
ORCHESTRATOR_CONTEXT_PATH=context.md
ORCHESTRATOR_BUILDER_AGENT_COMMAND=python -m patbtawo.builder
ORCHESTRATOR_VERIFIER_AGENT_COMMAND=python -m patbtawo.verifier
ORCHESTRATOR_PLANNER_AGENT_COMMAND=python -m patbtawo.planner
ORCHESTRATOR_PLANNER_TAG=plan
ORCHESTRATOR_CONTEXT_UPDATER_AGENT_COMMAND=python -m patbtawo.context_updater
ORCHESTRATOR_DEPLOYER_AGENT_COMMAND=python -m patbtawo.deployer
ORCHESTRATOR_SMOKE_COMMAND=python -m patbtawo.smoke
ORCHESTRATOR_MODEL=gpt-5.4-mini
PATBTAWO_BUILDER_RUN_COMMAND=
PATBTAWO_VERIFIER_RUN_COMMAND=
PATBTAWO_PLANNER_RUN_COMMAND=
PATBTAWO_CONTEXT_UPDATE_COMMAND=
PATBTAWO_DEPLOY_RUN_COMMAND=
PATBTAWO_SMOKE_RUN_COMMAND=
```

Example builder run command:

```sh
PATBTAWO_BUILDER_RUN_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read the task contract at ${ORCHESTRATOR_TASK_CONTRACT_PATH} and the shared context at ${ORCHESTRATOR_CONTEXT_PATH}. Implement the requested change in this worktree. Keep edits scoped, run relevant checks, and do not commit."
```

Example context update command:

```sh
PATBTAWO_CONTEXT_UPDATE_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read ${ORCHESTRATOR_TASK_SUMMARY_PATH} and refresh ${ORCHESTRATOR_CONTEXT_PATH} with a concise durable summary of the task that just completed."
```

Example planner command:

```sh
PATBTAWO_PLANNER_RUN_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read ${ORCHESTRATOR_TASK_CONTRACT_PATH} and ${ORCHESTRATOR_CONTEXT_PATH}. Break the goal into actionable, independently deployable tasks. Write JSON to ${ORCHESTRATOR_TASK_PLAN_PATH} with a tasks array. Include tags such as deploy when requested."
```

The planner output can be either the JSON plan file at
`ORCHESTRATOR_TASK_PLAN_PATH` or a `tasks`/`task_plan` field in the planner
report. The preferred file format is:

```json
{
  "tasks": [
    {
      "title": "Add production health route",
      "description": "Implement GET /health and cover it with tests.",
      "acceptance_criteria": ["GET /health returns 200 JSON"],
      "tags": ["deploy"]
    }
  ]
}
```

Planning tasks are detected when `ORCHESTRATOR_PLANNER_AGENT_COMMAND` is set and
the task has the tag named by `ORCHESTRATOR_PLANNER_TAG` or its text clearly
asks to break/create/turn work into tasks, tickets, chunks, or work items.
PATBTAWO validates the plan, creates every item in the provider's Ready state,
then marks the original planning task Done with created task IDs/links in the
handoff comment. Providers with native tag support receive the planned tags;
other providers preserve planned tags in the task description or comments.

For opencode, `--dangerously-skip-permissions` auto-approves prompts (yolo
mode). Place it before the prompt string.

PATBTAWO expands `${VAR}`, `$VAR`, and `%VAR%` in stage command strings before
launching the platform shell. Prefer `${VAR}` in shared examples because the
same `.env` command then works on Windows, macOS, and Linux for paths such as
`${ORCHESTRATOR_TASK_CONTRACT_PATH}`.

Provider-specific aliases are supported. For example, Asana can use
`ASANA_SECTION_READY_GID`, Trello can use `TRELLO_LIST_READY_ID`, ClickUp can use
`CLICKUP_STATUS_READY`, Jira can use `JIRA_TRANSITION_BUILDING_ID`, and monday.com
can use `MONDAY_STATUS_BUILDING`.

Optional local behavior:

- `ORCHESTRATOR_RETRY_LIMIT`, default `0`
- `ORCHESTRATOR_DRY_RUN`, default `false`
- `ORCHESTRATOR_BASE_BRANCH`, default current git branch or `HEAD`; set this to
  a named branch such as `main` if you want successful attempts fast-forward
  merged back before the next task starts
- `ORCHESTRATOR_WORKTREE_ROOT`, default `../.<repo>-orchestrator-worktrees`
- `ORCHESTRATOR_ARTIFACT_ROOT`, default `.orchestrator/artifacts`
- `ORCHESTRATOR_STAGE_TIMEOUT_SECONDS`
- `ORCHESTRATOR_KEEP_WORKTREES`, default `false`
- `ORCHESTRATOR_OBJECTIVE_VERIFIER`, default `true`
- `ORCHESTRATOR_CONTEXT_PATH`
- `ORCHESTRATOR_CONTEXT_UPDATER_AGENT_COMMAND`
- `ORCHESTRATOR_PLANNER_AGENT_COMMAND`
- `ORCHESTRATOR_PLANNER_TAG`, default `plan`
- `ORCHESTRATOR_DEPLOY_ENABLED`
- `ORCHESTRATOR_DEPLOY_POLICY`, one of `all`, `tagged`, or `none`
- `ORCHESTRATOR_DEPLOY_TAG`, required when `ORCHESTRATOR_DEPLOY_POLICY=tagged`
- `ORCHESTRATOR_BUILDER_AGENT_COMMAND`
- `ORCHESTRATOR_VERIFIER_AGENT_COMMAND`
- `ORCHESTRATOR_DEPLOYER_AGENT_COMMAND`
- `ORCHESTRATOR_BUILDER_COMMAND`, backward-compatible alias
- `ORCHESTRATOR_VERIFIER_COMMAND`, backward-compatible alias
- `PATBTAWO_CONTEXT_UPDATE_COMMAND`
- `PATBTAWO_PLANNER_RUN_COMMAND`
- `ORCHESTRATOR_DEPLOY_COMMAND`
- `ORCHESTRATOR_SMOKE_COMMAND`
- `ORCHESTRATOR_MODEL`
- `PATBTAWO_BUILDER_RUN_COMMAND`
- `PATBTAWO_VERIFIER_RUN_COMMAND`
- `PATBTAWO_DEPLOY_RUN_COMMAND`
- `PATBTAWO_SMOKE_RUN_COMMAND`

The `ORCHESTRATOR_*_AGENT_COMMAND` values are outer stage adapters. The packaged
`python -m patbtawo.builder`, `python -m patbtawo.verifier`,
`python -m patbtawo.planner`, `python -m patbtawo.context_updater`,
`python -m patbtawo.deployer`, and `python -m patbtawo.smoke` adapters always
write the required JSON reports.
Configure the underlying repo-specific work with the `PATBTAWO_*_RUN_COMMAND`
variables.

Deploy runs only when enabled or when a deploy/smoke command is set. If both
deploy and smoke commands are set, they run as one deployer stage joined with
`&&`; the packaged smoke adapter merges its smoke check into the deployer report.

PATBTAWO does not infer where production is. Configure a deployer command and
pass the target details as environment variables:

```sh
ORCHESTRATOR_DEPLOY_ENABLED=true
ORCHESTRATOR_DEPLOYER_AGENT_COMMAND=python -m patbtawo.deployer
ORCHESTRATOR_SMOKE_COMMAND=python -m patbtawo.smoke
PATBTAWO_DEPLOY_RUN_COMMAND=./scripts/deploy-prod
PATBTAWO_SMOKE_RUN_COMMAND=
ORCHESTRATOR_DEPLOY_HOST=prod.example.com
ORCHESTRATOR_DEPLOY_USER=deploy
ORCHESTRATOR_DEPLOY_PATH=/srv/app
ORCHESTRATOR_DEPLOY_SSH_KEY_PATH=~/.ssh/patbtawo_deploy
ORCHESTRATOR_SMOKE_URL=https://prod.example.com
```

`ORCHESTRATOR_DEPLOY_*` values, except PATBTAWO's own deploy command/enabled
settings, are passed to builder/verifier/deployer stage commands from `.env`.
For arbitrary variables, either list existing names in
`ORCHESTRATOR_STAGE_ENV_KEYS`:

```sh
ORCHESTRATOR_STAGE_ENV_KEYS=PRODUCTION_HOST,DEPLOY_REGION
PRODUCTION_HOST=prod.example.com
DEPLOY_REGION=us-east-1
```

or expose them with `ORCHESTRATOR_STAGE_ENV_`:

```sh
ORCHESTRATOR_STAGE_ENV_PRODUCTION_HOST=prod.example.com
```

which passes `PRODUCTION_HOST=prod.example.com` to stage commands.

The `PATBTAWO_*_RUN_COMMAND` variables are plain shell commands. They may launch
opencode, Claude Code, BMAD, LangGraph, local scripts, CI wrappers, or any other
executable workflow. The orchestrator starts a fresh subprocess for each stage
and passes a fresh `ORCHESTRATOR_SUBAGENT_ID` plus the stage role through the
environment.

Keep command bodies as portable as the tool allows: prefer `python -m ...` over
shell-specific wrappers, avoid PowerShell/Bash-only syntax in shared examples,
and put platform-specific setup behind scripts when a deploy target truly needs
it.

Do not point the backward-compatible aliases at placeholder scripts such as
`python scripts/builder.py` unless those files exist in the target repository.
The packaged adapters avoid that failure mode by living inside the installed
PATBTAWO package.

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
# Optional, recommended when you need exact visible order from a ClickUp view.
CLICKUP_VIEW_ID=
# Optional: api preserves ClickUp's returned order; orderindex sorts explicitly.
CLICKUP_TASK_ORDER=api
# Optional: passed to ClickUp's List tasks endpoint. The default matches ClickUp
# top-down order for status-grouped List data in the demo project.
CLICKUP_TASK_REVERSE=true
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
filtering tasks or moving tasks. PATBTAWO preserves ClickUp's returned task
order by default. If the raw List API order does not match the order you see in
ClickUp, set `CLICKUP_VIEW_ID` to the List or Board view you want PATBTAWO to
consume from, or adjust `CLICKUP_TASK_REVERSE`.

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
query. PATBTAWO appends `ORDER BY Rank ASC` when the Ready JQL does not already
provide an `ORDER BY` clause, so Jira boards are consumed in rank order by
default. The adapter tries Jira's enhanced `/search/jql` endpoint and falls back
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
ORCHESTRATOR_COMMAND_CREATE_TASK=./provider-create
```

`NEXT_TASK` and `GET_TASK` commands must print a JSON object with at least an
`id`, `gid`, or `key`; optional fields include `name`, `title`, `summary`,
`notes`, `description`, and `url`. `MOVE_TASK` receives `ORCHESTRATOR_TASK_ID`,
`ORCHESTRATOR_STATE`, and `ORCHESTRATOR_STATE_VALUE`. `COMMENT_TASK` receives
`ORCHESTRATOR_TASK_ID` and `ORCHESTRATOR_COMMENT_TEXT`. The command provider's
`NEXT_TASK` hook is responsible for returning the top Ready item from its
underlying system.
`CREATE_TASK` is optional unless you enable planner tasks. It receives the task
payload in `ORCHESTRATOR_TASK_CREATE_JSON` and should print the created task as
JSON with at least an `id`, `gid`, or `key`.

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
- `ORCHESTRATOR_TASK_PLAN_PATH`
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
`ORCHESTRATOR_KEEP_WORKTREES=true`. Successful attempts are fast-forward merged
back into `ORCHESTRATOR_BASE_BRANCH` before the task is marked Done, and
temporary attempt branches are deleted during cleanup.

If local attempt state gets interrupted or stale, reset it from inside the repo:

```sh
python -m patbtawo --reset-attempts
```

This clears `active_attempt`, removes `attempt-*` artifact directories, removes
attempt worktrees under `ORCHESTRATOR_WORKTREE_ROOT`, prunes git worktree
metadata, and deletes temporary `orchestrator/*-attempt-*` branches. Add
`--dry-run` to print the cleanup actions without deleting anything.

Deployment scope is controlled by `ORCHESTRATOR_DEPLOY_POLICY`:
`all` deploys every successful task, `tagged` deploys only tasks carrying the
tag named by `ORCHESTRATOR_DEPLOY_TAG`, and `none` disables deploy/smoke
globally. Task or stage text can still opt out with phrases or fields such as
`skip deploy`, `no deploy`, `deploy: false`, `smoke: false`,
`skip_deploy: true`, or `deploy_required: false`.

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
