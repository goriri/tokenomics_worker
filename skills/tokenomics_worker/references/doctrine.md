# Tokenomics Worker field doctrine

Hard-won failure modes from orchestrator/worker sessions. They are
independent of which models fill the roles.
Load this file when designing acceptance criteria or reviewing a worker's
evidence; it is deliberately kept out of SKILL.md to protect orchestrator
context.

## The acceptance test is yours, and it is the likeliest thing to be wrong

A worker asked to make a panel fit a short viewport reported the panel's bottom
edge, its headroom, and zero overflow. Every number was true and the defect was
untouched: the content *inside* the panel was clipped by an inner scroll
container. "Nothing overflows the viewport" and "the user can see all the
content" are different assertions, and only the second one matters.

A criterion phrased in terms of the container measures the container. The same
session produced the geometric version of this: a worker satisfied an
aspect-ratio band and a no-clipping check simultaneously by shrinking text to
roughly six physical pixels. Clipping detection is satisfied by text nobody can
read.

Before sending a prompt, write down what a malicious worker could do that
satisfies your criterion while leaving the problem in place. If that is easy,
the criterion is wrong and no amount of worker diligence will save it.

## Evaluate a parameterized fix at the opposite boundary

A CSS sizing expression was reasoned about on one viewport and shipped. On a
viewport *narrower* than the target aspect it did not remove the letterbox, it
rotated it, producing 135px of overflow and pushing a panel off-screen on a size
nobody had checked. Separately, a missing-asset report was derived from counting
17 files against 16 names without ever asking what the 17th was.

When a change is parameterized by a variable (such as a viewport dimension, a
file count, or a version), name the variable and evaluate both ends of it. This
is the fix-side sibling of preferring a property-based test over one that pins
the instances you happened to find.

## A number that cannot vary is not a measurement

A worker reported an element occupying 28.35% of frame height at two different
viewports, identical to four significant figures. That is what a `vh`-derived
figure looks like when it is computed rather than measured: the unit guarantees
the constant. It confirmed a stylesheet had parsed and said nothing about where
anything landed. Re-measured in raw pixels, the element overlapped its neighbour
by 48px.

Ask for raw units and an explicit predicate ("does A's bottom exceed B's top,
yes or no") rather than percentages. If a reported number is invariant across
the conditions it was supposedly measured under, it is a restatement of its own
unit.

## Review a rule-driven edit across the whole corpus

A worker added a text transform that strips redundant clauses and verified it by
eyeballing two records. Run across all 111, it modified six and damaged one,
deleting half of that record's meaning. Any transform that removes content must
be diffed over the entire corpus and the diff read item by item. Repair such a
transform at its **post-condition** (assert that the output is well-formed)
rather than by patching the single pattern that happened to misfire.

## An inherited hazard note decays exactly like an inherited measurement

Three false alarms in one session, all the same shape: a claim carried forward
and never re-checked against source. A missing-asset report where the extra file
turned out to be a symlink. A standing warning that a path helper
double-prefixes an already-prefixed argument, which would have dispatched a
worker to "fix" 18 correct call sites; the helper explicitly guards that case,
one line into its body. The boundary error above.

A stale hazard costs more than a stale measurement, because it sends work
against something that was never broken. Before repeating a warning into a
prompt, open the file it is about.

## A gate that cannot tell "broken" from "busy" is not yet a gate

Under concurrent browser-driving workers, a visual proof script exits 1 with a
navigation timeout and **zero** failure lines. Genuine breakage also exits 1.
Read the log before believing any exit 1 from a gate that drives a browser, a
network, or a device, and prefer giving infrastructure failure a distinct exit
code so the ambiguity stops existing. Workers round exit 1 up to success;
orchestrators round it down to regression. Both are guessing at a number carrying
two meanings.

## Verify a citation before it becomes a worker's input

Trust-then-verify covers claims coming out of a worker. It applies just as much
going in. A scout's audit cited two files: one did not exist in the repository
at all, and the other's line number pointed at a closing brace. A worker handed
a path that does not resolve does not stop and ask; it finds something nearby
that looks plausible and edits that. One `view_file` per citation, before the
citation becomes an instruction.

## Shared-workspace git discipline

With `Workspace: 'share'`, several workers hold uncommitted changes in one tree
and their mistakes land on each other.

-   Instruct `git add` with **explicit paths**. Never `git add -A` or `git add
    .`.
-   Forbid `git checkout`, `git restore` and `git stash` on any path outside the
    ownership list. One worker "tidied" by reverting a pre-existing modification
    that was not its own, which was harmless that time but causes silent data
    loss the next.
-   Require the commit SHA and `git log -1 --stat`, and reconcile against `git
    status` yourself. "Complete" with nothing committed has happened more than
    once, and the work is one careless command away from gone.
