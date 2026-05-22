# PATBTAWO

Provider-Agnostic Task-Board-To-Agent Workflow Orchestrator.

PATBTAWO is an MVP for running coding agents from a project-management board. It
pulls one Ready task at a time, runs isolated stage commands in fresh git
worktrees, reports back to the board, and moves the task through explicit
lifecycle states.

It currently supports Asana, Trello, ClickUp, Jira, monday.com, and a custom
command provider. Most active testing has happened on Windows with ClickUp. The
code is intended to be cross-platform, but Mac and Linux have not been fully
tested yet.

## What It Does

- Selects the next task from `Ready`.
- Creates a fresh builder worktree for each attempt.
- Runs a builder command that must produce a JSON report.
- Checkpoints builder changes.
- Runs an objective verifier in a separate fresh worktree by default.
- Fails verification if the verifier edits tracked files.
- Optionally runs deploy/smoke from a fresh deployer worktree.
- Optionally updates shared project context in `context.md`.
- Optionally runs a planner stage that creates new Ready tasks from a high-level
  goal.
- Moves tasks to `Done`, `Failed`, or `Blocked`.
- Comments human-readable status plus structured metadata back to the board.
- Retries transient failures up to `ORCHESTRATOR_RETRY_LIMIT`.
- Fast-forward merges successful task changes back into the base branch before
  the next task starts.
- Cleans up temporary worktrees and `orchestrator/*` attempt branches.

PATBTAWO is deliberately conservative. It does not try to hide failures. If a
stage command is misconfigured, a report is missing, a model is unavailable, or
the base branch is dirty, the task should fail loudly with artifacts you can
inspect.

## Important Caveats

This is an MVP. Expect sharp edges around provider differences, local agent
configuration, OS-specific process behavior, and deployment scripts.

Keep your base branch, usually `main`, untouched while PATBTAWO is running. The
workflow fast-forward merges successful task work back into that branch. If you
edit tracked files on `main` during a run, merges can fail with messages like
`Your local changes would be overwritten by merge`.

If you want to work in parallel with the agents, use a separate branch, separate
clone, or separate worktree. Do not use the base checkout as your scratchpad
during an active run.

Monitor credits and rate limits with your agent provider. PATBTAWO cannot always
tell the difference between "the task is bad" and "the agent provider rejected
the request." If your provider runs out of credits, the workflow may continue
pulling Ready tasks and mark them Failed until you stop it.

Run `--once` while testing new boards, models, deploy commands, or provider
credentials. Letting a full Ready queue drain is for after the first task works
end to end.

## Prerequisites

