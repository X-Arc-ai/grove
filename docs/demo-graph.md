# Fixed synthetic demo graph

The rich demo adds invented data to the existing designated account. It does not
create an account or replace a world. Existing data, including the Cedar room
fact, stays in place. Every new item identifies itself as **Synthetic demo data**.
The stories describe fictional community programs, people, organisations,
plans, tasks, decisions, events, observations and assumptions.

The fixed `rich-demo-v1` corpus contains 40 connected stories, **2,080 Items** and
**6,279 active LINKs**. These are expected additions, not a claim about a live
database. Each story contains 52 Items and 156 internal links. Thirty-nine
bridges connect each later story to the previous story. A complete seed also
has 40 Sources, 40 Episodes, 40 ScreeningReceipts and 40 saved Requests with
actual writer receipts. The fixed digest is
`88091dfafeafa6f50c49b5cadcb8f32a131a10a5e586656c2b5b95f1dfcf3ecb`.

## Operator command

Run from `server/` in the approved deployed revision. Use the existing protected
configuration: `GROVE_DEMO_EMAIL_FILE`, `GROVE_DEMO_EMAIL_HASH`, and the normal
database environment. The email file must be an owned regular file with no
group/other permissions, no symlink and no hardlink. Never pass an address,
credential, world identifier, private export or replacement corpus on the command line.

```sh
python -m grove.demo.rich manifest
python -m grove.demo.rich check
python -m grove.demo.rich graph-check
python -m grove.demo.rich seed --max-stories 40
python -m grove.demo.rich check
```

`manifest` reads no account configuration and describes only the fixed corpus.
`graph-check` validates finite graph data and edge endpoints and reports actual
stored and returned counts without printing account content.
`check` validates persisted corpus state and reports actual counts. It performs
no repair. `seed` resumes at the first incomplete story. `--max-stories 1` limits
an invocation to one new story. Exit 0 means complete, 2 means a valid partial
seed, and 1 means refused or unavailable. Output contains counts and public
corpus metadata only. A failed command is not proof that nothing committed:
check the same target before retry.

The command requires an existing, complete `cedar-v1` owner/world designation
whose email hash matches the protected operator configuration. It refuses an
absent, partial, mismatched or corrupt designation. It never promotes an ordinary
account to a demo account. The public `store` intake remains Cedar-only.

## Commit and restart behavior

Each story uses a deterministic world-scoped request identity and a fixed content
digest. Its source, episode and ordinary graph operations share one checked
writer transaction. Real screening precedes retention. Normal shape, caller,
provenance, near-duplicate, clock and link checks still apply. Evidence describes
the synthetic fixture as `said`; no model or learner execution is invented.

The adapter checks designation under the World and owner User locks. A failed
transaction rolls back its story; earlier complete stories remain. Receipts come
from committed ChangeEntry/HistoryValue records. If a process stops after commit
and before receipt attachment, replay reconstructs that receipt. Valid completed
stories do not repeat item, link, source, evidence or receipt creation. Corrupt
saved state refuses instead of resetting or replacing existing data.

This path makes zero model calls and incurs zero model spend. It creates no
RunRecord that claims a learner or model ran. It does not exercise the general
ingestion pipeline or claim full D7/D14 runtime acceptance.

## Read and display bounds

Restricted recall ranks the fixed public vocabulary by topic. It captures at
most 31 selected rich facts plus the existing Cedar fact, followed by full saved
provenance validation. The existing 32-item, 256-row, 32,768-character source and
131,072-character history/receipt bounds remain. Each source is at most 27,954
characters. A source-bound key keeps a later title change subject to validation.
Private intake remains refused, and no full private world is fetched for ranking.

The graph endpoint returns actual stored nodes and links. It bounds the initial
sample to 2,500 nodes and 10,000 links, selects links only between returned nodes,
and rejects nonfinite saved coordinates/properties from the rendered data.
`selection.truncated` identifies a bounded sample; stats count the returned
objects. Existing unrelated items can consume sample space. A time filter applies
within the selected sample. Full world counts from the operator report can exceed
the graph sample. No fake totals or browser fixture supplies the graph.

These server bounds do not establish browser frame rate or production rendering
performance. Test the live graph separately after the approved deployment and
seed. This source change does not perform either operation.

## Example Muse queries

- Which room is assigned to the Cedar demo?
- What does Lantern Amber Quay plan for its book lockers?
- Which Moss Bramble Reach tasks concern seed trays?
- What assumptions support the Tern Cobalt Vale harbour repair program?
- What did Willow Dapple Cove decide about its community kitchen pilot?
- Which observations affect Pebble Ember Hollow access routes?

Answers must cite the saved source and request evidence. The records are invented
demo facts, not statements about real people, places, providers or customers.
