# Tokenomics Worker worked patterns

Copy-paste dispatch templates for the reference pipeline in `SKILL.md`. Load one
section with `view_file` when you need it; do not read the whole file into an
orchestrator turn. `{plugin}` is the plugin root, normally
`~/.gemini/config/plugins/tokenomics_worker`.

If you installed conversation-scoped pins with `set_models`, substitute the
TypeNames it returned in `use_these_type_names` (for example
`tw_worker_gemini_3_8_flash_medium`) for `tw_worker` / `tw_scout` /
`tw_redteam` below.

## Pattern 1: dispatch envelope for `tw_worker`

The static contract (receipts, test integrity, 2-turn circuit breaker, file
boundary, return header) already lives in `agents/tw_worker/agent.json` and is
cached as part of the system prompt. The per-task prompt only carries what
changes between tasks:

```markdown
## TASK
- TASK_ID: {task_id}
- ROLE: tw_worker, implement and verify

## ORIGINAL_REQUEST
{one_sentence_user_goal}
Acceptance: WHEN {condition} THEN {observable_result}.

## ASSIGNED_FILES_EXCLUSIVE
- {path/to/module.py}
- {path/to/module_test.py}
READ_ONLY: {path/to/interface.proto}
Anything else you write is reverted by the orchestrator.

## VERIFIED SYMBOLS (from the scout report)
- `{Class.method}(ctx, token) -> Status` at `{path/to/file.py}:{line}`
- Keep the null check at `{path/to/file.py}:{line}`; added by {change} for {bug}.

## SHARED STATE
- Progress notes: {scratch_dir}/PROGRESS.md
- Failed approaches: {scratch_dir}/DEAD_ENDS.md. Read it first; append one line
  per approach you abandon.

## VERIFY
{test_command}
If you changed .py files:
python3 {plugin}/scripts/verify_and_record.py \
    --mode=lint-readability --workspace_dir={workspace_dir} \
    --allowed-files={path/to/module.py},{path/to/module_test.py}

## RETURN
STATUS / MODIFIED_FILES / VERIFY_RECEIPT header first, then receipts.
```

After the worker returns, the orchestrator reads only the three header lines and
the receipts. `DONE_WITH_CONCERNS` and `NEEDS_CONTEXT` are cheap to handle in
one more dispatch; `BLOCKED` means read `DEAD_ENDS.md` before deciding anything.

## Pattern 2: reconnaissance prompt for `tw_scout`

```markdown
You are tw_scout. Do not edit files. Task under investigation: {task_summary}.

1. For each function the plan touches in {path/to/file}, find why the existing
   conditionals, retries, or timeouts were added (`git log -L`, `git blame`),
   then read the review discussion on the top one to three changes. Report the
   rationale and any linked issue.
2. Verify every symbol, signature, proto field, and build target the plan names.
   Return a table: Symbol | File:Line | Exact signature | Visibility. Mark
   anything that does not exist at HEAD as MISSING; do not guess a replacement.
3. For every claim of the form "no caller does X" or "all implementations do Y",
   run the counter-example search and record it as
   [SEARCH_RECEIPT: query="..." matches=N].
4. Close with two sentences on what flag, build rule, or upstream wrapper the
   service owner would check first.
```

Fan several scouts out at once when the plan spans packages; each is read-only
and shares the workspace safely.

## Pattern 3: single-turn pre-review with `tw_redteam`

Run this after the implementer returns `DONE` and before the orchestrator reads
the diff, so the orchestrator's first read happens after the mechanical defects
are gone.

```markdown
You are tw_redteam. Review and fix in one turn; never message other subagents.

ORIGINAL_REQUEST: {one_sentence_user_goal}
PLAN: {scratch_dir}/PLAN.md
ASSIGNED_FILES_EXCLUSIVE: {same list the implementer had}
TEST TARGET: {test_command}

Check in order: stubs and TODOs; None/empty/error paths the plan required;
swallowed exceptions; edits outside the list; whether each new test fails with
the production change stashed and passes with it restored.
Fix what you find once, re-run the test target, run --mode=lint-readability on
any .py you touched, and return the STATUS header followed by every finding
marked FIXED or LEFT.
```

If it returns `BLOCKED`, the orchestrator decides between another `tw_worker`
dispatch with the finding pasted in, or escalating to the user. It does not loop
`tw_redteam` a second time on the same diff.

## Pattern 4: switching subagent models for one session

Cheaper scouts, stronger workers, with a fallback, for this conversation only.
Refresh the catalog (`python3 {plugin}/scripts/model_catalog.py`), then one
MCP call on `tokenomics-worker-tools` (labels from `/model` work as well as ids):

```json
{"tool": "set_models", "arguments": {
  "conversation_id": "{current_conversation_id}",
  "scout": "Gemini 3.8 Flash (Low)",
  "worker": "Gemini 3.8 Flash (High)",
  "subagent_fallback": "Gemini 3.1 Pro",
  "dry_run": true
}}
```

Read the pairing verdict and `routing` in the dry run, then repeat without
`dry_run` and use the returned `use_these_type_names` (and
`fallback_type_names` when a dispatch fails on quota or capacity). Do this once at the start of the session:
installing agents invalidates the orchestrator's prompt cache. Add
`"persist": true` only when the user wants the change as the plugin default.

## Short language recipes

Paste the relevant block into the `VERIFY` section of Pattern 1.

**Python.** `from __future__ import annotations` in any file using `|` unions.
No `pylint: disable`, no dunders beyond `__init__`/`__str__`/`__repr__`, no
`getattr`/`setattr`/`hasattr`, no nested `def`/`class`.
`--mode=lint-readability` checks these four; it does not replace the real
linters.

**Go.** After any import change run `go mod tidy` and `gofmt -l .`; if the
repo uses Bazel, regenerate BUILD files (for example with Gazelle).

**C++.** Check header and build-file dependency drift with the repo's own
tooling. Keep long compile loops inside `tw_worker`; never inline them in an
orchestrator turn.
