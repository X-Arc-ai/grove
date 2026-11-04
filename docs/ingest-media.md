# Internal document and media components

These components prepare the remaining A.4 connectors. They are not registered upload providers.
The upload API and its input contract remain unchanged. A successful component test is not
end-to-end connector acceptance.

## Documents

`grove.ingest.documents` parses text/Markdown sections and PDF pages with their source offsets.
The original document hash and the page or byte offset determine a part's message key.
Source metadata dates describe the source; they do not establish that a narrated event happened.
An image-only PDF page remains pending image extraction rather than disappearing from the result.
The document remains one logical episode even when it has several parts.

PDF parsing uses a separate bounded worker. The parent supplies bytes through stdin and suppresses
worker diagnostics that could expose source text. The worker has CPU, decompression, page, and
output bounds. Linux also has an address-space bound; the parent enforces a timeout on every OS.
These controls complement the shared upload budget. They are not a claim that a parser can accept
every PDF.

## Images

`grove.ingest.images.prepare_image` validates a static PNG, JPEG, WebP, or GIF, checks the pixel
count, and fully decodes it before a model call. It retains the original bytes and their SHA-256
in its result. The model receives a PNG containing pixels without ancillary metadata.
The helper charges both byte representations to the upload budget. It refuses multiple frames
instead of losing later frames.

`read_image` calls the metered `image_read` job: Glimmer, low effort, no tools. A fixed prompt
treats the image and caption as untrusted data. The schema accepts only transcribed text, not a
source type, speaker, or confirmation. An unreadable image is an explicit failure.

The caller must retain the image through the writer and commit its source and episode before
calling the adapter. The current result retains bytes in memory only; durable binary evidence
and connector integration are not implemented by this component. Modal inputs remain synthetic
under the existing data-flow restriction.

## Voice

`ModelLayer.transcribe` keeps `PUSH_TO_TALK` as its default. Existing callers send the same request
fields. Internal callers can select `ENDPOINTING` or `DIARIZATION`, and supply `keywords` and
`language_bias` as string lists. The wire field for the latter is `languageBias`.

The result includes `VoiceTurn` values. Each retains the provider turn ID, text, detection offsets,
chunk index, model-call ID, and optional speaker label. Chunk offsets follow actual WAV frames,
not the provider's billing duration. The label `A` in one chunk does not identify label `A` in
another chunk. Empty partial turns remain empty. Malformed turns are metered as invalid output.

These fields follow the [Voice schema](https://dev.meta.ai/docs/api-reference/voice/schemas).
Offsets are detection boundaries, not precise acoustic alignment. Hint lists influence recognition;
they do not prove spelling or speaker identity. See [Speech to text](https://dev.meta.ai/docs/speech-to-text).

`grove.ingest.voice.transcribe_wav` accepts normalized mono 16-bit PCM at 16 or 24 kHz, as required
by the [Voice API cookbook](https://github.com/meta-models/meta-model-cookbook/tree/main/06_muse_voice/01_voice_api_fundamentals).
It requests diarization and preserves source time separately from each detection offset.
An absent source time stays absent. Truncated audio is refused before a model call.
The existing model layer splits long audio into pieces below ten minutes and meters every piece.

The caller must commit source and episode provenance before transcription. Conversion from other
audio formats, original compressed-file provenance, speaker resolution, durable source writes,
and upload integration remain connector work. No upload route invokes these adapters yet.

## Verification scope

The document and media tests use invented fixtures. The two `live` adapter probes use macOS `say`
and a generated receipt image. They exercise the provider route and metering only. They do not
establish a public deployment, durable image retention, WhatsApp identity, or complete D5/D10
acceptance.
