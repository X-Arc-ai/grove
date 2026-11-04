# Internal audio conversion

`grove.ingest.audio.normalize_audio(data, budget=..., sample_rate=16000)` accepts bytes from one
WAV, Ogg Opus or M4A artifact. It returns mono PCM16 WAV at 16 or 24 kHz, the original bytes,
both SHA-256 hashes, the detected media type and the exact decoded frame count.

The converter accepts no filename, path, URL, sender or source timestamp. Frame duration is
the duration of decoded audio. It never establishes when a voice note was sent or recorded.
The caller must retain the original artifact and commit its source and episode before calling
the metered Voice adapter. This module makes no model call, writes no source, and registers no
upload provider. The existing model layer splits normalized WAV by actual frames below the
ten-minute provider limit, including a final partial chunk.

## Runtime and limits

The runtime needs `ffmpeg` and `ffprobe` on its trusted PATH. Missing binaries raise
`AudioDecoderUnavailable`; malformed or excessive input raises `NotAnExport`. Neither error
includes decoder diagnostics, source content or private paths.

The converter requires complete container boundaries. WAV must declare exactly its bytes,
complete PCM or float samples and a consistent frame size. Ogg must contain one complete Opus
stream with its final page. Chained Ogg streams are refused. M4A must contain complete top-level
boxes, including its file type, movie and media data. Exactly one audio stream is accepted;
video, additional tracks and cover-art streams are refused instead of silently discarded.

| Resource | Bound |
|---|---|
| Input | 32 MiB |
| Decoded audio | One hour; also limited by the remaining upload budget |
| Input format | 1–2 channels, 8–96 kHz |
| Container parts | 100,000 |
| Probe output | 4 KiB |
| Decoder output | PCM byte bound enforced on the output file before Python reads it |
| Each process | 20 CPU seconds, 45 elapsed seconds, 32 file descriptors, no core dump |
| Linux address space | 768 MiB |
| FFmpeg allocation | 32 MiB per allocation on all systems |
| Decoder/filter threads | One |

The decoder receives a closed container selection and a file-only input protocol. MOV external
tracks and absolute aliases are disabled. Input bytes go to a mode-600 file inside a private
temporary directory. Output uses a private temporary file. Both are removed after success or
failure. The child environment contains no application credentials. A separate Python process
sets OS limits before exec, without a shell or `preexec_fn` in the API process.

FFmpeg error detection refuses decode errors; it does not certify the authenticity or completeness
of a file whose container itself falsely describes a shorter valid recording. M4A codec padding
can affect decoded duration. The returned frame count describes the decoded samples and does not
assert precise acoustic alignment. macOS does not receive the Linux address-space limit; its
allocation, duration, output, CPU and elapsed-time bounds still apply.

## Verification boundary

The tests generate synthetic silent audio, exercise WAV and compressed formats through the real
local decoder, and test limits and subprocess failures. Decoder-dependent tests explicitly skip
when the runtime lacks FFmpeg. Pure refusal and process-limit tests still run there. Such skips
do not establish production or CI decoder acceptance. Deployment and CI dependency installation
remain owner-coordinated work outside this component.

The controls follow FFmpeg's [command options](https://ffmpeg.org/ffmpeg.html),
[protocol restrictions](https://ffmpeg.org/ffmpeg-protocols.html), and
[probe interface](https://ffmpeg.org/ffprobe.html). The local executable help also lists the MOV
external-track controls and the resource options used by this component.
