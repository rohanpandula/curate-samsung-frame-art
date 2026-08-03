#!/usr/bin/env python3
"""Render two portrait photographs as one Samsung Frame panel."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import statistics
import tempfile
from typing import Any

from PIL import Image, ImageCms, ImageDraw, ImageFilter, ImageFont, ImageOps


Image.MAX_IMAGE_PIXELS = 250_000_000
CANVAS = (1920, 1080)
SRGB_PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render verified portrait pairs as thick-matte 1920x1080 diptychs."
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
    """Publish a same-directory temp file without a no-overwrite race."""
    if overwrite:
        os.replace(temporary, target)
        return
    try:
        os.link(temporary, target)
    except FileExistsError as error:
        raise RuntimeError(f"Refusing to overwrite existing file: {target}") from error
    os.unlink(temporary)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_hex(value: str) -> tuple[int, int, int]:
    cleaned = value.lstrip("#")
    if len(cleaned) != 6:
        raise RuntimeError(f"Expected six-digit matte color, got {value!r}")
    try:
        return tuple(int(cleaned[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError as error:
        raise RuntimeError(f"Invalid color {value!r}") from error


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{value:02X}" for value in rgb)


def load_verified(record: dict[str, Any]) -> Image.Image:
    path = Path(str(record.get("source_path", "")))
    expected = str(record.get("source_sha256", ""))
    if not path.is_file() or not expected:
        raise RuntimeError(f"Catalog source is unavailable: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(f"Catalog source hash changed: {path}")
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        return image.copy()


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
        round(base[index] * (1 - amount) + sample[index] * amount) for index in range(3)
    )


def margins(record: dict[str, Any]) -> tuple[int, int, int, int]:
    raw = record.get("outer_margins", {})
    try:
        values = tuple(int(raw[name]) for name in ("left", "right", "top", "bottom"))
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Each diptych needs integer outer_margins") from error
    if min(values) < 64:
        raise RuntimeError("Diptych outer margins must be at least 64 px")
    return values


def shadow_layer(
    rects: list[tuple[int, int, int, int]], shadow: dict[str, Any]
) -> Image.Image | None:
    if not shadow.get("enabled", False):
        return None
    opacity = max(0, min(255, round(float(shadow.get("opacity", 0.14)) * 255)))
    blur = max(0, int(shadow.get("blur", 16)))
    offset_y = int(shadow.get("offset_y", 7))
    alpha = Image.new("L", CANVAS, 0)
    draw = ImageDraw.Draw(alpha)
    for x, y, width, height in rects:
        draw.rectangle(
            (x, y + offset_y, x + width - 1, y + height + offset_y - 1), fill=opacity
        )
    alpha = alpha.filter(ImageFilter.GaussianBlur(blur))
    layer = Image.new("RGB", CANVAS, (0, 0, 0))
    layer.putalpha(alpha)
    return layer


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


def render_pair(
    direction: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    output: Path,
    overwrite: bool,
    upscale_risk_acknowledged: bool,
) -> dict[str, Any]:
    asset_id = str(direction.get("asset_id", ""))
    source_ids = direction.get("source_asset_ids")
    if not asset_id or not isinstance(source_ids, list) or len(source_ids) != 2:
        raise RuntimeError("Each diptych needs asset_id and two source_asset_ids")
    if direction.get("treatment") != "diptych_portrait":
        raise RuntimeError(f"Unsupported treatment for {asset_id}")
    try:
        source_records = [sources[str(value)] for value in source_ids]
    except KeyError as error:
        raise RuntimeError(f"Unknown source asset in {asset_id}: {error}") from error
    images = [load_verified(record) for record in source_records]
    if any(image.width >= image.height for image in images):
        raise RuntimeError(f"Diptych source is not portrait-oriented: {asset_id}")

    strategy = str(direction.get("matte_strategy", "adaptive"))
    if strategy == "fixed":
        matte = parse_hex(str(direction.get("matte_hex", "")))
    elif strategy == "adaptive":
        tone = str(direction.get("matte_tone", "light"))
        if tone not in {"light", "dark"}:
            raise RuntimeError(f"Invalid matte_tone for {asset_id}")
        matte = adaptive_matte(images, tone)
    else:
        raise RuntimeError(f"Invalid matte_strategy for {asset_id}")

    left, right, top, bottom = margins(direction)
    gutter = int(direction.get("gutter", 64))
    if gutter < 32:
        raise RuntimeError(f"Diptych gutter is too narrow: {asset_id}")
    inner_width = CANVAS[0] - left - right - gutter
    inner_height = CANVAS[1] - top - bottom
    ratios = [image.width / image.height for image in images]
    common_height = min(inner_height, int(inner_width / sum(ratios)))
    widths = [round(common_height * ratio) for ratio in ratios]
    scale_factors = [common_height / image.height for image in images]
    allow_upscale = direction.get("allow_upscale") is True
    if max(scale_factors) > 1.000001 and not allow_upscale:
        raise RuntimeError(
            f"Diptych would upscale a source without approval: {asset_id}"
        )
    if max(scale_factors) > 1.000001 and not upscale_risk_acknowledged:
        raise RuntimeError(
            f"Diptych allows enlargement but acknowledge_upscale_risk is not true: {asset_id}"
        )
    group_width = sum(widths) + gutter
    x0 = round((CANVAS[0] - group_width) / 2)
    bias = float(direction.get("vertical_bias", 0.0))
    y = round((CANVAS[1] - common_height) / 2 + bias * CANVAS[1])
    y = max(top, min(CANVAS[1] - bottom - common_height, y))
    rects = [
        (x0, y, widths[0], common_height),
        (x0 + widths[0] + gutter, y, widths[1], common_height),
    ]

    canvas = Image.new("RGB", CANVAS, matte)
    shadow = shadow_layer(rects, direction.get("shadow", {}))
    if shadow is not None:
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow).convert("RGB")
    keyline = direction.get("keyline", {})
    if keyline.get("enabled", False):
        color = parse_hex(str(keyline.get("color", "#9A9A96")))
        line_width = max(1, int(keyline.get("width", 1)))
        draw = ImageDraw.Draw(canvas)
        for x, y0, width, height in rects:
            draw.rectangle(
                (
                    x - line_width,
                    y0 - line_width,
                    x + width + line_width - 1,
                    y0 + height + line_width - 1,
                ),
                outline=color,
                width=line_width,
            )
    for image, rect in zip(images, rects, strict=True):
        resized = image.resize((rect[2], rect[3]), Image.Resampling.LANCZOS)
        canvas.paste(resized, (rect[0], rect[1]))
    save_png(output, canvas, overwrite)
    return {
        "asset_id": asset_id,
        "treatment": "diptych_portrait",
        "source_asset_ids": source_ids,
        "sources": [
            {
                "asset_id": record["asset_id"],
                "source_name": record["source_name"],
                "source_path": record["source_path"],
                "source_sha256": record["source_sha256"],
                "source_width": image.width,
                "source_height": image.height,
            }
            for record, image in zip(source_records, images, strict=True)
        ],
        "output_path": str(output),
        "output_sha256": sha256_file(output),
        "output_width": CANVAS[0],
        "output_height": CANVAS[1],
        "output_mode": "RGB",
        "color_space": "sRGB",
        "matte_hex": hex_color(matte),
        "image_rects": [
            {"x": x, "y": y0, "width": width, "height": height}
            for x, y0, width, height in rects
        ],
        "gutter": gutter,
        "crop_fraction": 0.0,
        "max_crop_fraction": float(direction.get("max_crop_fraction", 0.0)),
        "allow_upscale": allow_upscale,
        "scale_factors": [round(value, 8) for value in scale_factors],
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
            label = record["asset_id"]
            draw.text((20 + column * 910, 1040), label, fill=(235, 235, 232), font=font)
        path = directory / f"diptych-contact-sheet-{page:02d}.png"
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
    catalog = load_object(args.catalog)
    direction = load_object(args.art_direction)
    catalog_records = catalog.get("records")
    direction_records = direction.get("records")
    if catalog.get("status") != "complete" or not isinstance(catalog_records, list):
        raise RuntimeError("Source catalog is incomplete")
    if not isinstance(direction_records, list) or not direction_records:
        raise RuntimeError("Art-direction manifest has no diptych records")
    sources = {str(record.get("asset_id", "")): record for record in catalog_records}
    if len(sources) != len(catalog_records) or "" in sources:
        raise RuntimeError("Source catalog has missing or duplicate asset IDs")
    ids = [str(record.get("asset_id", "")) for record in direction_records]
    if "" in ids or len(ids) != len(set(ids)):
        raise RuntimeError(
            "Art-direction manifest has missing or duplicate diptych IDs"
        )

    render_dir = args.output_dir / "rendered"
    outputs = [
        render_pair(
            record,
            sources,
            render_dir / f"{record['asset_id']}__1920x1080.png",
            args.overwrite,
            direction.get("acknowledge_upscale_risk") is True,
        )
        for record in direction_records
    ]
    sheets = contact_sheets(outputs, args.output_dir / "contact-sheets", args.overwrite)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "complete",
        "private_artifact": True,
        "catalog_path": str(args.catalog.resolve()),
        "catalog_sha256": sha256_file(args.catalog),
        "art_direction_path": str(args.art_direction.resolve()),
        "art_direction_sha256": sha256_file(args.art_direction),
        "record_count": len(outputs),
        "acknowledge_upscale_risk": direction.get("acknowledge_upscale_risk") is True,
        "records": outputs,
        "contact_sheets": sheets,
    }
    atomic_save(
        args.output_dir / "diptych-render-manifest.json", manifest, args.overwrite
    )
    print(f"COMPLETE rendered {len(outputs)} portrait diptychs", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR {error}", file=__import__("sys").stderr, flush=True)
        raise SystemExit(1) from error
