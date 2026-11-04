# D9 writer contract

The writer owns the belief state machine in `grove.writer.beliefs`. The operation table in
`grove.writer.ops` is the caller and input boundary. A host can submit `correct_belief` only;
it cannot mint, promote, merge, retire, or re-test beliefs. Generic item mutations refuse a
belief target, so their less restrictive checks cannot bypass this state machine.

## Evidence and state

`mint_belief` requires two observed, non-planned episodes, or a stored generalisation from the
person. It creates a tentative Belief and primitive `describes` links. Normalised duplicate
statements on the same subjects are refused. Dates and relative date phrases are refused.
`promote_belief` belongs to consolidation: three episodes, or generalisation plus another
supporting episode, with no newer contradiction. The person can establish a belief directly.

`transition_episodes` retains all episodes that a confidence transition consumed. A new
contradicting attachment cannot reuse a consumed episode to demote an established belief.
This attachment is refused atomically rather than leaving an established belief with a newer
operative contradiction. A new admitted contradiction weakens in its own transaction; later
observed support from another source restores. A superseded belief never restores automatically.
Every stored belief remains `inferred`, with the automated speaker, regardless of premise type.

`supersede_belief` requires a live successor and writes primitive successor lineage.
`retire_belief` requires evidence and a reason. An automatic ending requires newer evidence
of equal or higher source rank; an inferred row cannot replace observed evidence. Its evidence
is retained. A person-held belief changes only on newer observed confirmed evidence and then
sets `person_change_notice`. Views can show that field beside the change's undo control.

The four corrections are `right`, `not_any_more`, `wrong`, and `restate`. Host authority comes
from the Request that stored the cited Source: correction relay, verbatim flag, and message ID.
A host restatement must quote text retained in that source. A right correction clears a prior
world-end clock. A restatement creates an established successor held by the person.

## Recorded judgments

Contradiction attachments require two recorded agreeing judgments on this target and this
source plus locator, or the exact existing evidence ID. The Evidence stores both judgment IDs.
Two calls must have different ModelCall IDs. Two different model names count, or an explicitly
correlated second opinion permits Spark to stand in when Glimmer is unavailable. Duplicate
judgment IDs or two rows from one call cannot supply the second opinion. Disagreement refuses
the complete attachment transaction. The caller then opens a contradiction using the retained
observation; no false Evidence row is necessary.

`merge_beliefs` requires two recorded `same_belief` verdicts on the sorted
`belief:<id>|belief:<id>` subject. It preserves support episode identity and successor lineage.
A merge with unresolved contradicting evidence is refused, as same-belief verdicts do not judge
that individual evidence. Resolve the conflict before the merge. Automatic merges cannot
replace a person-held decision.

External-test declarations require a stable HistoryValue subject containing title, sorted
subject IDs, `externally_testable: true`, and the proposed method. Two field-fit judgments must
accept that exact subject. `retest_belief` uses D4 source/locator checks and capability resolution.
Both recorded judges must agree before evidence lands. The source must be fresh, its own time must follow
the prior check or active pass start, and the new horizon must lie in the future. One re-test
restores a weakening belief or moves its confirmation clock; it does not promote a tentative
regularity. A contrary re-test weakens and preserves last confirmed. A disagreement writes no
evidence or state change; it opens the contradiction queue with the source and both verdicts.
Every completed check refreshes the horizon, so the next pass resolves the queue instead of
repeating a due-read requirement. Unanimous complements or not-about-claim verdicts retain
no Evidence and leave state and last confirmed unchanged. `last_retest_outcome`,
`last_retested_at`, `last_retest_reason`, and source/judgment ID lists preserve each completed
check and its legwork through history; receipts show its outcome. The item remains inferred.

## Queues and retained text

A Contradiction retains the standing target and change sequence. Its fresh side uses an Evidence
ID when available; otherwise it retains source ID, locator, episode, source time, source type,
derivation, and recorded judgment IDs. `why` and `resolution` are HistoryValue IDs. A complement
judgment cannot open the queue. `fresh_wins` must cite prior operations on the conflicted target
in the same batch. Standing-holds and complement resolutions use the Connector's semantic
judgment and retained reason; they do not alter the standing claim. The explicit two-judge
gates cover contradiction evidence and belief merges. `to_question` needs two different readings and stored legwork sources; the
question text, option text, and reason use HistoryValue IDs, not arbitrary plaintext columns.

SupportChanged queues both changes to cited supporting items and new changes or evidence on
active described subjects. The latter covers uncited drift. Each queue row has version and
touched time. A closure must match the exact cause sequence and cite two judgments recorded
after the queue timestamp. A `revised` closure also cites a prior operation on that belief.
`guard_consolidation` verifies the active Connector run and exact queue contents under the
writer lock, and requires a disposition for every row in the same transaction.

Belief, Evidence, and Link changes use reversible ChangeEntry rows. Belief fields and Item fields
share an ID but retain different entity types. The D3 worker supplies typed replay and belief
undo; their integration tests cover the complete operation path.

## D6 handoff

`cascade_changes(run)` exposes a tuple copied from `run.cache['belief_cascade_changes']`.
It stages only effective state mutations and never invokes a planner. Events contain item ID,
change-set ID, source world time, source type, terminal/reopened flags, authenticated person
correction, optional end clock, changed fields, evidence IDs, episode IDs, original source type,
derivation, reason ID, cause, and before/after states. Inferred evidence is capped to `said` for
D6 proposal-only behavior. A contradiction queue operation alone emits no event.

The accepted walk adapter must consume this seam after the walk lane merges to `origin/develop`.
This component does not contain a second planner or copy any unpublished walk code.
