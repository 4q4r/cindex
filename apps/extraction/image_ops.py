"""pymupdf-backed image manipulation tools for the PERELMAN agent loop.

The vision LLM inspects rendered PDF pages / screenshots / figure images and
may call ``zoom`` / ``crop`` / ``rotate`` to examine a small region (a formula,
a graph axis, a table cell) before transcribing it. The host executes those
tools with **pymupdf only** — no Pillow (a new runtime dependency would need
explicit approval, and pymupdf is already on the stack).

Operations are pure functions over an :class:`ImageRegistry` (a per-extraction
map of ``image_id`` → stored bytes). They return new image ``bytes``; the
extractor registers the result under a fresh id (e.g. ``page-0-zoom1``) and
feeds it back to the LLM as an ``image_url`` content part.

Format support: pymupdf opens BMP / JPEG / GIF / TIFF / PNG as a 1-page
document. **WebP is not supported** as pymupdf input — ``content_fetcher``
filters figure URLs to png/jpeg/gif so WebP never reaches here, but if it does
we surface a :class:`ToolError` rather than crashing.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pymupdf

if TYPE_CHECKING:
    from .config import LLMConfig


class ToolError(Exception):
    """Raised when a tool call cannot be satisfied (bad region, unknown id).

    The agent loop catches this and returns it to the LLM as a tool error
    message so the loop can continue (the model retries or gives up on that
    region) — it never propagates out of the extraction.
    """


# pymupdf ``open`` filetype for each supported input mime. WebP is intentionally
# absent: pymupdf cannot decode it, so ``_filetype`` raises ``ToolError``.
_MIME_TO_FILETYPE: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/gif": "gif",
    "image/tiff": "tiff",
    "image/bmp": "bmp",
    "application/pdf": "pdf",
}


@dataclass(frozen=True, slots=True)
class ImageEntry:
    """A stored image available to the agent loop.

    ``width`` / ``height`` are the **source pixel dimensions** the LLM was told
    when the image was registered — crop regions and zoom factors are expressed
    in these units, then mapped into pymupdf's point space (an image document's
    page rect is the source pixels scaled by 72/96, i.e. 0.75x, so the mapping
    is not 1:1).
    """

    data: bytes
    mime: str
    kind: str  # "pdf-page" | "screenshot" | "figure" | "tool-result"
    width: int
    height: int


class ImageRegistry:
    """Per-extraction store of images keyed by ``image_id``."""

    def __init__(self) -> None:
        """Start with an empty image map."""
        self._entries: dict[str, ImageEntry] = {}

    def register(  # noqa: PLR0913  # registry insert surface is inherently wide
        self,
        image_id: str,
        data: bytes,
        mime: str,
        kind: str,
        width: int,
        height: int,
    ) -> str:
        """Store an image (with source pixel dims) and return its id."""
        self._entries[image_id] = ImageEntry(
            data=data,
            mime=mime,
            kind=kind,
            width=width,
            height=height,
        )
        return image_id

    def get(self, image_id: str) -> ImageEntry | None:
        """Return the entry for ``image_id`` or ``None`` if not registered."""
        return self._entries.get(image_id)

    def data_uri(self, image_id: str) -> str:
        """Return a ``data:{mime};base64,...`` URI for ``image_id``.

        Raises :class:`ToolError` if the id is unknown.
        """
        entry = self._entries.get(image_id)
        if entry is None:
            msg = f"unknown image_id: {image_id}"
            raise ToolError(msg)
        encoded = base64.standard_b64encode(entry.data).decode("ascii")
        return f"data:{entry.mime};base64,{encoded}"


def _filetype(mime: str) -> str:
    """Return the pymupdf filetype for ``mime`` or raise :class:`ToolError`."""
    ft = _MIME_TO_FILETYPE.get(mime.lower())
    if ft is None:
        msg = f"unsupported image mime (pymupdf cannot decode): {mime}"
        raise ToolError(msg)
    return ft


def _open_document(entry: ImageEntry) -> pymupdf.Document:
    """Open stored image bytes as a pymupdf document (1 page for images)."""
    try:
        return pymupdf.open(stream=entry.data, filetype=_filetype(entry.mime))
    except Exception as exc:  # pymupdf raises bare Exception
        msg = f"failed to open image ({entry.mime}): {exc}"
        raise ToolError(msg) from exc


def _point_scale(entry: ImageEntry, page: pymupdf.Page) -> float:
    """Source-pixel → pymupdf-point scale for ``entry`` on ``page``.

    pymupdf opens a raster image as a 1-page document whose page rect is the
    source pixels scaled by 72/96 (0.75x), so a region expressed in source
    pixels must be multiplied by this factor to land in the page's point space.
    Falls back to 1.0 (identity) when source dims are unknown (e.g. a raw PDF
    entry), in which case coordinates are treated as points.
    """
    if entry.width <= 0 or page.rect.width <= 0:
        return 1.0
    return page.rect.width / entry.width


def _rasterize(
    page: pymupdf.Page,
    *,
    matrix: pymupdf.Matrix,
    clip: pymupdf.Rect | None,
    max_long: int,
) -> pymupdf.Pixmap:
    """Rasterize ``page`` so the output long side does not exceed ``max_long``.

    If the base ``matrix`` would produce a larger pixmap, it is scaled down
    proportionally (downscaling happens at rasterization time — pymupdf
    ``Pixmap`` has no post-hoc scale method, and re-rasterizing keeps the
    result crisp rather than resampling a pixmap).
    """
    pix = page.get_pixmap(matrix=matrix, clip=clip)
    long_side = max(pix.width, pix.height)
    if max_long > 0 and long_side > max_long and long_side > 0:
        scale = max_long / long_side
        matrix = matrix * pymupdf.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=matrix, clip=clip)
    return pix


def zoom(
    reg: ImageRegistry,
    cfg: LLMConfig,
    image_id: str,
    factor: float,
) -> tuple[bytes, str]:
    """Zoom into ``image_id`` by ``factor`` (1.0 = original, 2.0 = 2x).

    Returns ``(jpeg_bytes, "image/jpeg")``. Raises :class:`ToolError` on an
    unknown id, unsupported mime, or non-positive factor.
    """
    if factor <= 0:
        msg = f"zoom factor must be positive, got {factor}"
        raise ToolError(msg)
    entry = reg.get(image_id)
    if entry is None:
        msg = f"unknown image_id: {image_id}"
        raise ToolError(msg)
    doc = _open_document(entry)
    page = doc[0]
    # Map the factor onto source pixels: pymupdf's point space is 0.75x source
    # pixels for image documents, so divide by ``_point_scale`` to make
    # ``factor=2.0`` truly double the source pixel resolution.
    scale = _point_scale(entry, page)
    f = factor / scale if scale else factor
    pix = _rasterize(
        page,
        matrix=pymupdf.Matrix(f, f),
        clip=None,
        max_long=cfg.max_image_dim,
    )
    return pix.tobytes("jpg", jpg_quality=cfg.image_quality), "image/jpeg"


def crop(
    reg: ImageRegistry,
    cfg: LLMConfig,
    image_id: str,
    region: dict,
) -> tuple[bytes, str]:
    """Crop ``image_id`` to ``region`` (``{x, y, w, h}`` in source pixels).

    The crop is supersampled 2x for text legibility, then downscaled to
    ``max_image_dim`` if needed. Returns ``(png_bytes, "image/png")`` (PNG to
    keep formulas/axis labels lossless). Raises :class:`ToolError` on an
    unknown id, malformed region, or out-of-bounds crop.
    """
    entry = reg.get(image_id)
    if entry is None:
        msg = f"unknown image_id: {image_id}"
        raise ToolError(msg)
    try:
        x = float(region["x"])
        y = float(region["y"])
        w = float(region["w"])
        h = float(region["h"])
    except (KeyError, TypeError, ValueError) as exc:
        msg = f"region must be {{x,y,w,h}} numbers, got {region!r}"
        raise ToolError(msg) from exc
    if w <= 0 or h <= 0:
        msg = f"region w/h must be positive, got w={w} h={h}"
        raise ToolError(msg)

    doc = _open_document(entry)
    page = doc[0]
    # Bounds are checked in source pixels (the units the LLM was told), then
    # mapped into pymupdf point space via ``_point_scale`` (image-doc page rect
    # is 0.75x source pixels, so the clip must be scaled to match).
    src_w = entry.width
    src_h = entry.height
    if src_w <= 0 or src_h <= 0:  # pragma: no cover - defensive (PDF entry)
        src_w = page.rect.width
        src_h = page.rect.height
    if x < 0 or y < 0 or x + w > src_w + 0.5 or y + h > src_h + 0.5:
        msg = (
            f"region ({x},{y},{w},{h}) out of bounds for image {src_w:.0f}x{src_h:.0f}"
        )
        raise ToolError(msg)

    s = _point_scale(entry, page)
    clip = pymupdf.Rect(x * s, y * s, (x + w) * s, (y + h) * s)
    pix = _rasterize(
        page,
        matrix=pymupdf.Matrix(2, 2),  # 2x supersample for legibility
        clip=clip,
        max_long=cfg.max_image_dim,
    )
    return pix.tobytes("png"), "image/png"


def rotate(
    reg: ImageRegistry,
    cfg: LLMConfig,
    image_id: str,
    degrees: int,
) -> tuple[bytes, str]:
    """Rotate ``image_id`` by ``degrees`` (multiple of 90).

    The image is embedded into a 1-page PDF so pymupdf's PDF-page rotation
    applies (image-document pages do not support ``set_rotation``), then
    rasterized. Returns ``(png_bytes, "image/png")``.
    """
    if degrees % 90 != 0:
        msg = f"rotate degrees must be a multiple of 90, got {degrees}"
        raise ToolError(msg)
    entry = reg.get(image_id)
    if entry is None:
        msg = f"unknown image_id: {image_id}"
        raise ToolError(msg)

    src = pymupdf.Pixmap(entry.data) if entry.mime != "application/pdf" else None
    if src is None:
        # PDF input — rotate its first page directly.
        doc = _open_document(entry)
        page = doc[0]
        page.set_rotation(degrees % 360)
        pix = _rasterize(
            page,
            matrix=pymupdf.Matrix(1, 1),
            clip=None,
            max_long=cfg.max_image_dim,
        )
        return pix.tobytes("png"), "image/png"

    pdf = pymupdf.open()
    page = pdf.new_page(width=src.width, height=src.height)
    page.insert_image(page.rect, stream=entry.data)
    page.set_rotation(degrees % 360)
    pix = _rasterize(
        page,
        matrix=pymupdf.Matrix(1, 1),
        clip=None,
        max_long=cfg.max_image_dim,
    )
    return pix.tobytes("png"), "image/png"


# OpenAI-compatible function schemas. The extractor tells the LLM each
# image's id and pixel dimensions in the user message, so the descriptions
# here stay static. ``region`` uses source-pixel coordinates.
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "zoom",
            "description": (
                "Zoom into an image by a positive factor (2.0 = 2x). Use to "
                "enlarge a small region for closer inspection of formulas, "
                "axis labels, or table cells. The result is a new image you "
                "can further crop/rotate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {
                        "type": "string",
                        "description": "The id of the image to zoom (e.g. page-0).",
                    },
                    "factor": {
                        "type": "number",
                        "description": "Positive zoom factor (1.0 = no change).",
                    },
                },
                "required": ["image_id", "factor"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crop",
            "description": (
                "Crop an image to a rectangular region given in source-pixel "
                "coordinates {x, y, w, h}. The crop is supersampled for text "
                "legibility. Use to extract a single formula, graph, or table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {
                        "type": "string",
                        "description": "The id of the image to crop.",
                    },
                    "region": {
                        "type": "object",
                        "description": "Crop rectangle in source pixels.",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "w": {"type": "number"},
                            "h": {"type": "number"},
                        },
                        "required": ["x", "y", "w", "h"],
                        "additionalProperties": False,
                    },
                },
                "required": ["image_id", "region"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rotate",
            "description": (
                "Rotate an image by a multiple of 90 degrees. Use to read "
                "sideways text or reorient a scanned figure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image_id": {"type": "string"},
                    "degrees": {
                        "type": "integer",
                        "description": "Rotation in degrees (multiple of 90).",
                    },
                },
                "required": ["image_id", "degrees"],
                "additionalProperties": False,
            },
        },
    },
]


def dispatch(
    reg: ImageRegistry,
    cfg: LLMConfig,
    name: str,
    arguments: dict,
) -> tuple[bytes, str]:
    """Dispatch a single tool call by name. Raises :class:`ToolError` on bad args."""
    if name == "zoom":
        return zoom(reg, cfg, arguments["image_id"], float(arguments["factor"]))
    if name == "crop":
        return crop(reg, cfg, arguments["image_id"], arguments["region"])
    if name == "rotate":
        return rotate(reg, cfg, arguments["image_id"], int(arguments["degrees"]))
    msg = f"unknown tool: {name}"
    raise ToolError(msg)
