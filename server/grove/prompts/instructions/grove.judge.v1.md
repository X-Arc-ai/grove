# Task: judge one claim against its evidence

Block 1 holds one claim that Grove keeps. Every later block holds one piece of evidence: an excerpt of a stored source, with its speaker and its source type in the start tag.

For each piece of evidence, read only the excerpt in that block and the claim in block 1. Do not use what you know from elsewhere. Then give one verdict for that block:

- supports: the excerpt shows that the claim is true, as the claim is written.
- contradicts: the excerpt shows that the claim is not true, or is no longer true.
- not_about_claim: the excerpt does not speak to the claim, or it is too unclear to decide.

Rules for the verdict:

- A plan, a booking, a reminder or a calendar entry for a future date shows that an intention or a schedule exists. It does not show that the planned thing happened.
- A message that says something was done shows that someone reported it. It supports a claim that the report was made, and it is not_about_claim for a claim that the thing happened, unless the excerpt itself is the record of the act (a receipt, a ticket, a merged commit, a page that states it).
- A pull-request title, a commit message or a profile does not show that a feature is live or where someone lives.
- An excerpt about another person, another thing or another time than the claim is not_about_claim.
- When you are not sure, choose not_about_claim.

Give each verdict a confidence from 0 to 1: how sure you are that the verdict is right.

Answer with one JSON object of the form {"verdicts": [{"block": <block number>, "verdict": "<verdict>", "confidence": <number>}]}, with exactly one entry for each evidence block and no entry for block 1.
