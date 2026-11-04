"""Private, isolated PDF text reader. Input is stdin; stdout is one bounded JSON result.

Run as a file with Python -I. It imports no application modules or environment settings.
The caller discards stderr and kills this process if the wall-clock limit expires.
"""

import io
import json
import logging
import resource
import sys

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
MAX_PAGES = 1000
MAX_STREAM = 4 * 1024 * 1024
MAX_CONTENT = 32 * 1024 * 1024
MAX_TEXT = 1_000_000


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    # Darwin does not reliably enforce address-space limits. Linux is the deployed platform.
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    logging.disable(logging.CRITICAL)
    from pypdf import PdfReader, apply_configuration

    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        return 1
    with apply_configuration(
        maximum_declared_stream_length=MAX_STREAM,
        array_based_stream_maximum_output_length=MAX_STREAM,
        zlib_maximum_output_length=MAX_STREAM,
        lzw_maximum_output_length=MAX_STREAM,
        run_length_maximum_output_length=MAX_STREAM,
        brotli_maximum_output_length=MAX_STREAM,
        stream_decoding_work_maximum_length=MAX_CONTENT,
        stream_parsing_work_maximum_count=250_000,
        page_tree_maximum_entries=MAX_PAGES * 4,
        page_tree_maximum_page_entries=MAX_PAGES * 32,
        page_tree_maximum_depth=32,
        xform_maximum_invocations_per_extraction=100,
        jbig2dec_binary=None,
    ):
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted or not 0 < len(reader.pages) <= MAX_PAGES:
            return 1
        pages = []
        content_bytes = text_chars = 0
        for page in reader.pages:
            contents = page.get_contents()
            if contents is not None:
                content_bytes += len(contents.get_data())
            if content_bytes > MAX_CONTENT:
                return 1
            text = page.extract_text()
            text_chars += len(text)
            if text_chars > MAX_TEXT:
                return 1
            pages.append(text)
        created_at = None
        try:
            created = reader.metadata.creation_date if reader.metadata else None
            if created is not None and created.utcoffset() is not None:
                created_at = created.isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
        answer = json.dumps({"pages": pages, "created_at": created_at}, ensure_ascii=True)
        if len(answer) > MAX_OUTPUT:
            return 1
        sys.stdout.write(answer)
        return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 1
    sys.exit(code)
