---
name: tokenomics-worker
version: 1.0.0
description: >-
  OPT-IN ONLY via the /tokenomics-worker slash command, or when running as the
  tw_orchestrator main agent. Runs a configurable orchestrator model that
  delegates token-heavy execution to pinned subagents (tw_scout, tw_worker,
  tw_redteam) on a configurable subagent model, so the expensive budget is
  spent on judgment rather than bulk volume. Accepts orchestrator=<model_id>
  and subagent=<model_id> arguments. Do NOT auto-activate on general coding
  tasks.
metadata:
  icon: 🪙
---

# Tokenomics Worker

> [!IMPORTANT] **Strict opt-in.** Apply this protocol **only** when the user
> explicitly triggers `/tokenomics-worker` (or asks for the `tokenomics-worker`
> workflow), or when you are running as the `tw_orchestrator` main agent. Never
> apply it automatically to general tasks.

A conductor/worker pattern with both halves configurable. The **orchestrator**
keeps the wheel and spends its tokens on decomposition, arbitration, and
judgment. Pinned **subagents** absorb the reading, typing, and waiting.

Everything is chosen inside the IDE; there is no script to run:

-   **Orchestrator model:** whatever the user picks in the IDE's model picker.
    `tw_orchestrator` is deliberately not pinned.
-   **Subagent models:** set from chat through the `tokenomics-worker-tools`
    MCP server (`set_models`, `learn_model`, `get_model_config`,
    `list_models`). Plugin defaults live in `tokenomics.config.json` (falling
    back to the shipped `tokenomics.default.json`) and are rendered into the
    `tw_*` agents.

