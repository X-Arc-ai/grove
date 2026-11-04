"""Internal image preparation and Glimmer adapter (A.4). No upload route is registered here.

The caller retains the original bytes as evidence and commits its source and episode before
requesting text. A model result is a transcription, never authority for a source type or speaker.
Only synthetic input is permitted until the data-flow conditions for Modal are satisfied.
"""

import base64
import hashlib
import io
import warnings
from dataclasses import dataclass, field

from PIL import Image, UnidentifiedImageError

from grove.ingest.connector import NotAnExport, ReadBudget, clean_text
from grove.models.errors import InvalidOutput
from grove.models.layer import ModelLayer
from grove.prompts.builder import DataBlock, build_prompt

MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_PIXELS = 16_000_000
FORMATS = ("PNG", "JPEG", "WEBP", "GIF")
TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 100_000, "pattern": r"\S"}
    },
    "required": ["text"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class RasterImage:
    sha256: str
    width: int
    height: int
    original: bytes = field(repr=False)
    model_png: bytes = field(repr=False)


@dataclass(frozen=True)
class ImageText:
    image_sha256: str
    text: str = field(repr=False)
    model_call_id: str


def prepare_image(data: bytes, *, budget: ReadBudget) -> RasterImage:
    """Validate and fully decode a raster before sending it. Preserve original evidence bytes.

    The model copy contains pixels only, without EXIF, comments, or other file metadata.
    Multiple frames need individual provenance; this adapter refuses them rather than losing
    frames. It does not alter Pillow's process-wide pixel limit or decoder settings.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise NotAnExport("the image is empty or exceeds the image byte limit")
    budget.take_bytes(len(data))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=FORMATS) as original:
                width, height = original.size
                if width * height > MAX_PIXELS or min(width, height) < 1:
                    raise NotAnExport("the image exceeds the pixel limit")
                if getattr(original, "n_frames", 1) != 1:
                    raise NotAnExport("the image has more than one frame")
                original.load()
                # A fresh image carries no ancillary metadata, including profile and EXIF data.
                pixels = original.convert("RGBA")
                clean = Image.new("RGB", pixels.size, "white")
                clean.paste(pixels, mask=pixels.getchannel("A"))
                encoded = io.BytesIO()
                clean.save(encoded, format="PNG")
    except NotAnExport:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise NotAnExport("the file is not a readable raster image") from None
    png = encoded.getvalue()
    if len(png) > MAX_IMAGE_BYTES:
        raise NotAnExport("the decoded image exceeds the image byte limit")
    budget.take_bytes(len(png))
    return RasterImage(hashlib.sha256(data).hexdigest(), width, height, data, png)


def read_image(
    layer: ModelLayer, world_id: str, image: RasterImage, *, source_id: str, caption: str = ""
) -> ImageText:
    """Read a retained synthetic image through the metered layer. Caption is screened text.

    Source/episode persistence belongs to the caller's writer transaction, not this model
    adapter. No endpoint invokes this adapter until the source integration is complete.
    """
    prompt = build_prompt(
        "grove.image-read.v1",
        [DataBlock("source", "third_party", None, source_id, clean_text(caption))],
    )
    messages = prompt.messages()
    messages[1]["content"] = [
        {"type": "text", "text": prompt.data},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(image.model_png).decode("ascii")
            },
        },
    ]
    result = layer.chat(
        world_id,
        job="image_read",
        messages=messages,
        schema=TEXT_SCHEMA,
        schema_name="image_text",
        tools=None,
    )
    text = clean_text(result.data.get("text") if isinstance(result.data, dict) else None)
    if not text.strip():
        raise InvalidOutput(
            "the image yielded no readable text", details={"model_call_id": result.model_call_id}
        )
    return ImageText(image.sha256, text, result.model_call_id)
