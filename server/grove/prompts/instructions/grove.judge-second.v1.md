# Task: give a second opinion on one claim

You are the second judge. Another model judged this claim already. You do not see its answer, and you must not guess it.

Block 1 holds a claim. Each later block holds an excerpt of a source. For each excerpt, decide one thing: does this excerpt, read alone, prove the claim?

- supports: yes. The excerpt itself proves the claim as it is written.
- contradicts: no. The excerpt proves that the claim is false or has stopped being true.
- not_about_claim: the excerpt proves neither. This includes a plan, a booking for a later date, a report that something was done when the excerpt is not the record of the act, an excerpt about another subject, and any case you cannot decide.

Use only the text in the blocks. A date that has passed proves nothing by itself.

Give each answer a confidence from 0 to 1.

Answer with one JSON object. Its one key is verdicts: a list with exactly one entry for each excerpt block and no entry for block 1. Each entry holds block (the number of the block), verdict (supports, contradicts or not_about_claim) and confidence (a number from 0 to 1).
