# Internal WhatsApp export reader

`grove.ingest.whatsapp.parse_export(Upload)` reads a UTF-8 text export, or a ZIP with
one supported transcript and its media. It returns immutable `WhatsAppExport`,
`ExportMessage`, `ExportTime`, `LocalTimeCandidate`, `AttachmentRef`, and `ExportAsset`
objects. It does not register an upload provider, change the shared transport, persist
source rows, or call a model. The complete connector still requires those integrations.

## Export facts and unknowns

Only the uploaded bytes supply source facts. No chat ID, timezone, date order, filename
metadata, or other manual field is required. The parser never uses the host locale,
current timezone, upload time, participant list, or filename to fill source identity or time.

Supported text forms are numeric Android and bracketed iOS headers. Day-first and
month-first dates use `/`, `.` or `-`; a year-first date has four year digits. A shared
date-order intersection across all messages removes interpretations that the export
contradicts. If both day-first and month-first remain possible, both are retained.
Impossible dates and inconsistent formats are typed refusals. Date order does not
establish a phone locale or a language.

A two-digit year remains two digits, with `century_unknown`. It does not use a century
pivot. Each candidate retains the printed calendar fields, and `local_at` is absent
until the full year is known. The raw timestamp and minute/second precision remain.
An explicit numeric offset or `Z` can establish an instant only when the full date is
unambiguous. Without an offset, `at` remains absent. An IANA timezone remains unknown.
The parser does not invent a UTC instant from a printed wall-clock value.

Messages retain the source's sender display text. A named sender has unresolved identity
and `third_party` speaker kind. The export does not establish which sender owns the
account. A structurally senderless notification has `automated` speaker kind. A display
name such as `Delivery Bot` does not attest a bot, and prose such as `Forwarded from ...`
does not attest an original author. Both remain source text. No message asserts a source
type, a completion, an artifact classification, or a person identity.

## Identity and provenance

`artifact_sha256` hashes the exact transcript bytes. It identifies that artifact;
`chat_identity` remains absent. The same transcript inside a different ZIP has the same
artifact digest and message keys. The ZIP filename never supplies chat identity.

A message key uses the existing `message_key` helper with the artifact digest and one
canonical JSON component. That component includes the raw source timestamp, a bounded
sender digest, a hash of the full joined message text, and the message's ordinal within
its candidate minute. JSON framing prevents component-separator collisions. Repeated
messages within a minute remain separate. Source line offsets do not enter that component.
The transcript digest still changes when the transcript bytes change.

Every message retains its original one-based line range and zero-based UTF-8 byte range.
Byte ranges include source line terminators and use the unmodified input, before BOM,
newline, NUL, and text-cap handling. Continuation lines join their preceding message with
LF. The text hash covers the full joined body before cleaning and capping.

Same-artifact import is deterministic. Different transcript artifacts use different
namespaces; they do not assert a shared chat from matching names or messages. Arbitrary
partial/overlapping exports therefore do not yet deduplicate across artifacts. The export
alone cannot establish that identity. Byte-identical exports from unrelated chats are
also indistinguishable from these bytes. This reader supplies no broader chat-identity
claim and does not claim the original A.4 cross-export guarantee.

## Order and windows

Messages use UTC order only when every message has an established instant. Otherwise,
they use printed local order only when every surviving date interpretation produces the
same order. If that cannot be established, original source order remains and
`order_basis` is `source_order_unresolved`. Original source locators remain after any sort.
Tied values preserve source order.

The versioned cut rule closes a window only for an established gap greater than 90
minutes. Exactly 90 minutes stays in the same window. Two established instants use
elapsed time. Local calendar comparisons use `export_local`, with the unknown offset
and timezone retained. Such a cut describes printed local time; it does not prove elapsed
silence across an unknown zone transition. A local split does not establish independent
earned support. An unresolved or negative gap stays in its
window with `silence_gap_unknown`. A two-digit year permits a local comparison within the
same printed date, but never invents a gap across dates with an unknown century.

These are internal window descriptions. They do not satisfy the existing writer's
mandatory aware episode bounds. Nullable source times, original local-time metadata,
and a compatible episode storage contract require owner coordination before integration.

## Media and limits

The reader recognizes `<attached: filename>` and `filename (file attached)` references,
plus explicit English omitted-media markers. It retains captions. An attachment resolves
only to an exact archive-relative path or a path beside the transcript. Two matches
refuse; basename searches across folders are forbidden. A missing or omitted file remains
an explicit reference state. Attachments retain their carrier's message key and window.
References remain even when the retained message text reaches the common text cap.

ZIP members remain in memory. No path is extracted or read from the filesystem.
Absolute paths, backslashes, traversal components, control characters, duplicates, and
nonregular members refuse. Assets retain their exact bytes, digest, member locator, and
suffix-based media routing hint. The hint does not validate the media or classify its
content as an artifact. Durable storage and media decoding remain separate work.

The reader uses the shared upload byte, record, chat, message, and text budgets. ZIP
transcript discovery also charges record reads. Input transcript and member sizes are
at most 32 MiB. The ZIP body and total expanded members are each at most 64 MiB.
An export has at most 1,000 attachment references. Names and senders obey the existing
1,024-character identifier limit. All error strings are generic and no result repr
includes message text, sender text, attachment names, or media bytes.

The ZIP library allocates its central directory before the member-count check, as in the
existing archive reader. The numeric header syntax cannot distinguish a pasted line
that exactly imitates an export header. Localized attachment-marker words outside the
listed forms remain text. The retained assets still allow later integration to report
unreferenced media. No complete D5/D10 or full-slice R4 acceptance follows from these tests.