- [Python 3.10+](https://www.python.org/downloads/)
- [Git](https://git-scm.com/downloads) with worktree support
- A board/list/project with lifecycle states for `Ready`, `Building`,
  `Verifying`, `Deploying`, `Done`, `Failed`, and `Blocked`
- Provider credentials:
  [Asana PAT](https://developers.asana.com/docs/personal-access-token),
  [Trello API key/token](https://support.atlassian.com/trello/docs/getting-started-with-trello-rest-api/),
  [ClickUp personal token](https://developer.clickup.com/docs/authentication),
  [Jira API token](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/),
  or [monday.com API token](https://developer.monday.com/api-reference/docs/authentication)
- An agent, script, or CI wrapper that can run non-interactively from the command
  line

For agents such as Codex CLI, Claude Code, opencode, BMAD, or LangGraph, install
and authenticate the tool first. Then test it in a throwaway git worktree before
assigning it to `PATBTAWO_BUILDER_RUN_COMMAND`.

## Install And Run

You can run PATBTAWO directly from a checkout:

```sh
python -m patbtawo --help
```

That is the safest path on Windows because it avoids generated console-command
launcher `.exe` files, which can trigger antivirus scans even when clean.

Optional local install:

```sh
python -m pip install .
```

The package intentionally does not install console scripts. Use
`python -m patbtawo` after installation too.

If you installed an older version that created `patbtawo.exe`,
`task-orchestrator.exe`, or `asana-orchestrator.exe`, remove those launchers:

```sh
python -m pip uninstall patbtawo
```

Validate configuration from inside the repository the agents should edit:

```sh
python -m patbtawo --validate-config --print-config
```

Process one task:

```sh
python -m patbtawo --once
```

Process tasks until Ready is empty:

```sh
python -m patbtawo
```

Clear stale/interrupted local attempts:

```sh
python -m patbtawo --reset-attempts
```

Preview reset cleanup first:

```sh
python -m patbtawo --reset-attempts --dry-run
```

## Core Configuration

Start from [.env.example](.env.example), then fill in your provider and stage
commands.

Minimum common settings:

```env
ORCHESTRATOR_PROVIDER=clickup
ORCHESTRATOR_STATE_READY_ID=Ready
ORCHESTRATOR_STATE_BUILDING_ID=Building
ORCHESTRATOR_STATE_VERIFYING_ID=Verifying
ORCHESTRATOR_STATE_DEPLOYING_ID=Deploying
ORCHESTRATOR_STATE_DONE_ID=Done
ORCHESTRATOR_STATE_FAILED_ID=Failed
ORCHESTRATOR_STATE_BLOCKED_ID=Blocked

ORCHESTRATOR_BASE_BRANCH=main
ORCHESTRATOR_WORKTREE_ROOT=../.orchestrator-worktrees
ORCHESTRATOR_ARTIFACT_ROOT=.orchestrator/artifacts
ORCHESTRATOR_RETRY_LIMIT=0
ORCHESTRATOR_OBJECTIVE_VERIFIER=true

ORCHESTRATOR_BUILDER_AGENT_COMMAND=python -m patbtawo.builder
ORCHESTRATOR_VERIFIER_AGENT_COMMAND=python -m patbtawo.verifier
PATBTAWO_BUILDER_RUN_COMMAND=
PATBTAWO_VERIFIER_RUN_COMMAND=
```

Useful optional settings:

```env
ORCHESTRATOR_MODEL=gpt-5.4-mini
ORCHESTRATOR_STAGE_TIMEOUT_SECONDS=3600
ORCHESTRATOR_KEEP_WORKTREES=false
ORCHESTRATOR_DRY_RUN=false
ORCHESTRATOR_CONTEXT_PATH=context.md
ORCHESTRATOR_CONTEXT_UPDATER_AGENT_COMMAND=python -m patbtawo.context_updater
PATBTAWO_CONTEXT_UPDATE_COMMAND=
```

PATBTAWO expands `${VAR}`, `$VAR`, and `%VAR%` inside stage commands before
launching the platform shell. Prefer `${VAR}` in shared examples because it is
the easiest to keep consistent across Windows, Mac, and Linux.

The builder receives `ORCHESTRATOR_TASK_CONTRACT_PATH` and
`ORCHESTRATOR_CONTEXT_PATH`. Your builder command should explicitly tell the
agent to read both files.

## Provider Notes

For ClickUp, set `CLICKUP_LIST_ID` to the list ID and optionally set
`CLICKUP_VIEW_ID` to a saved view if you need the workflow to follow a manual
view order. A default board/list URL ending in `/li/<number>` usually gives you
the list ID, not a saved view ID.

For ClickUp task ordering, use:

```env
CLICKUP_TASK_ORDER=api
CLICKUP_TASK_REVERSE=true
```

If visible order still does not match, create a saved view in ClickUp, manually
sort that view, and set `CLICKUP_VIEW_ID` to the view ID.

For Jira, set `JIRA_READY_JQL` if your Ready queue needs custom filtering or
ordering. If no `ORDER BY` is present, PATBTAWO appends `ORDER BY Rank ASC`.

For unsupported systems, use the command provider:

```env
ORCHESTRATOR_PROVIDER=command
ORCHESTRATOR_COMMAND_NEXT_TASK=./provider-next
ORCHESTRATOR_COMMAND_GET_TASK=./provider-get
ORCHESTRATOR_COMMAND_MOVE_TASK=./provider-move
ORCHESTRATOR_COMMAND_COMMENT_TASK=./provider-comment
ORCHESTRATOR_COMMAND_CREATE_TASK=./provider-create
```

`ORCHESTRATOR_COMMAND_CREATE_TASK` is only required if you use the planner stage.

## Stage Commands

The packaged adapters write the required PATBTAWO JSON reports. The
repo-specific work goes in `PATBTAWO_*_RUN_COMMAND`.

Builder example:

```env
PATBTAWO_BUILDER_RUN_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read the task contract at ${ORCHESTRATOR_TASK_CONTRACT_PATH} and the shared context at ${ORCHESTRATOR_CONTEXT_PATH}. Implement the requested task in this worktree. Keep edits scoped, run relevant checks, and do not commit."
```

Verifier example:

```env
PATBTAWO_VERIFIER_RUN_COMMAND=python -m unittest discover -s tests
```

Context updater example:

```env
PATBTAWO_CONTEXT_UPDATE_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read ${ORCHESTRATOR_TASK_SUMMARY_PATH} and refresh ${ORCHESTRATOR_CONTEXT_PATH} with a concise durable summary of the task that just completed."
```

Planner example:

```env
ORCHESTRATOR_PLANNER_AGENT_COMMAND=python -m patbtawo.planner
ORCHESTRATOR_PLANNER_TAG=plan
PATBTAWO_PLANNER_RUN_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read ${ORCHESTRATOR_TASK_CONTRACT_PATH} and ${ORCHESTRATOR_CONTEXT_PATH}. Break the goal into actionable, independently deployable tasks. Write JSON to ${ORCHESTRATOR_TASK_PLAN_PATH} with a tasks array. Include tags such as deploy when requested."
```

Planner output must look like:

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

PATBTAWO validates the plan, creates the tasks in Ready, then marks the planning
task Done with created task IDs/links in the comment.

## Local And Free Models

You can reduce cost by using local models through tools such as
[Ollama](https://ollama.com/) and an agent runner that supports Ollama-backed
models.

Example local setup:

```sh
ollama pull qwen2.5-coder:7b
ollama list
```

Then configure your model and agent command:

```env
ORCHESTRATOR_MODEL=qwen2.5-coder:7b
PATBTAWO_BUILDER_RUN_COMMAND=opencode run -m "${ORCHESTRATOR_MODEL}" --dangerously-skip-permissions "Read the task contract at ${ORCHESTRATOR_TASK_CONTRACT_PATH} and the shared context at ${ORCHESTRATOR_CONTEXT_PATH}. Implement the requested task in this worktree. Keep edits scoped, run relevant checks, and do not commit."
```

Some runners require a provider prefix, such as `ollama/qwen2.5-coder:7b`, while
others want the raw Ollama model name. Test the exact command manually before
letting PATBTAWO drain a board.

Tips for using local models:

- Start with small, explicit tasks. Local models struggle more with vague,
  multi-system tickets.
- Keep `ORCHESTRATOR_RETRY_LIMIT=0` while testing.
- Use deterministic verifier scripts where possible.
- Prefer a fast local model for planning or small edits, and reserve paid
  frontier models for complex builder work.
- Make sure the model is actually installed locally before running the workflow.
  A missing model will fail every task that reaches the builder.

## Getting Better Output

Good tasks make a huge difference. Each task should include:

- the concrete outcome expected
- relevant files, routes, commands, or APIs if known
- acceptance criteria
- whether deploy is required
- what not to change
- links to Figma/docs/issues when relevant

Keep shared project rules in `context.md`. PATBTAWO exposes that path to every
stage as `ORCHESTRATOR_CONTEXT_PATH`, so builders can get current project memory
without relying on a persistent model session.

Use the planner stage for high-level goals. Give the planning task the `plan`
tag, describe the goal, and ask it to create independently deployable tasks. Add
`deploy` to the requested tags when the generated tasks should deploy after
verification.

Keep verifier behavior objective. The verifier should inspect and test the
builder checkpoint, write findings, and avoid editing tracked files. If it edits
tracked files, PATBTAWO fails the verification stage.

## Deployment

Deployment is opt-in:

```env
ORCHESTRATOR_DEPLOY_ENABLED=true
ORCHESTRATOR_DEPLOY_POLICY=tagged
ORCHESTRATOR_DEPLOY_TAG=deploy
ORCHESTRATOR_DEPLOYER_AGENT_COMMAND=python -m patbtawo.deployer
PATBTAWO_DEPLOY_RUN_COMMAND=./scripts/deploy-prod
ORCHESTRATOR_SMOKE_COMMAND=python -m patbtawo.smoke
ORCHESTRATOR_SMOKE_URL=https://example.com
```

Deployment policies:

- `none`: never deploy
- `all`: deploy every successful task
- `tagged`: deploy only tasks with `ORCHESTRATOR_DEPLOY_TAG`

Task text can opt out with phrases such as `skip deploy`, `no deploy`,
`deploy: false`, or `smoke: false`.

PATBTAWO does not infer production. You must provide deploy commands, hostnames,
paths, SSH keys, smoke URLs, and any secrets your deploy scripts need.

## Common Pitfalls

- Dirty base branch: commit, stash, or discard local tracked changes before a
  run.
- Wrong ClickUp ordering: use a saved view and `CLICKUP_VIEW_ID` if manual order
  matters.
- Missing stage reports: every stage command must write JSON to
  `ORCHESTRATOR_REPORT_PATH`; using packaged adapters helps.
- Missing local model: run `ollama list` or the equivalent for your agent runner
  first.
- Provider credits/rate limits: stop the run if your agent provider starts
  rejecting requests.
- Antivirus/file locks on Windows: stale worktree folders may need editors,
  terminals, or scanners closed before cleanup can remove them.
- Deploy disabled: successful code changes will not reach production unless
  deploy is configured and enabled.
- Subdomain confusion: DNS points hostnames to servers, not URL paths. Configure
  the web server or deploy path separately.
- Over-broad tasks: one large vague task can burn a lot of agent time and still
  fail verification.

## Setup Prompt

Paste this into your coding agent from inside the repository you want PATBTAWO to
operate on:

```text
Set up PATBTAWO for this repository. Inspect the existing build, test, and
deploy commands. Prepare a .env from .env.example for my chosen provider, leave
secrets blank, and set PATBTAWO_BUILDER_RUN_COMMAND,
PATBTAWO_VERIFIER_RUN_COMMAND, PATBTAWO_PLANNER_RUN_COMMAND,
PATBTAWO_CONTEXT_UPDATE_COMMAND, and optional PATBTAWO_DEPLOY_RUN_COMMAND to the
right commands for this repo. Make sure context.md is present. Use --once for
the first run. Do not commit secrets. Run the tests or closest available sanity
checks and summarize the final commands I should use.
```

Full provider setup and implementation notes live in
[docs/task-orchestrator.md](docs/task-orchestrator.md).

## Naming

PATBTAWO is intentionally descriptive and admittedly not final. If you fork this
project and have a better name, open an issue or include the proposal in a pull
request with the suggested rename, rationale, and any docs/package updates needed
to make the change reviewable.
