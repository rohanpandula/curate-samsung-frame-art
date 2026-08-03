#!/usr/bin/env python3
"""Deterministically render art-directed photographs for a Samsung Frame panel.

This command performs offline image work only. It never connects to a television or
Home Assistant. Inputs are a verified source catalog and an art-direction manifest;
outputs are 1920x1080 sRGB PNG files, a machine-readable render manifest, and 3x3
current-versus-curated comparison sheets.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import colorsys
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
from typing import Any, Iterable

from PIL import (
    Image,
    ImageCms,
    ImageDraw,
    ImageEnhance,
    ImageFilter,
    ImageFont,
    ImageOps,
    UnidentifiedImageError,
)


# High-resolution stitched panoramas can legitimately exceed Pillow's default
# decompression-bomb threshold. The catalog hash and actual decode still have to pass.
Image.MAX_IMAGE_PIXELS = None

CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 1080
SUPPORTED_TREATMENTS = {
    "float_pano",
    "museum_light",
    "museum_dark",
    "minimal_crop",
    "full_bleed",
    "soft_extension",
}
PALETTE = {
    "gallery_white": "#F5F4EF",
    "warm_archival": "#F1ECE2",
    "cool_gallery_gray": "#EDF0F2",
    "soft_stone": "#E7E4DE",
    "charcoal": "#191B1E",
    "deep_warm_charcoal": "#201C1A",
}
DEFAULT_MARGINS = {
    "float_pano": {"left": 110, "right": 110, "top": 90, "bottom": 90},
    "museum_light": {"left": 48, "right": 48, "top": 48, "bottom": 48},
    "museum_dark": {"left": 48, "right": 48, "top": 48, "bottom": 48},
    "minimal_crop": {"left": 32, "right": 32, "top": 32, "bottom": 32},
    "full_bleed": {"left": 0, "right": 0, "top": 0, "bottom": 0},
    "soft_extension": {"left": 48, "right": 48, "top": 48, "bottom": 48},
}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
EDGE_NAMES = {"left", "right", "top", "bottom"}


class RenderError(RuntimeError):
    """An expected, user-correctable rendering failure."""


@dataclass(frozen=True)
class PreparedRecord:
    catalog: dict[str, Any]
    direction: dict[str, Any]
    output_name: str


def load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise RenderError(f"{label} does not exist or is not a file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RenderError(
            f"{label} is not valid JSON at line {exc.lineno}, column {exc.colno}: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise RenderError(f"{label} must contain one top-level JSON object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _srgb_profile_bytes() -> bytes:
    profile = bytearray(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    )
    # LittleCMS stamps generated profiles with the current second. Normalize the
    # informational ICC creation date so identical pixels produce identical PNGs.
    profile[24:36] = struct.pack(">6H", 2000, 1, 1, 0, 0, 0)
    return bytes(profile)


SRGB_PROFILE = _srgb_profile_bytes()


def open_srgb(path: Path) -> Image.Image:
    """Decode, apply EXIF orientation, and return a detached 8-bit sRGB image."""

    try:
        with Image.open(path) as opened:
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            embedded_profile = opened.info.get("icc_profile")
            if embedded_profile:
                try:
                    source_profile = ImageCms.ImageCmsProfile(BytesIO(embedded_profile))
                    converted = ImageCms.profileToProfile(
                        oriented.convert("RGB"),
                        source_profile,
                        ImageCms.createProfile("sRGB"),
                        outputMode="RGB",
                    )
                    return converted.copy()
                except (ImageCms.PyCMSError, OSError, ValueError):
                    # A malformed embedded profile should not make an otherwise valid
                    # photograph unusable; Pillow's mode conversion is deterministic.
                    pass
            if "A" in oriented.getbands():
                rgba = oriented.convert("RGBA")
                base = Image.new("RGBA", rgba.size, (245, 244, 239, 255))
                return Image.alpha_composite(base, rgba).convert("RGB")
            return oriented.convert("RGB").copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise RenderError(f"Could not decode image {path}: {exc}") from exc


def save_srgb_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(
        path,
        format="PNG",
        icc_profile=SRGB_PROFILE,
        compress_level=6,
        optimize=False,
    )


def require_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RenderError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise RenderError(f"{label} must be finite")
    if minimum is not None and result < minimum:
        raise RenderError(f"{label} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise RenderError(f"{label} must be at most {maximum}")
    return result


def normalize_hex(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HEX_RE.fullmatch(value):
        raise RenderError(f"{label} must be a color in #RRGGBB form")
    return value.upper()


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(value: Iterable[int]) -> str:
    r, g, b = (max(0, min(255, int(round(channel)))) for channel in value)
    return f"#{r:02X}{g:02X}{b:02X}"


def blend_rgb(
    base: tuple[int, int, int], accent: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    return tuple(
        round(base_channel * (1.0 - amount) + accent_channel * amount)
        for base_channel, accent_channel in zip(base, accent)
    )


def desaturate_rgb(
    value: tuple[int, int, int],
    factor: float,
    *,
    min_light: float | None = None,
    max_light: float | None = None,
) -> tuple[int, int, int]:
    r, g, b = (channel / 255.0 for channel in value)
    hue, light, saturation = colorsys.rgb_to_hls(r, g, b)
    saturation *= factor
    if min_light is not None:
        light = max(light, min_light)
    if max_light is not None:
        light = min(light, max_light)
    converted = colorsys.hls_to_rgb(hue, light, saturation)
    return tuple(round(channel * 255) for channel in converted)


def edge_median(image: Image.Image) -> tuple[int, int, int]:
    sample = image.copy()
    sample.thumbnail((256, 256), Image.Resampling.LANCZOS)
    width, height = sample.size
    band = max(1, round(min(width, height) * 0.06))
    pixels = sample.load()
    channels: tuple[list[int], list[int], list[int]] = ([], [], [])
    for y in range(height):
        for x in range(width):
            if x < band or x >= width - band or y < band or y >= height - band:
                pixel = pixels[x, y]
                for index in range(3):
                    channels[index].append(pixel[index])
    if not channels[0]:
        return (128, 128, 128)
    for channel in channels:
        channel.sort()
    middle = len(channels[0]) // 2
    return tuple(channel[middle] for channel in channels)  # type: ignore[return-value]


def relative_luminance(value: tuple[int, int, int]) -> float:
    def linear(channel: int) -> float:
        normalized = channel / 255.0
        return (
            normalized / 12.92
            if normalized <= 0.04045
            else ((normalized + 0.055) / 1.055) ** 2.4
        )

    return (
        0.2126 * linear(value[0])
        + 0.7152 * linear(value[1])
        + 0.0722 * linear(value[2])
    )


def adaptive_matte(image: Image.Image, treatment: str) -> tuple[str, str, str]:
    edge = edge_median(image)
    edge_hex = rgb_to_hex(edge)
    luminance = relative_luminance(edge)
    if treatment == "museum_dark" or (
        treatment in {"float_pano", "soft_extension"} and luminance < 0.18
    ):
        base_name = "charcoal"
        base = hex_to_rgb(PALETTE[base_name])
        matte = desaturate_rgb(blend_rgb(base, edge, 0.20), 0.22, max_light=0.18)
        return rgb_to_hex(matte), edge_hex, f"edge_dark:{base_name}"

    # Warm edge samples get archival paper; blue/cyan samples get cool gray.
    warmth = (edge[0] - edge[2]) / 255.0
    base_name = "warm_archival" if warmth > 0.035 else "cool_gallery_gray"
    base = hex_to_rgb(PALETTE[base_name])
    matte = desaturate_rgb(blend_rgb(base, edge, 0.15), 0.18, min_light=0.86)
    return rgb_to_hex(matte), edge_hex, f"edge_light:{base_name}"


def normalize_focal(value: Any, content_id: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 2:
        raise RenderError(
            f"Art direction {content_id}: focal_point must be [x, y] in normalized 0..1 coordinates"
        )
    return [
        require_number(
            value[0],
            f"Art direction {content_id}: focal_point[0]",
            minimum=0,
            maximum=1,
        ),
        require_number(
            value[1],
            f"Art direction {content_id}: focal_point[1]",
            minimum=0,
            maximum=1,
        ),
    ]


def normalize_margins(value: Any, treatment: str, content_id: str) -> dict[str, int]:
    defaults = DEFAULT_MARGINS[treatment]
    if value is None:
        margins = dict(defaults)
    else:
        if not isinstance(value, dict):
            raise RenderError(f"Art direction {content_id}: margins must be an object")
        margins = {}
        for edge in ("left", "right", "top", "bottom"):
            raw = value.get(edge, defaults[edge])
            number = require_number(
                raw,
                f"Art direction {content_id}: margins.{edge}",
                minimum=0,
                maximum=600,
            )
            if not number.is_integer():
                raise RenderError(
                    f"Art direction {content_id}: margins.{edge} must be a whole pixel"
                )
            margins[edge] = int(number)
    if margins["left"] + margins["right"] >= CANVAS_WIDTH:
        raise RenderError(
            f"Art direction {content_id}: horizontal margins consume the canvas"
        )
    if margins["top"] + margins["bottom"] >= CANVAS_HEIGHT:
        raise RenderError(
            f"Art direction {content_id}: vertical margins consume the canvas"
        )
    if treatment == "full_bleed" and any(margins.values()):
        raise RenderError(
            f"Art direction {content_id}: full_bleed margins must all be zero"
        )
    if treatment == "float_pano" and not (
        90 <= margins["left"] <= 140 and 90 <= margins["right"] <= 140
    ):
        raise RenderError(
            f"Art direction {content_id}: float_pano left/right margins must be 90..140 px"
        )
    if treatment in {"museum_light", "museum_dark"} and min(margins.values()) < 32:
        raise RenderError(
            f"Art direction {content_id}: museum treatments require at least 32 px on every edge"
        )
    if treatment == "minimal_crop" and not all(
        24 <= number <= 40 for number in margins.values()
    ):
        raise RenderError(
            f"Art direction {content_id}: minimal_crop margins must each be 24..40 px"
        )
    if treatment == "soft_extension" and min(margins.values()) < 32:
        raise RenderError(
            f"Art direction {content_id}: soft_extension requires at least 32 px on every edge"
        )
    return margins


def normalize_keyline(value: Any, treatment: str, content_id: str) -> dict[str, Any]:
    dark = treatment == "museum_dark"
    defaults = {
        "enabled": treatment != "full_bleed",
        "color": "#8B9198" if dark else "#B8B4AB",
        "width": 1,
        "opacity": 0.45 if dark else 0.65,
    }
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise RenderError(f"Art direction {content_id}: keyline must be an object")
    enabled = value.get("enabled", defaults["enabled"])
    if not isinstance(enabled, bool):
        raise RenderError(
            f"Art direction {content_id}: keyline.enabled must be true or false"
        )
    width = require_number(
        value.get("width", defaults["width"]),
        f"Art direction {content_id}: keyline.width",
        minimum=0,
        maximum=4,
    )
    if not width.is_integer():
        raise RenderError(
            f"Art direction {content_id}: keyline.width must be a whole pixel"
        )
    return {
        "enabled": enabled,
        "color": normalize_hex(
            value.get("color", defaults["color"]),
            f"Art direction {content_id}: keyline.color",
        ),
        "width": int(width),
        "opacity": require_number(
            value.get("opacity", defaults["opacity"]),
            f"Art direction {content_id}: keyline.opacity",
            minimum=0,
            maximum=1,
        ),
    }


def normalize_shadow(value: Any, treatment: str, content_id: str) -> dict[str, Any]:
    default_enabled = treatment in {
        "float_pano",
        "museum_light",
        "museum_dark",
        "soft_extension",
    }
    default_opacity = 0.08 if treatment == "museum_dark" else 0.14
    defaults = {
        "enabled": default_enabled,
        "opacity": default_opacity,
        "blur": 14,
        "offset_x": 0,
        "offset_y": 8,
    }
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise RenderError(f"Art direction {content_id}: shadow must be an object")
    enabled = value.get("enabled", defaults["enabled"])
    if not isinstance(enabled, bool):
        raise RenderError(
            f"Art direction {content_id}: shadow.enabled must be true or false"
        )
    normalized: dict[str, Any] = {
        "enabled": enabled,
        "opacity": require_number(
            value.get("opacity", defaults["opacity"]),
            f"Art direction {content_id}: shadow.opacity",
            minimum=0,
            maximum=0.35,
        ),
    }
    for key, minimum, maximum in (
        ("blur", 0, 64),
        ("offset_x", -64, 64),
        ("offset_y", -64, 64),
    ):
        number = require_number(
            value.get(key, defaults[key]),
            f"Art direction {content_id}: shadow.{key}",
            minimum=minimum,
            maximum=maximum,
        )
        if not number.is_integer():
            raise RenderError(
                f"Art direction {content_id}: shadow.{key} must be a whole pixel"
            )
        normalized[key] = int(number)
    return normalized


def normalize_direction(record: dict[str, Any]) -> dict[str, Any]:
    content_id, identity_field = preferred_identity(
        record, "Every art-direction record"
    )
    treatment = str(record.get("treatment", "")).strip()
    if treatment not in SUPPORTED_TREATMENTS:
        raise RenderError(
            f"Art direction {content_id}: unsupported treatment {treatment!r}; "
            f"choose one of {', '.join(sorted(SUPPORTED_TREATMENTS))}"
        )
    focal = normalize_focal(record.get("focal_point"), content_id)
    protected = record.get("protected_edges", [])
    if not isinstance(protected, list):
        raise RenderError(
            f"Art direction {content_id}: protected_edges must be a list containing only "
            "left, right, top, or bottom"
        )
    invalid_edges = [value for value in protected if value not in EDGE_NAMES]
    if invalid_edges:
        raise RenderError(
            f"Art direction {content_id}: protected_edges contains non-canonical values "
            f"{invalid_edges!r}; semantic labels belong in protected_subjects"
        )
    protected_subjects = record.get("protected_subjects", [])
    if not isinstance(protected_subjects, list) or not all(
        isinstance(value, str) and value.strip() for value in protected_subjects
    ):
        raise RenderError(
            f"Art direction {content_id}: protected_subjects must be a list of non-empty strings"
        )
    if "max_crop_fraction" not in record:
        raise RenderError(
            f"Art direction {content_id}: max_crop_fraction is required for a reviewable crop ceiling"
        )
    max_crop = require_number(
        record["max_crop_fraction"],
        f"Art direction {content_id}: max_crop_fraction",
        minimum=0,
        maximum=0.05,
    )
    if treatment == "full_bleed" and max_crop > 0.02:
        raise RenderError(
            f"Art direction {content_id}: full_bleed max_crop_fraction cannot exceed 0.02"
        )
    if (
        treatment in {"float_pano", "museum_light", "museum_dark", "soft_extension"}
        and max_crop != 0
    ):
        raise RenderError(
            f"Art direction {content_id}: {treatment} preserves the whole photo, so max_crop_fraction must be 0"
        )
    matte_strategy = str(record.get("matte_strategy", "adaptive")).strip().lower()
    if matte_strategy not in {"adaptive", "fixed"}:
        raise RenderError(
            f"Art direction {content_id}: matte_strategy must be 'adaptive' or 'fixed'"
        )
    matte_hex = record.get("matte_hex")
    if matte_strategy == "fixed" and treatment != "full_bleed" and matte_hex is None:
        raise RenderError(
            f"Art direction {content_id}: fixed matte_strategy requires matte_hex"
        )
    if matte_hex is not None:
        matte_hex = normalize_hex(matte_hex, f"Art direction {content_id}: matte_hex")
    vertical_default = -0.02 if treatment == "float_pano" else 0.0
    vertical_bias = require_number(
        record.get("vertical_bias", vertical_default),
        f"Art direction {content_id}: vertical_bias",
        minimum=-0.25,
        maximum=0.25,
    )
    allow_upscale = record.get("allow_upscale", False)
    if not isinstance(allow_upscale, bool):
        raise RenderError(
            f"Art direction {content_id}: allow_upscale must be true or false"
        )
    return {
        "record_id": content_id,
        "identity_field": identity_field,
        "treatment": treatment,
        "rationale": str(record.get("rationale", "")).strip(),
        "focal_point": focal,
        "protected_edges": list(protected),
        "protected_subjects": list(protected_subjects),
        "max_crop_fraction": max_crop,
        "matte_strategy": matte_strategy,
        "matte_hex": matte_hex,
        "margins": normalize_margins(record.get("margins"), treatment, content_id),
        "vertical_bias": vertical_bias,
        "allow_upscale": allow_upscale,
        "keyline": normalize_keyline(record.get("keyline"), treatment, content_id),
        "shadow": normalize_shadow(record.get("shadow"), treatment, content_id),
        "soft_extension": normalize_soft_extension(
            record.get("soft_extension"), content_id
        ),
    }


def normalize_soft_extension(value: Any, content_id: str) -> dict[str, Any]:
    defaults = {"blur": 36, "saturation": 0.28, "brightness": 0.58}
    if value is None:
        return defaults
    if not isinstance(value, dict):
        raise RenderError(
            f"Art direction {content_id}: soft_extension must be an object"
        )
    blur = require_number(
        value.get("blur", defaults["blur"]),
        f"Art direction {content_id}: soft_extension.blur",
        minimum=8,
        maximum=96,
    )
    if not blur.is_integer():
        raise RenderError(
            f"Art direction {content_id}: soft_extension.blur must be a whole pixel"
        )
    return {
        "blur": int(blur),
        "saturation": require_number(
            value.get("saturation", defaults["saturation"]),
            f"Art direction {content_id}: soft_extension.saturation",
            minimum=0,
            maximum=0.65,
        ),
        "brightness": require_number(
            value.get("brightness", defaults["brightness"]),
            f"Art direction {content_id}: soft_extension.brightness",
            minimum=0.25,
            maximum=0.85,
        ),
    }


def identity_fields(record: dict[str, Any]) -> dict[str, str]:
    """Return the non-empty supported identities without inventing either one."""

    result: dict[str, str] = {}
    for key in ("content_id", "asset_id"):
        value = record.get(key)
        if value is not None and str(value).strip():
            result[key] = str(value).strip()
    return result


def preferred_identity(record: dict[str, Any], label: str) -> tuple[str, str]:
    identities = identity_fields(record)
    if "content_id" in identities:
        return identities["content_id"], "content_id"
    if "asset_id" in identities:
        return identities["asset_id"], "asset_id"
    raise RenderError(f"{label} needs a non-empty content_id or asset_id")


def safe_slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (slug or fallback)[:80]


def prepare_records(
    catalog: dict[str, Any], art_direction: dict[str, Any]
) -> list[PreparedRecord]:
    acknowledge_upscale_risk = art_direction.get("acknowledge_upscale_risk", False)
    if not isinstance(acknowledge_upscale_risk, bool):
        raise RenderError(
            "art-direction acknowledge_upscale_risk must be true or false"
        )
    canvas = art_direction.get("canvas")
    if canvas is not None:
        if not isinstance(canvas, dict):
            raise RenderError("art-direction canvas must be an object")
        if canvas.get("width") != CANVAS_WIDTH or canvas.get("height") != CANVAS_HEIGHT:
            raise RenderError(
                f"art-direction canvas must be exactly {CANVAS_WIDTH}x{CANVAS_HEIGHT}"
            )
    catalog_records = catalog.get("records")
    if not isinstance(catalog_records, list) or not catalog_records:
        raise RenderError("catalog.records must be a non-empty list")
    by_id: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(catalog_records, start=1):
        if not isinstance(record, dict):
            raise RenderError(f"catalog record {index} must be an object")
        identities = identity_fields(record)
        if not identities:
            raise RenderError(
                f"catalog record {index} needs a non-empty content_id or asset_id"
            )
        for field, identity in identities.items():
            if identity in by_id and by_id[identity] is not record:
                raise RenderError(
                    f"catalog identity {identity!r} is ambiguous across records ({field})"
                )
            by_id[identity] = record

    direction_records = art_direction.get("records")
    if not isinstance(direction_records, list) or not direction_records:
        raise RenderError("art-direction records must be a non-empty list")
    result: list[PreparedRecord] = []
    seen: set[str] = set()
    output_names: set[str] = set()
    for index, raw in enumerate(direction_records, start=1):
        if not isinstance(raw, dict):
            raise RenderError(f"art-direction record {index} must be an object")
        normalized = normalize_direction(raw)
        direction_identities = identity_fields(raw)
        matched_records: list[dict[str, Any]] = []
        for field, identity in direction_identities.items():
            match = by_id.get(identity)
            if match is None:
                raise RenderError(
                    f"art-direction {field} is absent from catalog: {identity}"
                )
            matched_records.append(match)
        if any(record is not matched_records[0] for record in matched_records[1:]):
            raise RenderError(
                "art-direction content_id and asset_id resolve to different catalog records"
            )
        catalog_record = matched_records[0]
        record_id, identity_field = preferred_identity(
            catalog_record, f"catalog record {index}"
        )
        if record_id in seen:
            raise RenderError(
                f"art-direction has duplicate record identity {record_id}"
            )
        seen.add(record_id)
        normalized["record_id"] = record_id
        normalized["identity_field"] = identity_field
        normalized["acknowledge_upscale_risk"] = acknowledge_upscale_risk
        source_name = str(catalog_record.get("source_name", record_id))
        stem = safe_slug(Path(source_name).stem, "photo")
        position = catalog_record.get("position", index)
        try:
            position_number = int(position)
        except (TypeError, ValueError) as exc:
            raise RenderError(
                f"catalog {record_id}: position must be an integer"
            ) from exc
        output_name = (
            f"{position_number:03d}-{safe_slug(record_id, 'record')}-{stem}.png"
        )
        if output_name in output_names:
            raise RenderError(f"rendered filename collision: {output_name}")
        output_names.add(output_name)
        result.append(PreparedRecord(catalog_record, normalized, output_name))
    return result


def verify_catalog_file(
    record: dict[str, Any], path_key: str, hash_key: str, catalog_base: Path
) -> tuple[Path, str]:
    record_id, _ = preferred_identity(record, "catalog record")
    raw_path = str(record.get(path_key, "")).strip()
    expected = str(record.get(hash_key, "")).strip().lower()
    if not raw_path or not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RenderError(
            f"catalog {record_id}: {path_key} and {hash_key} are required"
        )
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = catalog_base / path
    path = path.resolve(strict=False)
    if not path.is_file():
        raise RenderError(f"catalog {record_id}: file is missing: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise RenderError(
            f"catalog {record_id}: hash mismatch for {path}; expected {expected}, got {actual}"
        )
    return path, actual


def verify_optional_catalog_file(
    record: dict[str, Any], path_key: str, hash_key: str, catalog_base: Path
) -> tuple[Path | None, str | None]:
    record_id, _ = preferred_identity(record, "catalog record")
    has_path = bool(str(record.get(path_key, "")).strip())
    has_hash = bool(str(record.get(hash_key, "")).strip())
    if has_path != has_hash:
        raise RenderError(
            f"catalog {record_id}: optional {path_key} and {hash_key} must appear together"
        )
    if not has_path:
        return None, None
    path, digest = verify_catalog_file(record, path_key, hash_key, catalog_base)
    return path, digest


def expected_source_dimensions(record: dict[str, Any]) -> tuple[int, int]:
    record_id, _ = preferred_identity(record, "catalog record")
    candidates: list[tuple[str, Any, Any]] = []
    if "source_width" in record or "source_height" in record:
        candidates.append(
            (
                "source_width/source_height",
                record.get("source_width"),
                record.get("source_height"),
            )
        )
    if "width" in record or "height" in record:
        candidates.append(("width/height", record.get("width"), record.get("height")))
    if not candidates:
        raise RenderError(
            f"catalog {record_id}: source dimensions require source_width/source_height or width/height"
        )
    normalized: list[tuple[int, int]] = []
    for label, raw_width, raw_height in candidates:
        width = require_number(
            raw_width, f"catalog {record_id}: {label} width", minimum=1
        )
        height = require_number(
            raw_height, f"catalog {record_id}: {label} height", minimum=1
        )
        if not width.is_integer() or not height.is_integer():
            raise RenderError(f"catalog {record_id}: {label} must contain whole pixels")
        normalized.append((int(width), int(height)))
    if any(value != normalized[0] for value in normalized[1:]):
        raise RenderError(
            f"catalog {record_id}: declared source dimension pairs disagree"
        )
    return normalized[0]


def contain_rect(
    source_size: tuple[int, int], margins: dict[str, int], vertical_bias: float
) -> tuple[dict[str, int], tuple[int, int], dict[str, float]]:
    source_width, source_height = source_size
    available_width = CANVAS_WIDTH - margins["left"] - margins["right"]
    available_height = CANVAS_HEIGHT - margins["top"] - margins["bottom"]
    scale = min(available_width / source_width, available_height / source_height)
    target_width = max(1, min(available_width, round(source_width * scale)))
    target_height = max(1, min(available_height, round(source_height * scale)))
    x = margins["left"] + (available_width - target_width) // 2
    centered_y = margins["top"] + (available_height - target_height) // 2
    y = centered_y + round(vertical_bias * CANVAS_HEIGHT)
    y = max(margins["top"], min(y, CANVAS_HEIGHT - margins["bottom"] - target_height))
    return (
        {"x": x, "y": y, "width": target_width, "height": target_height},
        (target_width, target_height),
        {"x": target_width / source_width, "y": target_height / source_height},
    )


def crop_box_for_aspect(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    focal_point: list[float],
    protected_edges: list[str],
    content_id: str,
) -> tuple[tuple[int, int, int, int], float]:
    source_width, source_height = source_size
    target_width, target_height = target_size
    target_aspect = target_width / target_height
    source_aspect = source_width / source_height
    if source_aspect >= target_aspect:
        crop_height = source_height
        crop_width = min(source_width, max(1, round(crop_height * target_aspect)))
    else:
        crop_width = source_width
        crop_height = min(source_height, max(1, round(crop_width / target_aspect)))

    max_x = source_width - crop_width
    max_y = source_height - crop_height
    x = round(focal_point[0] * source_width - crop_width / 2)
    y = round(focal_point[1] * source_height - crop_height / 2)
    x = max(0, min(max_x, x))
    y = max(0, min(max_y, y))
    protected = set(protected_edges) & EDGE_NAMES
    if max_x:
        if {"left", "right"} <= protected:
            raise RenderError(
                f"Art direction {content_id}: crop would violate protected left and right edges"
            )
        if "left" in protected:
            x = 0
        elif "right" in protected:
            x = max_x
    if max_y:
        if {"top", "bottom"} <= protected:
            raise RenderError(
                f"Art direction {content_id}: crop would violate protected top and bottom edges"
            )
        if "top" in protected:
            y = 0
        elif "bottom" in protected:
            y = max_y

    focal_x = focal_point[0] * source_width
    focal_y = focal_point[1] * source_height
    if not (x <= focal_x <= x + crop_width and y <= focal_y <= y + crop_height):
        raise RenderError(
            f"Art direction {content_id}: protected edges and focal_point cannot both survive the crop"
        )
    fraction = 1.0 - (crop_width * crop_height) / (source_width * source_height)
    return (x, y, x + crop_width, y + crop_height), max(0.0, fraction)


def apply_shadow(
    canvas: Image.Image, rect: dict[str, int], settings: dict[str, Any]
) -> Image.Image:
    if not settings["enabled"] or settings["opacity"] <= 0:
        return canvas
    mask = Image.new("L", canvas.size, 0)
    draw = ImageDraw.Draw(mask)
    x = rect["x"] + settings["offset_x"]
    y = rect["y"] + settings["offset_y"]
    draw.rectangle(
        (x, y, x + rect["width"] - 1, y + rect["height"] - 1),
        fill=round(255 * settings["opacity"]),
    )
    if settings["blur"]:
        mask = mask.filter(ImageFilter.GaussianBlur(settings["blur"]))
    shadow = Image.new("RGB", canvas.size, (0, 0, 0))
    return Image.composite(shadow, canvas, mask)


def apply_keyline(
    canvas: Image.Image, rect: dict[str, int], settings: dict[str, Any]
) -> Image.Image:
    if not settings["enabled"] or settings["width"] <= 0 or settings["opacity"] <= 0:
        return canvas
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    color = (*hex_to_rgb(settings["color"]), round(255 * settings["opacity"]))
    draw.rectangle(
        (
            rect["x"],
            rect["y"],
            rect["x"] + rect["width"] - 1,
            rect["y"] + rect["height"] - 1,
        ),
        outline=color,
        width=settings["width"],
    )
    return Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")


def render_one(
    source: Image.Image, direction: dict[str, Any]
) -> tuple[Image.Image, dict[str, Any]]:
    content_id = direction["record_id"]
    treatment = direction["treatment"]
    margins = direction["margins"]
    if direction["matte_strategy"] == "fixed" and direction["matte_hex"]:
        matte_hex = direction["matte_hex"]
        edge_hex = rgb_to_hex(edge_median(source))
        matte_basis = "fixed"
    else:
        matte_hex, edge_hex, matte_basis = adaptive_matte(source, treatment)

    crop_box = (0, 0, source.width, source.height)
    crop_fraction = 0.0
    background: dict[str, Any] = {"kind": "solid_matte"}

    if treatment == "full_bleed":
        crop_box, crop_fraction = crop_box_for_aspect(
            source.size,
            (CANVAS_WIDTH, CANVAS_HEIGHT),
            direction["focal_point"],
            direction["protected_edges"],
            content_id,
        )
        if crop_fraction > direction["max_crop_fraction"] + 1e-9:
            raise RenderError(
                f"Art direction {content_id}: full_bleed needs {crop_fraction:.4%} crop, "
                f"above max_crop_fraction {direction['max_crop_fraction']:.4%}"
            )
        foreground = source.crop(crop_box).resize(
            (CANVAS_WIDTH, CANVAS_HEIGHT), Image.Resampling.LANCZOS
        )
        canvas = foreground
        rect = {"x": 0, "y": 0, "width": CANVAS_WIDTH, "height": CANVAS_HEIGHT}
        scale = {
            "x": CANVAS_WIDTH / (crop_box[2] - crop_box[0]),
            "y": CANVAS_HEIGHT / (crop_box[3] - crop_box[1]),
        }
        background = {"kind": "photo_full_bleed"}
    elif treatment == "minimal_crop":
        inner = (
            CANVAS_WIDTH - margins["left"] - margins["right"],
            CANVAS_HEIGHT - margins["top"] - margins["bottom"],
        )
        crop_box, crop_fraction = crop_box_for_aspect(
            source.size,
            inner,
            direction["focal_point"],
            direction["protected_edges"],
            content_id,
        )
        if crop_fraction > direction["max_crop_fraction"] + 1e-9:
            raise RenderError(
                f"Art direction {content_id}: minimal_crop needs {crop_fraction:.4%} crop, "
                f"above max_crop_fraction {direction['max_crop_fraction']:.4%}"
            )
        foreground = source.crop(crop_box).resize(inner, Image.Resampling.LANCZOS)
        rect = {
            "x": margins["left"],
            "y": margins["top"] + round(direction["vertical_bias"] * CANVAS_HEIGHT),
            "width": inner[0],
            "height": inner[1],
        }
        rect["y"] = max(0, min(CANVAS_HEIGHT - rect["height"], rect["y"]))
        canvas = Image.new("RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), hex_to_rgb(matte_hex))
        canvas = apply_shadow(canvas, rect, direction["shadow"])
        canvas.paste(foreground, (rect["x"], rect["y"]))
        canvas = apply_keyline(canvas, rect, direction["keyline"])
        scale = {
            "x": inner[0] / (crop_box[2] - crop_box[0]),
            "y": inner[1] / (crop_box[3] - crop_box[1]),
        }
    else:
        rect, target_size, scale = contain_rect(
            source.size, margins, direction["vertical_bias"]
        )
        foreground = source.resize(target_size, Image.Resampling.LANCZOS)
        if treatment == "soft_extension":
            extension_box, _ = crop_box_for_aspect(
                source.size,
                (CANVAS_WIDTH, CANVAS_HEIGHT),
                direction["focal_point"],
                [],
                content_id,
            )
            extension = source.crop(extension_box).resize(
                (CANVAS_WIDTH, CANVAS_HEIGHT), Image.Resampling.LANCZOS
            )
            soft = direction["soft_extension"]
            extension = ImageEnhance.Color(extension).enhance(soft["saturation"])
            extension = ImageEnhance.Brightness(extension).enhance(soft["brightness"])
            canvas = extension.filter(ImageFilter.GaussianBlur(soft["blur"]))
            background = {
                "kind": "blurred_source_extension",
                "crop_box": {
                    "x": extension_box[0],
                    "y": extension_box[1],
                    "width": extension_box[2] - extension_box[0],
                    "height": extension_box[3] - extension_box[1],
                },
                **soft,
            }
        else:
            canvas = Image.new(
                "RGB", (CANVAS_WIDTH, CANVAS_HEIGHT), hex_to_rgb(matte_hex)
            )
        canvas = apply_shadow(canvas, rect, direction["shadow"])
        canvas.paste(foreground, (rect["x"], rect["y"]))
        canvas = apply_keyline(canvas, rect, direction["keyline"])

    if canvas.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise RenderError(
            f"Internal error for {content_id}: renderer produced {canvas.size}"
        )
    upscale_factor = max(
        rect["width"] / source.width,
        rect["height"] / source.height,
    )
    upscaled = upscale_factor > 1.0000001
    if upscaled and not (
        direction["allow_upscale"] and direction["acknowledge_upscale_risk"]
    ):
        missing_gates: list[str] = []
        if not direction["allow_upscale"]:
            missing_gates.append("record allow_upscale=true")
        if not direction["acknowledge_upscale_risk"]:
            missing_gates.append("top-level acknowledge_upscale_risk=true")
        raise RenderError(
            f"Art direction {content_id}: sharp image rectangle {rect['width']}x{rect['height']} "
            f"would enlarge decoded source {source.width}x{source.height} by "
            f"{upscale_factor:.4f}x; enlargement requires both explicit gates; missing "
            + " and ".join(missing_gates)
        )
    metrics = {
        "crop_fraction": round(crop_fraction, 8),
        "crop_box": {
            "x": crop_box[0],
            "y": crop_box[1],
            "width": crop_box[2] - crop_box[0],
            "height": crop_box[3] - crop_box[1],
        },
        "image_rect": rect,
        "scale": {"x": round(scale["x"], 8), "y": round(scale["y"], 8)},
        "upscaled": upscaled,
        "upscale_factor": round(upscale_factor, 8),
        "matte_hex": matte_hex,
        "edge_sample_hex": edge_hex,
        "matte_basis": matte_basis,
        "background": background,
    }
    return canvas.convert("RGB"), metrics


def fit_thumbnail(
    image: Image.Image, size: tuple[int, int], background: str
) -> Image.Image:
    thumbnail = image.copy()
    thumbnail.thumbnail(size, Image.Resampling.LANCZOS)
    panel = Image.new("RGB", size, hex_to_rgb(background))
    x = (size[0] - thumbnail.width) // 2
    y = (size[1] - thumbnail.height) // 2
    panel.paste(thumbnail, (x, y))
    return panel


def load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    names = (
        ["DejaVuSans-Bold.ttf", "Arial Bold.ttf"]
        if bold
        else ["DejaVuSans.ttf", "Arial.ttf"]
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def ellipsize(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int
) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= width:
        return text
    suffix = "…"
    while text and draw.textbbox((0, 0), text + suffix, font=font)[2] > width:
        text = text[:-1]
    return text + suffix


def comparison_sheet(rows: list[dict[str, Any]], destination: Path) -> tuple[int, int]:
    cell_width, cell_height = 640, 420
    sheet = Image.new("RGB", (cell_width * 3, cell_height * 3), (230, 228, 222))
    draw = ImageDraw.Draw(sheet)
    title_font = load_font(18, bold=True)
    meta_font = load_font(14)
    small_font = load_font(12, bold=True)
    thumb_size = (292, 164)
    for index, row in enumerate(rows):
        column, line = index % 3, index // 3
        origin_x, origin_y = column * cell_width, line * cell_height
        draw.rectangle(
            (
                origin_x + 8,
                origin_y + 8,
                origin_x + cell_width - 8,
                origin_y + cell_height - 8,
            ),
            fill=(250, 249, 246),
            outline=(194, 191, 184),
            width=1,
        )
        label = f"{row['record_id']}  ·  {row['source_name']}"
        label = ellipsize(draw, label, title_font, cell_width - 40)
        draw.text(
            (origin_x + 20, origin_y + 22), label, font=title_font, fill=(34, 34, 32)
        )
        treatment = ellipsize(draw, row["treatment"], meta_font, cell_width - 40)
        draw.text(
            (origin_x + 20, origin_y + 50), treatment, font=meta_font, fill=(87, 84, 78)
        )
        left_x, right_x = origin_x + 20, origin_x + 328
        top = origin_y + 92
        baseline = open_srgb(Path(row["comparison_baseline_path"]))
        curated = open_srgb(Path(row["temporary_output_path"]))
        sheet.paste(fit_thumbnail(baseline, thumb_size, "#17191B"), (left_x, top))
        sheet.paste(fit_thumbnail(curated, thumb_size, "#17191B"), (right_x, top))
        draw.text(
            (left_x, top - 20),
            row["comparison_baseline_label"],
            font=small_font,
            fill=(90, 87, 81),
        )
        draw.text((right_x, top - 20), "CURATED", font=small_font, fill=(90, 87, 81))
        details = (
            f"crop {row['crop_fraction']:.2%}  ·  matte {row['matte_hex']}  ·  "
            f"{row['image_rect']['width']}×{row['image_rect']['height']}"
        )
        details = ellipsize(draw, details, meta_font, cell_width - 40)
        draw.text(
            (origin_x + 20, origin_y + 276), details, font=meta_font, fill=(68, 66, 61)
        )
        rationale = row.get("rationale") or "No rationale recorded"
        rationale = ellipsize(draw, rationale, meta_font, cell_width - 40)
        draw.text(
            (origin_x + 20, origin_y + 304),
            rationale,
            font=meta_font,
            fill=(98, 94, 87),
        )
        draw.line(
            (origin_x + 20, origin_y + 340, origin_x + cell_width - 20, origin_y + 340),
            fill=(220, 217, 210),
        )
    save_srgb_png(sheet, destination)
    return sheet.size


def preflight_outputs(
    output_dir: Path,
    prepared: list[PreparedRecord],
    sheet_count: int,
    overwrite: bool,
) -> None:
    intended_render_names = {record.output_name for record in prepared}
    intended_sheet_names = {
        f"comparison-{index:03d}.png" for index in range(1, sheet_count + 1)
    }
    intended_paths = [output_dir / "render-manifest.json"]
    intended_paths.extend(
        output_dir / "renders" / name for name in intended_render_names
    )
    intended_paths.extend(
        output_dir / "contact-sheets" / name for name in intended_sheet_names
    )
    existing = [path for path in intended_paths if path.exists()]
    if existing and not overwrite:
        display = "\n  ".join(str(path) for path in existing[:12])
        extra = f"\n  … and {len(existing) - 12} more" if len(existing) > 12 else ""
        raise RenderError(
            "Refusing to overwrite existing output. Choose a new --output-dir or pass --overwrite:\n  "
            f"{display}{extra}"
        )
    if overwrite:
        for directory, intended, label in (
            (output_dir / "renders", intended_render_names, "render"),
            (output_dir / "contact-sheets", intended_sheet_names, "contact-sheet"),
        ):
            if directory.is_dir():
                extras = sorted(
                    path.name
                    for path in directory.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in {".png", ".jpg", ".jpeg"}
                    and path.name not in intended
                )
                if extras:
                    raise RenderError(
                        f"Refusing --overwrite because {directory} contains undeclared {label} images: "
                        + ", ".join(extras[:8])
                        + (" …" if len(extras) > 8 else "")
                    )


def commit_tree(temporary_root: Path, output_dir: Path, overwrite: bool) -> None:
    def publication_order(path: Path) -> tuple[int, str]:
        relative = path.relative_to(temporary_root)
        if path.name == "render-manifest.json":
            return 2, str(relative)
        if relative.parts and relative.parts[0] == "contact-sheets":
            return 1, str(relative)
        return 0, str(relative)

    files = sorted(
        (path for path in temporary_root.rglob("*") if path.is_file()),
        key=publication_order,
    )
    for source in files:
        relative = source.relative_to(temporary_root)
        destination = output_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if overwrite:
            os.replace(source, destination)
            continue
        try:
            # The temporary tree is created beside output_dir, so this hard-link
            # publication is same-filesystem and atomically fails if another process
            # won the target name. It cannot silently clobber a concurrent render.
            os.link(source, destination)
        except FileExistsError as exc:
            raise RenderError(
                f"Concurrent publication collision; refusing to overwrite {destination}"
            ) from exc
        except OSError as exc:
            raise RenderError(
                f"Could not atomically publish {destination} without overwrite: {exc}"
            ) from exc
        source.unlink()


def run(args: argparse.Namespace) -> int:
    catalog_path = args.catalog.expanduser().resolve()
    art_path = args.art_direction.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    catalog = load_json(catalog_path, "catalog")
    art_direction = load_json(art_path, "art-direction manifest")
    prepared = prepare_records(catalog, art_direction)
    sheet_count = math.ceil(len(prepared) / 9)
    preflight_outputs(output_dir, prepared, sheet_count, args.overwrite)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_parent = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.render-", dir=output_dir.parent)
    )
    try:
        temporary_renders = temporary_parent / "renders"
        temporary_sheets = temporary_parent / "contact-sheets"
        final_renders = output_dir / "renders"
        final_sheets = output_dir / "contact-sheets"
        manifest_records: list[dict[str, Any]] = []
        contact_rows: list[dict[str, Any]] = []
        for batch_position, prepared_record in enumerate(prepared, start=1):
            catalog_record = prepared_record.catalog
            direction = prepared_record.direction
            record_id = direction["record_id"]
            source_path, source_hash = verify_catalog_file(
                catalog_record, "source_path", "source_sha256", catalog_path.parent
            )
            current_path, current_hash = verify_optional_catalog_file(
                catalog_record,
                "current_payload_path",
                "current_payload_sha256",
                catalog_path.parent,
            )
            source = open_srgb(source_path)
            declared_dimensions = expected_source_dimensions(catalog_record)
            if source.size != declared_dimensions:
                raise RenderError(
                    f"catalog {record_id}: decoded source dimensions {source.width}x{source.height} "
                    f"do not match declared {declared_dimensions[0]}x{declared_dimensions[1]}"
                )
            rendered, metrics = render_one(source, direction)
            temporary_output = temporary_renders / prepared_record.output_name
            save_srgb_png(rendered, temporary_output)
            output_hash = sha256_file(temporary_output)
            final_output = final_renders / prepared_record.output_name
            record = {
                "batch_position": batch_position,
                "catalog_position": catalog_record.get("position"),
                "record_id": record_id,
                "identity_field": direction["identity_field"],
                "source_name": str(catalog_record.get("source_name", source_path.name)),
                "treatment": direction["treatment"],
                "rationale": direction["rationale"],
                "source_path": str(source_path),
                "source_sha256": source_hash,
                "source_dimensions": {"width": source.width, "height": source.height},
                "comparison_baseline": (
                    "current_payload" if current_path is not None else "source"
                ),
                "output_path": str(final_output),
                "output_sha256": output_hash,
                "output_dimensions": {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT},
                "color_space": "sRGB",
                "focal_point": direction["focal_point"],
                "protected_edges": direction["protected_edges"],
                "protected_subjects": direction["protected_subjects"],
                "crop_fraction": metrics["crop_fraction"],
                "max_crop_fraction": direction["max_crop_fraction"],
                "crop_box": metrics["crop_box"],
                "image_rect": metrics["image_rect"],
                "scale": metrics["scale"],
                "upscaled": metrics["upscaled"],
                "upscale_factor": metrics["upscale_factor"],
                "allow_upscale": direction["allow_upscale"],
                "acknowledge_upscale_risk": direction["acknowledge_upscale_risk"],
                "matte_strategy": direction["matte_strategy"],
                "matte_hex": metrics["matte_hex"],
                "edge_sample_hex": metrics["edge_sample_hex"],
                "matte_basis": metrics["matte_basis"],
                "margins": direction["margins"],
                "vertical_bias": direction["vertical_bias"],
                "keyline": direction["keyline"],
                "shadow": direction["shadow"],
                "background": metrics["background"],
            }
            record.update(identity_fields(catalog_record))
            if current_path is not None and current_hash is not None:
                record["current_payload_path"] = str(current_path)
                record["current_payload_sha256"] = current_hash
            manifest_records.append(record)
            contact_rows.append(
                {
                    **record,
                    "comparison_baseline_path": str(current_path or source_path),
                    "comparison_baseline_label": (
                        "CURRENT" if current_path is not None else "NEW SOURCE"
                    ),
                    "temporary_output_path": str(temporary_output),
                }
            )

        contact_sheets: list[dict[str, Any]] = []
        for index in range(sheet_count):
            rows = contact_rows[index * 9 : (index + 1) * 9]
            name = f"comparison-{index + 1:03d}.png"
            temporary_sheet = temporary_sheets / name
            width, height = comparison_sheet(rows, temporary_sheet)
            sheet_record: dict[str, Any] = {
                "path": str(final_sheets / name),
                "sha256": sha256_file(temporary_sheet),
                "width": width,
                "height": height,
                "record_count": len(rows),
                "record_ids": [row["record_id"] for row in rows],
            }
            content_ids = [row["content_id"] for row in rows if "content_id" in row]
            asset_ids = [row["asset_id"] for row in rows if "asset_id" in row]
            if content_ids:
                sheet_record["content_ids"] = content_ids
            if asset_ids:
                sheet_record["asset_ids"] = asset_ids
            contact_sheets.append(sheet_record)

        manifest = {
            "schema_version": "1.0",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "complete",
            "catalog_path": str(catalog_path),
            "art_direction_path": str(art_path),
            "output_dir": str(output_dir),
            "rendered_dir": str(final_renders),
            "canvas": {
                "width": CANVAS_WIDTH,
                "height": CANVAS_HEIGHT,
                "color_space": "sRGB",
                "format": "PNG",
            },
            "render_count": len(manifest_records),
            "acknowledge_upscale_risk": art_direction.get(
                "acknowledge_upscale_risk", False
            ),
            "records": manifest_records,
            "comparison_contact_sheets": contact_sheets,
        }
        atomic_json(temporary_parent / "render-manifest.json", manifest)

        commit_tree(temporary_parent, output_dir, args.overwrite)
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)

    print(
        f"COMPLETE rendered {len(prepared)} photos and {sheet_count} comparison sheet(s) "
        f"into {output_dir}",
        flush=True,
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render a verified Samsung Frame photo catalog from an art-direction manifest. "
            "This command is offline and never contacts the TV or Home Assistant."
        )
    )
    parser.add_argument(
        "--catalog", type=Path, required=True, help="verified source catalog JSON"
    )
    parser.add_argument(
        "--art-direction", type=Path, required=True, help="per-photo art-direction JSON"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="destination containing renders/, contact-sheets/, and render-manifest.json",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace the exact declared outputs; undeclared images still cause a safe failure",
    )
    return parser.parse_args()


def main() -> int:
    try:
        return run(parse_args())
    except (RenderError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
