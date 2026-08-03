#!/usr/bin/env python3
"""Render two verified square photographs as one Samsung Frame panel."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import struct
import tempfile
from typing import Any

from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageFont, ImageOps


Image.MAX_IMAGE_PIXELS = 250_000_000
CANVAS = (1920, 1080)
MIN_OUTER_MATTE = 64
MIN_GUTTER = 32
SAFE_ASSET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


def canonical_srgb_profile() -> bytes:
    """Return an sRGB profile with stable header metadata."""
    profile = bytearray(
        ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    )
    if len(profile) < 100:
        raise RuntimeError("Generated sRGB ICC profile has an invalid header")
    profile[24:36] = struct.pack(">6H", 2000, 1, 1, 0, 0, 0)
    profile[84:100] = bytes(16)
    return bytes(profile)


SRGB_PROFILE = canonical_srgb_profile()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render verified square pairs as shared-matte 1920x1080 diptychs."
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--art-direction", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


def atomic_save(path: Path, value: dict[str, Any], overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        publish(temporary, path, overwrite)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def publish(temporary: str, target: Path, overwrite: bool) -> None:
    """Publish a same-directory temporary file without a no-overwrite race."""
    if overwrite:
        os.replace(temporary, target)
        return
    try:
        os.link(temporary, target)
    except FileExistsError as error:
        raise RuntimeError(f"Refusing to overwrite existing file: {target}") from error
    os.unlink(temporary)


def save_png(path: Path, image: Image.Image, overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".png"
    )
    os.close(descriptor)
    try:
        image.save(temporary, format="PNG", optimize=True, icc_profile=SRGB_PROFILE)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        publish(temporary, path, overwrite)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be an integer")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} must be an integer") from error
    if not math.isfinite(number) or not number.is_integer():
        raise RuntimeError(f"{label} must be an integer")
    return int(number)


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise RuntimeError(f"{label} must be a finite number")
    return number


def parse_hex(value: Any, label: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise RuntimeError(f"{label} must be a six-digit hex color")
    cleaned = value.strip().lstrip("#")
    if len(cleaned) != 6:
        raise RuntimeError(f"{label} must be a six-digit hex color")
    try:
        return tuple(int(cleaned[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError as error:
        raise RuntimeError(f"{label} must be a six-digit hex color") from error


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{value:02X}" for value in rgb)


def edge_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    sample = image.copy()
    sample.thumbnail((160, 160), Image.Resampling.LANCZOS)
    width, height = sample.size
    band = max(1, min(width, height) // 12)
    pixels: list[tuple[int, int, int]] = []
    pixels.extend(sample.crop((0, 0, width, band)).getdata())
    pixels.extend(sample.crop((0, height - band, width, height)).getdata())
    pixels.extend(sample.crop((0, band, band, height - band)).getdata())
    pixels.extend(sample.crop((width - band, band, width, height - band)).getdata())
    return pixels


def neutralize(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    luminance = round(0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2])
    return tuple(round(luminance * 0.9 + channel * 0.1) for channel in rgb)


def adaptive_matte(images: list[Image.Image], tone: str) -> tuple[int, int, int]:
    pixels = [pixel for image in images for pixel in edge_pixels(image)]
    sample = neutralize(
        tuple(
            round(statistics.median(pixel[index] for pixel in pixels))
            for index in range(3)
        )
    )
    if tone == "dark":
        base, amount = (25, 27, 30), 0.2
    else:
        base, amount = (245, 244, 239), 0.16
    return tuple(
        round(base[index] * (1 - amount) + sample[index] * amount)
        for index in range(3)
    )


def outer_margins(record: dict[str, Any]) -> dict[str, int]:
    raw = record.get("outer_margins")
    if not isinstance(raw, dict):
        raise RuntimeError("Each square diptych needs outer_margins")
    result = {
        name: integer(raw.get(name), f"outer_margins.{name}")
        for name in ("left", "right", "top", "bottom")
    }
    if min(result.values()) < MIN_OUTER_MATTE:
        raise RuntimeError(
            f"Square-diptych outer margins must be at least {MIN_OUTER_MATTE} px"
        )
    return result


def normalize_keyline(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("keyline")
    if raw is None:
        return {"enabled": False}
    if not isinstance(raw, dict):
        raise RuntimeError("keyline must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RuntimeError("keyline.enabled must be true or false")
    if not enabled:
        return {"enabled": False}
    width = integer(raw.get("width", 1), "keyline.width")
    if width < 1 or width > 16:
        raise RuntimeError("keyline.width must be between 1 and 16 px")
    color = parse_hex(raw.get("color", "#9A9A96"), "keyline.color")
    return {"enabled": True, "color": hex_color(color), "width": width}


def normalize_shadow(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("shadow")
    if raw is None:
        return {"enabled": False}
    if not isinstance(raw, dict):
        raise RuntimeError("shadow must be an object")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise RuntimeError("shadow.enabled must be true or false")
    if not enabled:
        return {"enabled": False}
    opacity = finite_number(raw.get("opacity", 0.14), "shadow.opacity")
    blur = integer(raw.get("blur", 16), "shadow.blur")
    offset_x = integer(raw.get("offset_x", 0), "shadow.offset_x")
    offset_y = integer(raw.get("offset_y", 7), "shadow.offset_y")
    if not 0 <= opacity <= 1:
        raise RuntimeError("shadow.opacity must be between 0 and 1")
    if not 0 <= blur <= 128:
        raise RuntimeError("shadow.blur must be between 0 and 128 px")
    if abs(offset_x) > 256 or abs(offset_y) > 256:
        raise RuntimeError("shadow offsets must stay within 256 px")
    color = parse_hex(raw.get("color", "#000000"), "shadow.color")
    return {
        "enabled": True,
        "color": hex_color(color),
        "opacity": opacity,
        "blur": blur,
        "offset_x": offset_x,
        "offset_y": offset_y,
    }


def validate_canvas(document: dict[str, Any]) -> None:
    canvas = document.get("canvas")
    if not isinstance(canvas, dict):
        raise RuntimeError("Art direction needs a canvas object")
    if (
        integer(canvas.get("width"), "canvas.width") != CANVAS[0]
        or integer(canvas.get("height"), "canvas.height") != CANVAS[1]
        or canvas.get("color_space") != "sRGB"
    ):
        raise RuntimeError("Square diptychs require a 1920x1080 sRGB canvas")


def load_verified(
    record: dict[str, Any], catalog_base: Path
) -> tuple[Image.Image, Path, str]:
    path_raw = record.get("source_path")
    expected_hash = str(record.get("source_sha256", "")).strip().lower()
    if not path_raw or len(expected_hash) != 64:
        raise RuntimeError("Catalog source path or SHA-256 is missing")
    path = resolve_path(path_raw, catalog_base)
    if not path.is_file():
        raise RuntimeError(f"Catalog source is unavailable: {path}")
    actual_hash = sha256_file(path)
    if actual_hash != expected_hash:
        raise RuntimeError(f"Catalog source hash changed: {path}")
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.load()
    width = integer(record.get("width"), "catalog width")
    height = integer(record.get("height"), "catalog height")
    if image.size != (width, height):
        raise RuntimeError(f"Catalog source dimensions changed: {path}")
    if image.width != image.height or record.get("orientation") != "square":
        raise RuntimeError(f"Square diptych source is not exactly 1:1: {path}")
    if not isinstance(record.get("source_name"), str) or not record["source_name"].strip():
        raise RuntimeError(f"Catalog source name is missing: {path}")
    return image.copy(), path, actual_hash


def pair_settings(
    direction: dict[str, Any], images: list[Image.Image], upscale_acknowledged: bool
) -> dict[str, Any]:
    asset_id = str(direction.get("asset_id", "")).strip()
    if not SAFE_ASSET_ID.fullmatch(asset_id):
        raise RuntimeError(f"Unsafe or missing square-diptych asset_id: {asset_id!r}")
    source_ids = direction.get("source_asset_ids")
    if (
        not isinstance(source_ids, list)
        or len(source_ids) != 2
        or not all(isinstance(value, str) and value.strip() for value in source_ids)
        or source_ids[0] == source_ids[1]
    ):
        raise RuntimeError(f"{asset_id} needs exactly two distinct source_asset_ids")
    if direction.get("treatment") != "diptych_square":
        raise RuntimeError(f"Unsupported treatment for {asset_id}")
    if len(images) != 2 or any(image.width != image.height for image in images):
        raise RuntimeError(f"{asset_id} needs exactly two decoded 1:1 sources")

    crop_limit = finite_number(
        direction.get("max_crop_fraction", 0.0), "max_crop_fraction"
    )
    if crop_limit != 0.0:
        raise RuntimeError(f"Square diptychs do not crop sources: {asset_id}")
    margins = outer_margins(direction)
    gutter = integer(direction.get("gutter", 64), "gutter")
    if gutter < MIN_GUTTER:
        raise RuntimeError(f"Square-diptych gutter must be at least {MIN_GUTTER} px")
    available_width = CANVAS[0] - margins["left"] - margins["right"] - gutter
    available_height = CANVAS[1] - margins["top"] - margins["bottom"]
    side = min(available_height, available_width // 2)
    if side <= 0:
        raise RuntimeError(f"Margins and gutter leave no room for {asset_id}")

    allow_upscale = direction.get("allow_upscale", False)
    if not isinstance(allow_upscale, bool):
        raise RuntimeError("allow_upscale must be true or false")
    scale_factors = [side / image.width for image in images]
    if max(scale_factors) > 1.000001 and not allow_upscale:
        raise RuntimeError(f"Square diptych would enlarge a source: {asset_id}")
    if max(scale_factors) > 1.000001 and not upscale_acknowledged:
        raise RuntimeError(
            f"{asset_id} allows enlargement but acknowledge_upscale_risk is not true"
        )

    group_width = side * 2 + gutter
    usable_left = margins["left"]
    usable_width = CANVAS[0] - margins["left"] - margins["right"]
    x = usable_left + (usable_width - group_width) // 2
    bias = finite_number(direction.get("vertical_bias", 0.0), "vertical_bias")
    y = round((CANVAS[1] - side) / 2 + bias * CANVAS[1])
    y = max(margins["top"], min(CANVAS[1] - margins["bottom"] - side, y))
    rects = [(x, y, side, side), (x + side + gutter, y, side, side)]

    strategy = direction.get("matte_strategy", "adaptive")
    if strategy == "fixed":
        matte = parse_hex(direction.get("matte_hex"), "matte_hex")
    elif strategy == "adaptive":
        tone = direction.get("matte_tone", "light")
        if tone not in {"light", "dark"}:
            raise RuntimeError(f"Invalid matte_tone for {asset_id}")
        matte = adaptive_matte(images, str(tone))
    else:
        raise RuntimeError(f"Invalid matte_strategy for {asset_id}")

    actual_margins = {
        "left": rects[0][0],
        "right": CANVAS[0] - rects[1][0] - side,
        "top": y,
        "bottom": CANVAS[1] - y - side,
    }
    return {
        "asset_id": asset_id,
        "source_asset_ids": [str(value) for value in source_ids],
        "matte": matte,
        "matte_hex": hex_color(matte),
        "declared_outer_margins": margins,
        "actual_outer_margins": actual_margins,
        "gutter": gutter,
        "square_size": side,
        "rects": rects,
        "keyline": normalize_keyline(direction),
        "shadow": normalize_shadow(direction),
        "allow_upscale": allow_upscale,
        "scale_factors": scale_factors,
    }


def shadow_layer(
    rects: list[tuple[int, int, int, int]], shadow: dict[str, Any]
) -> Image.Image | None:
    if not shadow["enabled"]:
        return None
    alpha = Image.new("L", CANVAS, 0)
    draw = ImageDraw.Draw(alpha)
    opacity = round(shadow["opacity"] * 255)
    for x, y, width, height in rects:
        draw.rectangle(
            (
                x + shadow["offset_x"],
                y + shadow["offset_y"],
                x + width - 1 + shadow["offset_x"],
                y + height - 1 + shadow["offset_y"],
            ),
            fill=opacity,
        )
    alpha = alpha.filter(ImageFilter.GaussianBlur(shadow["blur"]))
    layer = Image.new("RGBA", CANVAS, (*parse_hex(shadow["color"], "shadow.color"), 0))
    layer.putalpha(alpha)
    return layer


def compose_pair(
    direction: dict[str, Any], images: list[Image.Image], upscale_acknowledged: bool
) -> tuple[Image.Image, dict[str, Any]]:
    """Compose a pair and return the panel plus normalized rendering facts."""
    settings = pair_settings(direction, images, upscale_acknowledged)
    canvas = Image.new("RGB", CANVAS, settings["matte"])
    shadow = shadow_layer(settings["rects"], settings["shadow"])
    if shadow is not None:
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow).convert("RGB")
    keyline = settings["keyline"]
    if keyline["enabled"]:
        draw = ImageDraw.Draw(canvas)
        color = parse_hex(keyline["color"], "keyline.color")
        width = keyline["width"]
        for x, y, side, _ in settings["rects"]:
            draw.rectangle(
                (x - width, y - width, x + side + width - 1, y + side + width - 1),
                outline=color,
                width=width,
            )
    for image, rect in zip(images, settings["rects"], strict=True):
        resized = image.resize((rect[2], rect[3]), Image.Resampling.LANCZOS)
        canvas.paste(resized, (rect[0], rect[1]))
    return canvas, settings


def render_pair(
    direction: dict[str, Any],
    source_records: list[dict[str, Any]],
    catalog_base: Path,
    output: Path,
    overwrite: bool,
    upscale_acknowledged: bool,
) -> dict[str, Any]:
    loaded = [load_verified(record, catalog_base) for record in source_records]
    images = [value[0] for value in loaded]
    panel, settings = compose_pair(direction, images, upscale_acknowledged)
    save_png(output, panel, overwrite)
    return {
        "asset_id": settings["asset_id"],
        "treatment": "diptych_square",
        "source_asset_ids": settings["source_asset_ids"],
        "sources": [
            {
                "asset_id": record["asset_id"],
                "source_name": record["source_name"],
                "source_path": str(path),
                "source_sha256": digest,
                "source_width": image.width,
                "source_height": image.height,
                "orientation": "square",
            }
            for record, image, (_, path, digest) in zip(
                source_records, images, loaded, strict=True
            )
        ],
        "output_path": str(output),
        "output_sha256": sha256_file(output),
        "output_width": CANVAS[0],
        "output_height": CANVAS[1],
        "output_mode": "RGB",
        "color_space": "sRGB",
        "matte_hex": settings["matte_hex"],
        "declared_outer_margins": settings["declared_outer_margins"],
        "actual_outer_margins": settings["actual_outer_margins"],
        "image_rects": [
            {"x": x, "y": y, "width": width, "height": height}
            for x, y, width, height in settings["rects"]
        ],
        "gutter": settings["gutter"],
        "square_size": settings["square_size"],
        "crop_fraction": 0.0,
        "max_crop_fraction": 0.0,
        "complete_sources": True,
        "equal_size": True,
        "keyline": settings["keyline"],
        "shadow": settings["shadow"],
        "allow_upscale": settings["allow_upscale"],
        "scale_factors": [round(value, 8) for value in settings["scale_factors"]],
        "rationale": direction.get("rationale"),
        "pair_evidence": direction.get("pair_evidence"),
        "left_right_reason": direction.get("left_right_reason"),
    }


def contact_sheets(
    outputs: list[dict[str, Any]], directory: Path, overwrite: bool
) -> list[dict[str, Any]]:
    sheets: list[dict[str, Any]] = []
    font = ImageFont.load_default(size=24)
    for page, offset in enumerate(range(0, len(outputs), 2), start=1):
        records = outputs[offset : offset + 2]
        sheet = Image.new("RGB", (1840, 1140), (22, 23, 25))
        draw = ImageDraw.Draw(sheet)
        for column, record in enumerate(records):
            with Image.open(record["output_path"]) as opened:
                preview = opened.convert("RGB")
                preview.thumbnail((880, 990), Image.Resampling.LANCZOS)
                x = 20 + column * 910 + (880 - preview.width) // 2
                y = 30 + (990 - preview.height) // 2
                sheet.paste(preview, (x, y))
            draw.text(
                (20 + column * 910, 1040),
                record["asset_id"],
                fill=(235, 235, 232),
                font=font,
            )
        path = directory / f"square-diptych-contact-sheet-{page:02d}.png"
        save_png(path, sheet, overwrite)
        sheets.append(
            {
                "path": str(path),
                "sha256": sha256_file(path),
                "width": 1840,
                "height": 1140,
            }
        )
    return sheets


def main() -> int:
    args = parse_args()
    catalog_path = args.catalog.resolve(strict=True)
    art_path = args.art_direction.resolve(strict=True)
    output_dir = args.output_dir.resolve(strict=False)
    catalog = load_object(catalog_path)
    direction = load_object(art_path)
    if catalog.get("schema_version") != 1 or direction.get("schema_version") != 1:
        raise RuntimeError("Catalog and art direction must use schema_version 1")
    validate_canvas(direction)
    catalog_records = catalog.get("records")
    direction_records = direction.get("records")
    if (
        catalog.get("status") != "complete"
        or not isinstance(catalog_records, list)
        or not all(isinstance(record, dict) for record in catalog_records)
        or catalog.get("record_count") != len(catalog_records)
    ):
        raise RuntimeError("Source catalog is incomplete or malformed")
    if (
        not isinstance(direction_records, list)
        or not direction_records
        or not all(isinstance(record, dict) for record in direction_records)
    ):
        raise RuntimeError("Art-direction manifest has no valid square-diptych records")
    sources = {str(record.get("asset_id", "")): record for record in catalog_records}
    if len(sources) != len(catalog_records) or "" in sources:
        raise RuntimeError("Source catalog has missing or duplicate asset IDs")

    pair_ids: list[str] = []
    unordered_pairs: list[tuple[str, str]] = []
    pair_sources: list[list[dict[str, Any]]] = []
    for record in direction_records:
        pair_id = str(record.get("asset_id", "")).strip()
        source_ids = record.get("source_asset_ids")
        if not isinstance(source_ids, list) or len(source_ids) != 2:
            raise RuntimeError(f"{pair_id or '<missing>'} needs exactly two sources")
        source_names = [str(value) for value in source_ids]
        try:
            selected = [sources[value] for value in source_names]
        except KeyError as error:
            raise RuntimeError(f"Unknown source asset in {pair_id}: {error}") from error
        pair_ids.append(pair_id)
        unordered_pairs.append(tuple(sorted(source_names)))
        pair_sources.append(selected)
    if "" in pair_ids or len(pair_ids) != len(set(pair_ids)):
        raise RuntimeError("Square-diptych IDs are missing or duplicated")
    if len(unordered_pairs) != len(set(unordered_pairs)):
        raise RuntimeError("A square source pair appears more than once")

    acknowledged = direction.get("acknowledge_upscale_risk", False)
    if not isinstance(acknowledged, bool):
        raise RuntimeError("acknowledge_upscale_risk must be true or false")
    render_dir = output_dir / "rendered"
    outputs = [
        render_pair(
            record,
            selected,
            catalog_path.parent,
            render_dir / f"{pair_id}__1920x1080.png",
            args.overwrite,
            acknowledged,
        )
        for record, selected, pair_id in zip(
            direction_records, pair_sources, pair_ids, strict=True
        )
    ]
    sheets = contact_sheets(outputs, output_dir / "contact-sheets", args.overwrite)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "complete",
        "private_artifact": True,
        "catalog_path": str(catalog_path),
        "catalog_sha256": sha256_file(catalog_path),
        "art_direction_path": str(art_path),
        "art_direction_sha256": sha256_file(art_path),
        "record_count": len(outputs),
        "acknowledge_upscale_risk": acknowledged,
        "records": outputs,
        "contact_sheets": sheets,
    }
    atomic_save(output_dir / "square-diptych-render-manifest.json", manifest, args.overwrite)
    print(f"COMPLETE rendered {len(outputs)} square diptychs", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR {error}", file=__import__("sys").stderr, flush=True)
        raise SystemExit(1) from error
