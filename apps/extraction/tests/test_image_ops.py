"""Unit tests for :mod:`apps.extraction.image_ops` (real pymupdf, no mocks).

Exercises the PERELMAN agent-loop image tools — ``zoom`` / ``crop`` / ``rotate``
— against genuinely generated pymupdf output (a blank A4 page PNG and a 1-page
PDF) so the coordinate mapping (source pixels → pymupdf point space, which is
0.75x for image documents) is verified end to end. ``ImageRegistry`` round-trip,
out-of-bounds / unknown-id / WebP ``ToolError`` paths, and the OpenAI tool
schemas are all covered.
"""

from __future__ import annotations

import base64

import pymupdf
import pytest

from apps.extraction.config import LLMConfig
from apps.extraction.image_ops import (
    TOOL_SCHEMAS,
    ImageRegistry,
    ToolError,
    crop,
    dispatch,
    rotate,
    zoom,
)


def _cfg() -> LLMConfig:
    return LLMConfig(
        base_url="http://llm.example.com/v1",
        api_key="secret",
        model="vision-model",
        max_image_dim=4000,
        image_quality=85,
    )


def _blank_png() -> tuple[bytes, int, int]:
    """Return (png_bytes, width, height) for a real blank A4 page."""
    doc = pymupdf.open()
    page = doc.new_page()
    pix = page.get_pixmap()
    return pix.tobytes("png"), pix.width, pix.height


def _one_page_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    return doc.tobytes()


def _decode_dims(image_bytes: bytes) -> tuple[int, int]:
    pix = pymupdf.Pixmap(image_bytes)
    return pix.width, pix.height


