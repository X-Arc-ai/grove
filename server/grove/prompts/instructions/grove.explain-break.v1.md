# Task: explain one existing consequence mark

The data block holds a mark that code already computed. Explain that mark only. Do not select another item, add a consequence, or decide that a claim is true.

Read the trigger's old and new state, its clocks and source type, the affected item, and the meanings on the path. Choose one short sentence form below. The item references use the exact item IDs from the data. Code inserts their titles, the source qualifier and the world date.

Return one JSON object with mark_id and parts. Return the exact mark_id from the block. Each element of parts is a whole token. An item token consists of the literal prefix item: followed by its exact ID. The other tokens are literal phrases from the response schema.

Choose one of these forms, with the affected item first and the trigger item second:

- affected item, depends on, trigger item, comma, which has, source, time, period
- affected item, is affected by, source, to, trigger item, time, period
- affected item, needs review after, source, to, trigger item, time, period

Use the punctuation characters for the comma and period tokens. Use the literal tokens source and time; do not write a source qualifier or date yourself. Never write a title or other free text into parts. Choose depends on only when the path's meanings justify that wording. A reported or planned change is not confirmed, and review is not a change to the affected item's claim.