Paths below use `{plugin}` for the plugin root (the folder holding this
plugin's `plugin.json`).

## Step 0: confirm the configuration before delegating

1.  **Call `get_model_config`** with your conversation id. It returns the
    plugin defaults (per role: model id, label, whether it is pinned, enum,
    billing), the pairing verdict with its findings, the orchestrator cost
    rules, and any session pins for this conversation. Session pins win over
    the defaults.
2.  **Check your own model.** Call `get_model_config` with your conversation
    id: `live_model` is the enum that actually served your last turn. If it is
    not the orchestrator the verdict was computed for, say so in one line and
    re-run `set_models` with `orchestrator=<the id the user picked>` so the cost
    math matches. A live conversation's model cannot be swapped by you; the
    user changes it in the picker.
3.  **Act on the verdict.**
    -   `offload`: proceed.
    -   `partial`: proceed, but obey each `warn` line (for example, if workers
        are token-metered, keep their reads bounded and stop treating their
        output as free).
    -   `counterproductive`: tell the user what the `risk` line says and
        recommend a fix before fanning out. Do not quietly run an inverted
        pairing.
    -   A role reported as **not pinned** (a fresh install: no model enum
        learned yet) runs on whatever model the IDE picks, usually yours. Walk
        the user through `learn_model` (below) before delegating anything
        token-heavy.

### Arguments: choosing models from the slash command

```text
/tokenomics-worker [orchestrator=<id>] [subagent=<id>] [scout=<id>] [worker=<id>] [redteam=<id>] [persist]
```

Map the arguments straight onto one `set_models` MCP call. Always pass
`conversation_id` (your current conversation id).

-   **No model arguments:** use the active configuration as is. No call needed.
-   **Model arguments, no `persist`:** call `set_models` with
    `persist: false`. It installs conversation-scoped pins that load
    immediately, without touching the config, and returns
    `use_these_type_names`. These have **different TypeNames** (for example
    `tw_worker_gemini_3_8_flash_medium`) so the plugin's own `tw_worker` cannot
    shadow them. Use exactly those TypeNames for the rest of the conversation;
    they are also recorded in `<app data dir>/brain/<id>/tokenomics.session.json`.
-   **`persist`:** call `set_models` with `persist: true`. It also saves the
    config and re-renders the plugin's `tw_*` agents, so later conversations get
    the new defaults after an IDE restart.
-   **`orchestrator=<id>`** never changes your model; the picker does. It tells
    the economics check which billing to assume. Say so instead of pretending.
-   Use `dry_run: true` to preview the verdict before installing anything.
-   **Unknown id?** `list_models` with a `filter`. If the model is in the IDE's
    picker but not in the registry, it needs an enum, which is never guessed:
    ask the user to start a new conversation on that model, send one message,
    and give you that conversation id. Then call `learn_model` with the
    `model_id`, that `conversation_id`, and optionally `billing_like` (an id
    whose billing it shares; flagged as assumed). After that it pins like any
    other model.

Installing agents changes the tool and subagent definitions in your system
prompt, which invalidates the prompt cache. Do it once, at the start, not
mid-task.

## Why this exists

Models are often billed in **different units against different budgets**. A
token-metered model pays for every token it holds in context and writes.
A call-metered model pays one unit per turn regardless of size. When the
orchestrator is token-metered and the subagents are call-metered, every file a
scout reads on your behalf is a file you never pay to hold in context, and
every paragraph a worker writes is output you are not billed for. Delegation
moves tokens from a metered budget to an unmetered one.

That only holds for some pairings. `set_models` computes it for you: the
pairing verdict above says whether this configuration moves load off the
orchestrator's budget, merely isolates context, or inverts the economics.

Two traps are invisible in the tool signature:

1.  `invoke_subagent` defaults to `Model: 'inherit'`. An orchestrator that fans
    out without a pinned `TypeName` spawns children on **its own model** and
    bills them to its own budget.
2.  Tier names are coarse. `Model: 'flash'` resolves to whatever the server
    currently puts in that slot. If you need an exact model or thinking level,
    only an `agent.json` pin can express it. That is what this plugin's agents
    are.

## The three levers

| Lever | Resolves to | Use when |
| :---- | :---------- | :------- |
| `Model: 'inherit'` (default) | The orchestrator's own model | Never for fan-out. |
| `Model: 'flash'` / `'flash_lite'` / `'pro'` | Server-controlled tier slot | Cheap one-off lookups where the exact model does not matter. |
| Pinned agent (`tw_scout`, `tw_worker`, `tw_redteam`) | Exactly the configured model | Default for this workflow. Omit `Model`; the pin owns it. |

```json
{"Subagents": [{
  "TypeName": "tw_scout",
  "Role": "Auth Regression Hunter",
  "Prompt": "...",
  "Workspace": "inherit"
}]}
```

-   `tw_scout`: read-only tool allowlist that includes `run_command` for
    read-only CLIs (`git log`, `git blame`, `rg`), with a system prompt
    forbidding edits and mutating commands. Several can crawl one workspace at once.
-   `tw_worker`: full coding tooling plus a contract enforcing mechanical test
    receipts, test-assertion protection, a 2-turn circuit breaker, an
    `ASSIGNED_FILES_EXCLUSIVE` boundary, branch path translation, and a
    four-state `STATUS` header.
-   `tw_redteam`: one-turn, fresh-context reviewer-fixer run on an
    implementer's diff before you read it.

### How the pin works, and the five ways it breaks

```json
{
  "name": "tw_worker",
  "subagent": true,
  "main_agent": false,
  "disable_model_selection": true,
  "config": {
    "cascade_config": {"planner_config": {"plan_model": "MODEL_..."}}
  }
}
```

-   `disable_model_selection` missing: the request-time tier or the parent
    model wins and `plan_model` is ignored.
-   `disable_model_selection` present with no `plan_model`: pins the agent to
    the IDE's *default* model rather than erroring. Set both. (This is why the
    plugin ships unpinned agents until a model's enum has been learned.)
-   Model in the wrong field: `agent.json` is parsed strictly, so one
    misplaced key stops the whole agent loading. If a `TypeName` is suddenly
    "not found", suspect a bad field first.
-   A stray `agent.md` beside `agent.json` shadows it and drops the pin.
    `set_models` deletes one if it finds it.
-   Not every enum is servable. Smoke-test a new pin before building on it.

### Always verify the pin

Pins fail silently and expensively. Verify once per new model choice, after
the first subagent turn: call `get_model_config` with the **subagent's**
conversation id. Its `live_model` is read from the structured `model_enum`
field the IDE records for every turn, and must equal the enum shown for that
role. If it does not, stop fanning out and tell the user.

`tw_orchestrator` is not pinned, so verify your own conversation the same way
when the economics matter.

## Orchestrator doctrine

### Dynamic intent routing

Do not over-orchestrate trivial changes, and do not under-verify risky ones.

-   **Tier 1, surgical edit.** Single-file fix, typo, or localized assertion.
    One `tw_worker` in `Workspace: 'inherit'`, no formal plan, no separate
    review subagent.
-   **Tier 2, standard feature or bugfix.** Multi-file change with clear
    requirements. `tw_scout` fan-out, then your plan, then `tw_worker` in
    `Workspace: 'share'`, then `tw_redteam`, then your diff review, then a
    `tw_worker` shepherd.
-   **Tier 3, high-risk or ambiguous refactor.** Cross-package redesign or
    subtle concurrency/state changes. Use competing branches (below).

### Adversarial dual-branch mode

1.  Spawn two `tw_worker`s with `Workspace: 'branch'` and contrasting angles:
    *Alpha* (smallest diff, existing local patterns, no new abstractions) and
    *Beta* (clean interfaces, explicit error boundaries, edge-case tests).
2.  **Branch path trap:** inside a `branch` clone, search tools may return
    paths into the *parent* workspace. Tell branch workers explicitly to translate
    every such path to their own branch root before reading or editing.
    Without this they silently edit the user's base workspace.
3.  When both report green receipts, compare with `diff -u` or `view_file`
    (not a VCS diff across clones), synthesize one merge plan, and dispatch a
    single `tw_worker` in `Workspace: 'inherit'` to apply it.

### Protect orchestrator context: the 1% skill rule

Never `view_file` a domain `SKILL.md` into your own context. When a skill is
even 1% likely to apply, pass its path in the delegation prompt and have the
worker read and follow it in its isolated context.

### Never go idle, but never act on partial results

-   Fill a wait with work that does not depend on the workers' answers: draft
    the artifact they will populate, verify a fact needed either way, prepare
    the next prompts.
-   Do not edit code, synthesize conclusions, or report to the user until every
    outstanding worker has returned.
-   Do not poll. The IDE delivers a message when a worker finishes; a status
    check returns nothing new and permanently lengthens your history.
-   Delegate long build-and-fix loops. The worker sits through the compile; you
    take one resume when it reports.

### Split of responsibilities

Keep on the orchestrator: decomposition and deciding what is worth delegating;
arbitration between workers that disagree; decisions that are expensive to
reverse (API shape, data model, rollout); auditing test-file diffs for weakened
assertions; the final answer (never forward a worker's report verbatim).

Hand to subagents: bounded search with a verifiable answer; mechanical execution
of an approved plan; anything whose output is long but whose conclusion is
short.

### Worker prompt contract

Workers follow instructions well and infer intent poorly. Give every prompt:

1.  **Role and scope** with absolute paths.
2.  **Numbered objectives**, phrased so a wrong answer is detectable.
3.  **Context paths** (`ORIGINAL_REQUEST.md`, `DEAD_ENDS.md`, the approved
    plan) by path, not pasted.
4.  **Report contract** with grounding categories: `Verified` (cited
    `file:line` opened with `view_file`), `Assumption`, `Unverified` ("searched
    X in Y, found nothing"). Silence is not absence.
5.  **Boundary.** "Do not edit any files" for scouts; an explicit
    `ASSIGNED_FILES_EXCLUSIVE` list for implementers. Parallel implementers get
    disjoint lists. If `MODIFIED_FILES` names anything outside the list, revert
    it yourself (`git checkout -- <file>` or equivalent) before reviewing
    the rest. The manifest is a contract, not a sandbox.
6.  **Known context.** State what you already believe and invite correction.
7.  **Return header.** `tw_worker` and `tw_redteam` already carry it in their
    system prompts:

    ```text
    STATUS: DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED
    MODIFIED_FILES: <comma-separated paths, or none>
    VERIFY_RECEIPT: PASSED | FAILED | NOT_RUN
    ```

Copy-paste dispatch templates live in `references/worked_patterns.md` beside
this file. Load one section when you need it.

### Anti-cheating guardrails

-   **Zero verbal verification.** Accept only mechanical receipts: raw exit
    code, exact target, verbatim pass summary.
-   **Test integrity.** Forbid edits to `*_test.*` / `Test*.*` unless assigned;
    forbid `@Suppress`, `// NOLINT`, `# pylint: disable`. Audit test diffs
    yourself, or run the `detect_test_tampering` MCP tool.
-   **2-turn circuit breaker.** A worker looping on the same error after two
    attempts gets killed (`manage_subagents(Action='kill')`). Log the approach
    in `DEAD_ENDS.md` (`Attempt | Error Signature | Root Cause | Pivot Rule`)
    and re-scope with a fresh worker that is handed that file.

### Fan-out, workspaces, and context economy

-   Fan out 3 to 5 scouts on genuinely independent questions in one
    `invoke_subagent` call. Overlapping questions return duplicated findings
    that land in your context.
-   `Workspace: 'inherit'` for read-only scouts; `'share'` for implementers that
    must see each other's edits; `'branch'` for competing or speculative work.
    Never run two writers over the same files in `inherit` or `share`.
-   Continue an existing thread with `send_message` instead of spawning a fresh
    worker that must re-read everything.
-   Copy large subagent artifacts with `cp`, not `view_file` + `write_to_file`.

### Trust, then verify

Open one cited `file:line` from every report before acting on it. Reports that
cite nothing, and reports that answer a question nobody asked, are the two
reliable smells. Verify citations going *into* a prompt too: a worker handed a
path that does not resolve edits something nearby that looks plausible.

Field-tested failure modes (acceptance criteria that measure the container,
parameterized fixes checked at one boundary, invariant "measurements", stale
hazard notes, gates that cannot tell broken from busy, shared-workspace git
discipline) are in `references/doctrine.md`. Load it when designing acceptance
criteria for a non-trivial task.

## Token economics

The rules depend on how **your** model is billed. The active configuration
states it; apply the matching section.

### If the orchestrator is token-metered (`tokens`)

-   **Turn count is not billed; tokens are, and not equally.** Output and
    thinking typically cost far more per token than a cached read, often by
    more than an order of magnitude. Decide in one line; delegate prose to a
    worker.
-   **Everything in context is re-billed every turn.** A bloated context is a
    standing charge, which is why the 1% skill rule and "long output stays in
    the worker's context" are worth real money.
-   **Cache warmth.** The prompt cache has a TTL (`cache_ttl_seconds` in the
    model's billing, commonly about five minutes), and the clock starts at
    *request start*. Past it, the next turn re-bills the
    whole history at cache-write rates. Bridging a wait with real work is free
    cache refresh. For a genuinely empty wait, hold the timer yourself with
    `schedule(DurationSeconds=<ttl-60>, TimerCondition=<worker-conv-id>)` and
    make the wake turn tool-free; abandon bridging past roughly 25 minutes on a
    100k context and treat the cold window as the moment to compact.
-   **Prefix stability.** Editing an `AGENTS.md`, toggling a plugin, running
    `define_subagent`, or installing conversation-scoped agents all invalidate
    the cache. Batch such changes.
-   **Delegation has a break-even.** The delegation prompt is your output.
    Writing 800 tokens to save a two-line lookup is a loss.

### If the orchestrator is call-metered (`calls`)

-   **Turn count is the bill.** Every orchestrator turn costs one call however
    small. Batch decisions and tool calls into fewer, larger turns.
-   **Never bridge waits.** A keepalive turn costs a call and buys nothing;
    cache warmth does not touch call quota. Let the IDE wake you when workers
    finish.
-   **Delegate loops, not lookups.** A worker on a separate bucket turns a
    10-turn build-fix loop into one orchestrator turn. A single lookup costs a
    call either way, so do it yourself.
-   **Context size still matters for quality**, just not for quota: keep long
    reads in workers so your reasoning stays sharp.

### Either way

-   **A dead worker is a total loss.** Subagents can die when a message
    exceeds the size limit, usually from full-size screenshots. Downscale
    (`convert in.png -resize 1000x -quality 70 out.jpg`), cap images at 3 to 5
    per worker, and state the cap in the prompt.
-   **State the known-honest baseline.** "The suite currently fails 9 tests,
    none of them yours" gets an accurate report the first time.
-   **Reuse idle workers** with `send_message`.
-   **Concurrency has a ceiling.** Beyond 3 to 4 active agents on one machine,
    timing-sensitive gates produce false failures.

### Check live consumption

The `tokenomics-worker-tools` MCP server exposes `get_quota_status` and
`get_cache_status` (estimates from the configured billing and the local
ledger), `detect_test_tampering`, and `record_orchestration_event`. For real
quota numbers use whatever your provider's console or CLI reports; the plugin
does not query any billing backend.

## Reference pipeline

1.  **Orchestrator** classifies the tier and resolves ambiguity with the user
    before planning.
2.  **Scouts** (`tw_scout`, fan-out) surface context with grounding categories.
3.  **Orchestrator** writes a terse decision spine (decisions, constraints,
    acceptance criteria). A `tw_worker` expands it into the full plan; you audit
    the expansion against the spine. Get user approval here.
4.  **Implementers** (`tw_worker`, `share` or competing `branch`) execute the
    plan, given its path, each with a disjoint `ASSIGNED_FILES_EXCLUSIVE`.
5.  **Pre-review fixer** (`tw_redteam`, one turn, `share`) checks stubs, missing
    error paths, swallowed exceptions, out-of-manifest edits, and whether each
    new test fails with the fix stashed. It repairs once and never messages the
    implementer; a second failure is `BLOCKED`.
6.  **Orchestrator** reviews the diff and audits test changes.
7.  **Fixers** (`tw_worker`) apply review comments or the merge plan.
8.  **Verification gate:**

    ```bash
    python3 {plugin}/scripts/verify_and_record.py \
        --workspace_dir=. \
        --test_cmd="<test_command>" \
        --task_tier=tier_2_pipeline \
        --task_category=feature \
        --conversation_id=<current_conversation_id> \
        --conductor_turns=<turns> \
        --subagents_spawned=<count>
    ```

    Models, billing weights, and the plugin name default from the active
    configuration. Side modes for workers:

    ```bash
    # Every path:line citation in a plan exists and is in bounds.
    python3 {plugin}/scripts/verify_and_record.py --mode=verify-citations \
        --workspace_dir=<dir> --report-path=<plan.md>
    # Python readability heuristics (not a substitute for real linters).
    python3 {plugin}/scripts/verify_and_record.py --mode=lint-readability \
        --workspace_dir=<dir> --allowed-files=<a.py,b_test.py>
    # Last steps of an unresponsive subagent, read before killing it.
    python3 {plugin}/scripts/verify_and_record.py --mode=recover-transcript \
        --subagent-id=<conversation_id>
    ```

9.  **Shepherd** (`tw_worker`) handles push, CI, and the review loop.

Collapse steps freely for Tier 1. The invariant: subagents do the reading,
typing, and waiting; the orchestrator does the deciding.