class TestImageRegistry:
    """``ImageRegistry`` storage + ``data_uri`` round-trip."""

    def test_register_get_round_trip(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        returned = reg.register("p0", png, "image/png", "pdf-page", w, h)

        assert returned == "p0"
        entry = reg.get("p0")
        assert entry is not None
        assert entry.data == png
        assert entry.mime == "image/png"
        assert entry.kind == "pdf-page"
        assert entry.width == w
        assert entry.height == h

    def test_get_unknown_returns_none(self) -> None:
        assert ImageRegistry().get("nope") is None

    def test_data_uri_round_trips_bytes(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("fig-0", png, "image/png", "figure", w, h)

        uri = reg.data_uri("fig-0")
        assert uri.startswith("data:image/png;base64,")
        encoded = uri.split(",", 1)[1]
        assert base64.standard_b64decode(encoded) == png

    def test_data_uri_unknown_raises_tool_error(self) -> None:
        with pytest.raises(ToolError, match="unknown image_id"):
            ImageRegistry().data_uri("missing")


class TestZoom:
    """``zoom`` enlarges by the factor in source-pixel space."""

    def test_factor_two_doubles_source_pixels(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        out, mime = zoom(reg, _cfg(), "p0", 2.0)

        assert mime == "image/jpeg"
        ow, oh = _decode_dims(out)
        # 2.0x source pixels, within the max_image_dim cap (4000 > 1684).
        assert ow == pytest.approx(w * 2, abs=2)
        assert oh == pytest.approx(h * 2, abs=2)

    def test_downscales_to_max_image_dim(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)
        cfg = LLMConfig(
            base_url="http://x",
            api_key="k",
            model="m",
            max_image_dim=500,
            image_quality=85,
        )

        out, _ = zoom(reg, cfg, "p0", 4.0)

        ow, oh = _decode_dims(out)
        assert max(ow, oh) <= 500

    def test_non_positive_factor_raises(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        with pytest.raises(ToolError, match="positive"):
            zoom(reg, _cfg(), "p0", 0.0)

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(ToolError, match="unknown image_id"):
            zoom(reg=ImageRegistry(), cfg=_cfg(), image_id="missing", factor=2.0)


class TestCrop:
    """``crop`` returns a supersampled sub-rectangle; OOB / malformed raise."""

    def test_subrectangle_dimensions(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        out, mime = crop(reg, _cfg(), "p0", {"x": 10, "y": 10, "w": 100, "h": 100})

        assert mime == "image/png"
        ow, oh = _decode_dims(out)
        # 100 source px → 75 pt (0.75 scale) → 150 px after 2x supersample.
        assert (ow, oh) == (150, 150)

    def test_out_of_bounds_raises(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        with pytest.raises(ToolError, match="out of bounds"):
            crop(reg, _cfg(), "p0", {"x": 0, "y": 0, "w": 10000, "h": 10000})

    def test_nonpositive_size_raises(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        with pytest.raises(ToolError, match="positive"):
            crop(reg, _cfg(), "p0", {"x": 0, "y": 0, "w": 0, "h": 100})

    def test_malformed_region_raises(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        with pytest.raises(ToolError, match="region"):
            crop(reg, _cfg(), "p0", {"x": 0, "y": 0})  # missing w/h

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(ToolError, match="unknown image_id"):
            crop(
                reg=ImageRegistry(),
                cfg=_cfg(),
                image_id="missing",
                region={"x": 0, "y": 0, "w": 10, "h": 10},
            )


class TestRotate:
    """``rotate`` returns bytes; 90° swaps dimensions; non-multiples raise."""

    def test_rotate_90_swaps_dimensions(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "figure", w, h)

        out, mime = rotate(reg, _cfg(), "p0", 90)

        assert mime == "image/png"
        ow, oh = _decode_dims(out)
        assert (ow, oh) == (h, w)

    def test_rotate_180_preserves_dimensions(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "figure", w, h)

        out, _ = rotate(reg, _cfg(), "p0", 180)

        ow, oh = _decode_dims(out)
        assert (ow, oh) == (w, h)

    def test_rotate_pdf_page(self) -> None:
        pdf = _one_page_pdf()
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        page = doc[0]
        w, h = int(page.rect.width), int(page.rect.height)
        reg = ImageRegistry()
        reg.register("page-0", pdf, "application/pdf", "pdf-page", w, h)

        out, mime = rotate(reg, _cfg(), "page-0", 90)

        assert mime == "image/png"
        ow, oh = _decode_dims(out)
        assert (ow, oh) == (h, w)

    def test_non_multiple_of_90_raises(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "figure", w, h)

        with pytest.raises(ToolError, match="multiple of 90"):
            rotate(reg, _cfg(), "p0", 45)

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(ToolError, match="unknown image_id"):
            rotate(reg=ImageRegistry(), cfg=_cfg(), image_id="missing", degrees=90)


class TestUnsupportedFormats:
    """WebP is not decodable by pymupdf — surfaces as ``ToolError``."""

    def test_webp_input_raises_tool_error(self) -> None:
        reg = ImageRegistry()
        # A fake WebP payload; pymupdf cannot decode WebP regardless of bytes.
        reg.register(
            "fig-w",
            b"RIFF\x00\x00\x00\x00WEBP",
            "image/webp",
            "figure",
            10,
            10,
        )

        with pytest.raises(ToolError, match="unsupported image mime"):
            zoom(reg, _cfg(), "fig-w", 2.0)


class TestDispatchAndSchemas:
    """``dispatch`` routes by name; ``TOOL_SCHEMAS`` covers the three tools."""

    def test_dispatch_zoom(self) -> None:
        png, w, h = _blank_png()
        reg = ImageRegistry()
        reg.register("p0", png, "image/png", "pdf-page", w, h)

        out, mime = dispatch(reg, _cfg(), "zoom", {"image_id": "p0", "factor": 2.0})

        assert mime == "image/jpeg"
        assert _decode_dims(out)  # decodable

    def test_dispatch_unknown_tool_raises(self) -> None:
        with pytest.raises(ToolError, match="unknown tool"):
            dispatch(reg=ImageRegistry(), cfg=_cfg(), name="noop", arguments={})

    def test_tool_schemas_cover_three_tools(self) -> None:
        names = {t["function"]["name"] for t in TOOL_SCHEMAS}
        assert names == {"zoom", "crop", "rotate"}
        for schema in TOOL_SCHEMAS:
            assert schema["type"] == "function"
            params = schema["function"]["parameters"]
            assert params["type"] == "object"
            assert params["additionalProperties"] is False
