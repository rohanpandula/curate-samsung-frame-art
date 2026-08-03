#!/usr/bin/env python3
"""Validate portrait diptych proofs before they can enter a Frame upload batch."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from io import BytesIO
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from PIL import Image, ImageChops, ImageCms, ImageOps, ImageStat


Image.MAX_IMAGE_PIXELS = 250_000_000
CANVAS = (1920, 1080)
MIN_OUTER_MATTE = 64
MIN_GUTTER = 32
CROP_TOLERANCE = 0.002


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate rendered 1920x1080 portrait diptychs and write a PASS/FAIL audit."
    )
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--art-direction", type=Path, required=True)
    parser.add_argument("--render-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_save(path: Path, value: dict[str, Any]) -> None:
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


def load_object(path: Path) -> tuple[dict[str, Any], str]:
    digest = sha256_file(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value, digest


def resolve_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def valid_sha256(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        return ""
    return candidate


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        candidate = float(value)
    except (TypeError, ValueError):
        return None
    return candidate if math.isfinite(candidate) else None


def integer(value: Any) -> int | None:
    candidate = number(value)
    if candidate is None or not candidate.is_integer():
        return None
    return int(candidate)


def pair_id(record: dict[str, Any]) -> str:
    value = record.get("asset_id")
    return str(value).strip() if value is not None else ""


def source_ids(record: dict[str, Any]) -> list[str]:
    raw = record.get("source_asset_ids")
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw]


def records(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("records")
    if not isinstance(raw, list):
        return []
    return [value for value in raw if isinstance(value, dict)]


def parse_rect(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, dict):
        return None
    parts = tuple(integer(value.get(key)) for key in ("x", "y", "width", "height"))
    if any(part is None for part in parts):
        return None
    return parts  # type: ignore[return-value]


def profile_is_srgb(profile_bytes: bytes) -> bool:
    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(profile_bytes))
        description = ImageCms.getProfileDescription(profile)
        name = ImageCms.getProfileName(profile)
    except Exception:
        return False
    return "srgb" in f"{description} {name}".lower()


def open_source(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.load()
        return image.copy()


def open_panel(path: Path) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(path) as opened:
        opened.load()
        profile = opened.info.get("icc_profile")
        facts = {
            "format": str(opened.format or ""),
            "mode": opened.mode,
            "width": opened.width,
            "height": opened.height,
            "icc_profile_bytes": len(profile) if isinstance(profile, bytes) else 0,
            "srgb_icc_profile": isinstance(profile, bytes) and profile_is_srgb(profile),
        }
        return opened.convert("RGB"), facts


def strip_difference(first: Image.Image, second: Image.Image) -> float:
    means = ImageStat.Stat(ImageChops.difference(first, second)).mean
    return sum(means) / len(means) if means else 0.0


def boundary_contrast(image: Image.Image, rect: tuple[int, int, int, int]) -> float:
    x, y, width, height = rect
    right = x + width
    bottom = y + height
    inset_x = max(1, min(width // 10, 80))
    inset_y = max(1, min(height // 10, 80))
    values: list[float] = []
    if x >= 2 and height > 2 * inset_y:
        values.append(
            strip_difference(
                image.crop((x, y + inset_y, x + 1, bottom - inset_y)),
                image.crop((x - 2, y + inset_y, x - 1, bottom - inset_y)),
            )
        )
    if right + 2 <= image.width and height > 2 * inset_y:
        values.append(
            strip_difference(
                image.crop((right - 1, y + inset_y, right, bottom - inset_y)),
                image.crop((right + 1, y + inset_y, right + 2, bottom - inset_y)),
            )
        )
    if y >= 2 and width > 2 * inset_x:
        values.append(
            strip_difference(
                image.crop((x + inset_x, y, right - inset_x, y + 1)),
                image.crop((x + inset_x, y - 2, right - inset_x, y - 1)),
            )
        )
    if bottom + 2 <= image.height and width > 2 * inset_x:
        values.append(
            strip_difference(
                image.crop((x + inset_x, bottom - 1, right - inset_x, bottom)),
                image.crop((x + inset_x, bottom + 1, right - inset_x, bottom + 2)),
            )
        )
    return max(values, default=0.0)


def rectangles_overlap(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> bool:
    first_right = first[0] + first[2]
    first_bottom = first[1] + first[3]
    second_right = second[0] + second[2]
    second_bottom = second[1] + second[3]
    return not (
        first_right <= second[0]
        or second_right <= first[0]
        or first_bottom <= second[1]
        or second_bottom <= first[1]
    )


def crop_fraction(source_size: tuple[int, int], output_size: tuple[int, int]) -> float:
    source_ratio = source_size[0] / source_size[1]
    output_ratio = output_size[0] / output_size[1]
    kept = (
        output_ratio / source_ratio
        if source_ratio > output_ratio
        else source_ratio / output_ratio
    )
    return max(0.0, 1.0 - min(1.0, kept))


class Audit:
    def __init__(self, inputs: dict[str, str]) -> None:
        self.inputs = inputs
        self.checks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.pairs: list[dict[str, Any]] = []

    def passed(self, name: str, **details: Any) -> None:
        self.checks.append({"name": name, "status": "PASS", **details})

    def fail(self, name: str, message: str, **details: Any) -> None:
        failure = {"name": name, "message": message, **details}
        self.failures.append(failure)
        self.checks.append(
            {"name": name, "status": "FAIL", "message": message, **details}
        )

    def warn(self, name: str, message: str, **details: Any) -> None:
        self.warnings.append({"name": name, "message": message, **details})

    def document(self, hashes: dict[str, str]) -> dict[str, Any]:
        status = "FAIL" if self.failures else "PASS"
        return {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "inputs": self.inputs,
            "input_sha256": hashes,
            "summary": {
                "checks_passed": sum(
                    check["status"] == "PASS" for check in self.checks
                ),
                "checks_failed": len(self.failures),
                "warnings": len(self.warnings),
                "pairs_audited": len(self.pairs),
            },
            "checks": self.checks,
            "failures": self.failures,
            "warnings": self.warnings,
            "pairs": self.pairs,
        }


def load_inputs(
    paths: dict[str, Path], audit: Audit
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    documents: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for label, path in paths.items():
        try:
            document, digest = load_object(path)
        except Exception as error:
            audit.fail(
                f"input.{label}",
                f"Cannot hash/read JSON input: {error}",
                path=str(path),
            )
            continue
        documents[label] = document
        hashes[label] = digest
        audit.passed(f"input.{label}", path=str(path), sha256=digest)
    return documents, hashes


def validate_input_bindings(
    manifest: dict[str, Any],
    paths: dict[str, Path],
    hashes: dict[str, str],
    audit: Audit,
) -> None:
    problems: dict[str, Any] = {}
    for label, prefix in (("catalog", "catalog"), ("art_direction", "art_direction")):
        recorded_path = manifest.get(f"{prefix}_path")
        recorded_hash = valid_sha256(manifest.get(f"{prefix}_sha256"))
        actual_path = paths[label]
        resolved_recorded = (
            resolve_path(recorded_path, paths["render_manifest"].parent)
            if recorded_path
            else None
        )
        if resolved_recorded != actual_path or recorded_hash != hashes.get(label):
            problems[label] = {
                "recorded_path": str(resolved_recorded) if resolved_recorded else None,
                "actual_path": str(actual_path),
                "recorded_sha256": recorded_hash or None,
                "actual_sha256": hashes.get(label),
            }
    if problems:
        audit.fail(
            "render_manifest.input_bindings",
            "Render manifest is not bound to these exact catalog/art-direction inputs",
            problems=problems,
        )
    else:
        audit.passed("render_manifest.input_bindings")


def validate_record_sets(
    catalog: dict[str, Any],
    art: dict[str, Any],
    manifest: dict[str, Any],
    audit: Audit,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    catalog_records = records(catalog)
    art_records = records(art)
    render_records = records(manifest)
    catalog_ids = [pair_id(record) for record in catalog_records]
    art_ids = [pair_id(record) for record in art_records]
    render_ids = [pair_id(record) for record in render_records]
    problems: dict[str, Any] = {}
    for label, identifiers, raw_records in (
        ("catalog", catalog_ids, catalog.get("records")),
        ("art_direction", art_ids, art.get("records")),
        ("render_manifest", render_ids, manifest.get("records")),
    ):
        if not isinstance(raw_records, list) or len(raw_records) != len(identifiers):
            problems[f"{label}_malformed_records"] = True
        if not identifiers or "" in identifiers:
            problems[f"{label}_missing_ids"] = [
                index for index, value in enumerate(identifiers) if not value
            ]
        duplicates = sorted(
            {value for value in identifiers if value and identifiers.count(value) > 1}
        )
        if duplicates:
            problems[f"{label}_duplicate_ids"] = duplicates
    if catalog.get("status") != "complete":
        problems["catalog_status"] = catalog.get("status")
    if manifest.get("status") != "complete":
        problems["render_manifest_status"] = manifest.get("status")
    if catalog.get("record_count") != len(catalog_records):
        problems["catalog_record_count"] = catalog.get("record_count")
    if manifest.get("record_count") != len(render_records):
        problems["render_manifest_record_count"] = manifest.get("record_count")
    if art_ids != render_ids:
        problems["pair_id_order"] = {
            "art_direction": art_ids,
            "render_manifest": render_ids,
        }
    if problems:
        audit.fail(
            "record_sets",
            "Catalog or pair records are malformed, duplicated, incomplete, or out of order",
            problems=problems,
        )
    else:
        audit.passed(
            "record_sets",
            catalog_count=len(catalog_records),
            pair_count=len(art_records),
            pair_order=art_ids,
        )
    return (
        {pair_id(record): record for record in catalog_records if pair_id(record)},
        {pair_id(record): record for record in art_records if pair_id(record)},
        {pair_id(record): record for record in render_records if pair_id(record)},
    )


def validate_pair_uniqueness(
    art_records: list[dict[str, Any]],
    render_records: list[dict[str, Any]],
    audit: Audit,
) -> None:
    unordered_pairs: list[tuple[str, str]] = []
    bad_pairs: dict[str, Any] = {}
    for record in art_records:
        identifier = pair_id(record)
        identifiers = source_ids(record)
        if (
            len(identifiers) != 2
            or not all(identifiers)
            or identifiers[0] == identifiers[1]
        ):
            bad_pairs[identifier or "<missing>"] = identifiers
        elif record.get("treatment") != "diptych_portrait":
            bad_pairs[identifier] = {
                "source_asset_ids": identifiers,
                "treatment": record.get("treatment"),
            }
        else:
            unordered_pairs.append(tuple(sorted(identifiers)))
    duplicate_unordered = sorted(
        {pair for pair in unordered_pairs if unordered_pairs.count(pair) > 1}
    )
    output_paths = [
        str(record.get("output_path", "")).strip() for record in render_records
    ]
    duplicate_outputs = sorted(
        {path for path in output_paths if path and output_paths.count(path) > 1}
    )
    missing_outputs = [
        pair_id(record)
        for record in render_records
        if not str(record.get("output_path", "")).strip()
    ]
    if bad_pairs or duplicate_unordered or duplicate_outputs or missing_outputs:
        audit.fail(
            "pair_uniqueness",
            "Pairs need two distinct sources plus unique IDs, unordered pairs, and output paths",
            invalid_pairs=bad_pairs,
            duplicate_unordered_source_pairs=[
                list(pair) for pair in duplicate_unordered
            ],
            duplicate_output_paths=duplicate_outputs,
            missing_output_paths=missing_outputs,
        )
    else:
        audit.passed(
            "pair_uniqueness",
            pair_count=len(art_records),
            output_count=len(output_paths),
        )


def validate_source(
    pair_identifier: str,
    side: int,
    source_identifier: str,
    catalog_record: dict[str, Any] | None,
    render_source: dict[str, Any] | None,
    catalog_base: Path,
    audit: Audit,
) -> tuple[Image.Image | None, dict[str, Any]]:
    label = "left" if side == 0 else "right"
    facts: dict[str, Any] = {"asset_id": source_identifier, "side": label}
    if catalog_record is None or render_source is None:
        audit.fail(
            f"pair.{pair_identifier}.source.{label}",
            "Source is absent from the catalog or renderer source list",
            source_asset_id=source_identifier,
            catalog_present=catalog_record is not None,
            renderer_present=render_source is not None,
        )
        return None, facts
    path_raw = catalog_record.get("source_path")
    path = resolve_path(path_raw, catalog_base) if path_raw else None
    expected_hash = valid_sha256(catalog_record.get("source_sha256"))
    expected_width = integer(catalog_record.get("width"))
    expected_height = integer(catalog_record.get("height"))
    errors: list[str] = []
    actual_hash = ""
    image: Image.Image | None = None
    if path is None or not path.is_file():
        errors.append("catalog source file is missing")
    elif not expected_hash or expected_width is None or expected_height is None:
        errors.append("catalog source hash/dimensions are missing or invalid")
    else:
        try:
            actual_hash = sha256_file(path)
            image = open_source(path)
        except Exception as error:
            errors.append(f"source cannot be hashed/decoded: {error}")
        if actual_hash and actual_hash != expected_hash:
            errors.append("source SHA-256 changed")
        if image is not None and image.size != (expected_width, expected_height):
            errors.append("source dimensions changed")
        if image is not None and image.width >= image.height:
            errors.append("source is not portrait-oriented")
        if catalog_record.get("orientation") != "portrait":
            errors.append("catalog orientation is not portrait")
    render_path_raw = render_source.get("source_path")
    render_path = (
        resolve_path(render_path_raw, catalog_base) if render_path_raw else None
    )
    render_hash = valid_sha256(render_source.get("source_sha256"))
    render_width = integer(render_source.get("source_width"))
    render_height = integer(render_source.get("source_height"))
    if str(render_source.get("asset_id", "")) != source_identifier:
        errors.append("renderer source ID/order changed")
    if path is not None and render_path != path:
        errors.append("renderer source path differs from catalog")
    if not actual_hash or render_hash != actual_hash:
        errors.append("renderer source hash differs from rehashed catalog source")
    if image is not None and (render_width, render_height) != image.size:
        errors.append("renderer source dimensions differ from decoded source")
    facts.update(
        {
            "path": str(path) if path else None,
            "sha256": actual_hash or None,
            "width": image.width if image is not None else None,
            "height": image.height if image is not None else None,
        }
    )
    if errors:
        audit.fail(
            f"pair.{pair_identifier}.source.{label}",
            "Source verification failed",
            errors=errors,
            **facts,
        )
    else:
        audit.passed(f"pair.{pair_identifier}.source.{label}", **facts)
    return image, facts


def validate_output(
    pair_identifier: str,
    record: dict[str, Any],
    manifest_base: Path,
    audit: Audit,
) -> tuple[Path | None, Image.Image | None, dict[str, Any]]:
    raw_path = record.get("output_path")
    path = resolve_path(raw_path, manifest_base) if raw_path else None
    expected_hash = valid_sha256(record.get("output_sha256"))
    actual_hash = ""
    panel: Image.Image | None = None
    facts: dict[str, Any] = {"path": str(path) if path else None}
    errors: list[str] = []
    if path is None or not path.is_file():
        errors.append("rendered output is missing")
    else:
        try:
            actual_hash = sha256_file(path)
            panel, image_facts = open_panel(path)
            facts.update(image_facts)
        except Exception as error:
            errors.append(f"rendered output cannot be hashed/decoded: {error}")
        if path.suffix.lower() != ".png" or facts.get("format", "").upper() != "PNG":
            errors.append("rendered output is not a PNG")
        if (facts.get("width"), facts.get("height")) != CANVAS:
            errors.append("rendered output is not exactly 1920x1080")
        if facts.get("mode") != "RGB":
            errors.append("rendered output is not RGB")
        if not facts.get("srgb_icc_profile"):
            errors.append("rendered output lacks an sRGB ICC profile")
        if not expected_hash or expected_hash != actual_hash:
            errors.append("rendered output SHA-256 differs from manifest")
        if (
            integer(record.get("output_width")) != CANVAS[0]
            or integer(record.get("output_height")) != CANVAS[1]
            or record.get("output_mode") != "RGB"
            or record.get("color_space") != "sRGB"
        ):
            errors.append("render manifest panel metadata is not 1920x1080 RGB/sRGB")
    facts["sha256"] = actual_hash or None
    if errors:
        audit.fail(
            f"pair.{pair_identifier}.output",
            "Rendered diptych failed output validation",
            errors=errors,
            expected_sha256=expected_hash or None,
            **facts,
        )
    else:
        audit.passed(f"pair.{pair_identifier}.output", **facts)
    return path, panel, facts


def declared_outer_margins(record: dict[str, Any]) -> dict[str, int] | None:
    raw = record.get("outer_margins")
    if not isinstance(raw, dict):
        return None
    result = {key: integer(raw.get(key)) for key in ("left", "right", "top", "bottom")}
    if any(value is None for value in result.values()):
        return None
    return result  # type: ignore[return-value]


def validate_geometry(
    pair_identifier: str,
    art_record: dict[str, Any],
    render_record: dict[str, Any],
    sources: list[Image.Image | None],
    panel: Image.Image | None,
    acknowledge_upscale_risk: bool,
    audit: Audit,
) -> dict[str, Any]:
    raw_rects = render_record.get("image_rects")
    rects = (
        [parse_rect(value) for value in raw_rects]
        if isinstance(raw_rects, list)
        else []
    )
    outer = declared_outer_margins(art_record)
    declared_gutter = integer(art_record.get("gutter"))
    rendered_gutter = integer(render_record.get("gutter"))
    facts: dict[str, Any] = {
        "image_rects": [list(rect) if rect else None for rect in rects],
        "declared_outer_margins": outer,
        "declared_gutter": declared_gutter,
        "rendered_gutter": rendered_gutter,
    }
    errors: list[str] = []
    if len(rects) != 2 or any(rect is None for rect in rects):
        errors.append("manifest does not contain exactly two valid integer image_rects")
        audit.fail(
            f"pair.{pair_identifier}.geometry",
            "Diptych geometry validation failed",
            errors=errors,
            **facts,
        )
        return facts
    left, right = rects  # type: ignore[misc]
    for index, rect in enumerate((left, right)):
        x, y, width, height = rect
        if (
            width <= 0
            or height <= 0
            or x < 0
            or y < 0
            or x + width > CANVAS[0]
            or y + height > CANVAS[1]
        ):
            errors.append(
                f"image_rects[{index}] is outside the panel or has non-positive size"
            )
    if rectangles_overlap(left, right):
        errors.append("image rectangles overlap")
    if left[0] >= right[0]:
        errors.append("image rectangle order is not left then right")
    if left[1] != right[1] or left[3] != right[3]:
        errors.append("image rectangles do not share equal optical height/alignment")
    actual_gutter = right[0] - (left[0] + left[2])
    actual_outer = {
        "left": left[0],
        "right": CANVAS[0] - right[0] - right[2],
        "top": min(left[1], right[1]),
        "bottom": min(
            CANVAS[1] - left[1] - left[3],
            CANVAS[1] - right[1] - right[3],
        ),
    }
    facts["actual_outer_margins"] = actual_outer
    facts["actual_gutter"] = actual_gutter
    if outer is None or any(value < MIN_OUTER_MATTE for value in outer.values()):
        errors.append("declared outer matte is missing or below 64 px")
    elif any(actual_outer[key] < outer[key] for key in outer):
        errors.append("rendered outer matte is smaller than the art-direction margins")
    if any(value < MIN_OUTER_MATTE for value in actual_outer.values()):
        errors.append("rendered outer matte is below 64 px")
    if (
        declared_gutter is None
        or rendered_gutter is None
        or declared_gutter < MIN_GUTTER
        or rendered_gutter != declared_gutter
        or actual_gutter != declared_gutter
    ):
        errors.append(
            "center gutter is below 32 px or differs from the manifests/rectangles"
        )

    max_crop = number(art_record.get("max_crop_fraction"))
    rendered_max_crop = number(render_record.get("max_crop_fraction"))
    actual_crop = number(render_record.get("crop_fraction"))
    facts.update(
        {
            "max_crop_fraction": max_crop,
            "rendered_max_crop_fraction": rendered_max_crop,
            "crop_fraction": actual_crop,
        }
    )
    if (
        max_crop is None
        or rendered_max_crop is None
        or actual_crop is None
        or max_crop < 0
        or max_crop > 1
        or abs(rendered_max_crop - max_crop) > 1e-9
        or actual_crop < 0
        or actual_crop > max_crop + 1e-9
        or (max_crop == 0 and actual_crop != 0)
    ):
        errors.append("crop is missing, changed, invalid, or nonzero without approval")

    source_facts: list[dict[str, Any]] = []
    upscale_factors: list[float] = []
    for index, (source, rect) in enumerate(zip(sources, (left, right), strict=True)):
        side = "left" if index == 0 else "right"
        geometry: dict[str, Any] = {"side": side}
        if source is not None:
            expected_width = round(rect[3] * source.width / source.height)
            calculated_crop = crop_fraction(source.size, (rect[2], rect[3]))
            source_upscale_factor = max(
                rect[2] / source.width,
                rect[3] / source.height,
            )
            upscale_factors.append(source_upscale_factor)
            geometry.update(
                {
                    "source_aspect_ratio": source.width / source.height,
                    "rendered_aspect_ratio": rect[2] / rect[3],
                    "expected_width_at_rendered_height": expected_width,
                    "calculated_crop_fraction": calculated_crop,
                    "upscale_factor": source_upscale_factor,
                }
            )
            if actual_crop is not None and actual_crop <= CROP_TOLERANCE:
                if abs(rect[2] - expected_width) > 1:
                    errors.append(
                        f"{side} source aspect ratio was not preserved within one resize pixel"
                    )
            elif (
                actual_crop is not None
                and abs(calculated_crop - actual_crop) > CROP_TOLERANCE
            ):
                errors.append(
                    f"{side} crop geometry differs from the recorded crop fraction"
                )
            if (
                calculated_crop
                > (max_crop if max_crop is not None else -1) + CROP_TOLERANCE
            ):
                errors.append(f"{side} geometry exceeds the approved crop ceiling")
            if (
                panel is not None
                and actual_crop is not None
                and actual_crop <= CROP_TOLERANCE
            ):
                expected = source.resize((rect[2], rect[3]), Image.Resampling.LANCZOS)
                actual = panel.crop(
                    (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
                )
                difference = ImageChops.difference(expected, actual)
                pixel_difference = sum(ImageStat.Stat(difference).mean) / 3
                geometry["mean_absolute_pixel_difference"] = pixel_difference
                if difference.getbbox() is not None:
                    errors.append(
                        f"{side} image pixels do not match a deterministic resize of its source"
                    )
        if panel is not None:
            contrast = boundary_contrast(panel, rect)
            crop_image = panel.crop(
                (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
            )
            variance = max(ImageStat.Stat(crop_image).var)
            geometry["boundary_contrast"] = contrast
            geometry["interior_variance"] = variance
            if contrast < 2.0 or variance < 1.0:
                errors.append(
                    f"{side} image rectangle is not visibly distinct from the matte"
                )
        source_facts.append(geometry)
    facts["sources"] = source_facts
    allow_upscale = art_record.get("allow_upscale") is True
    facts["allow_upscale"] = allow_upscale
    facts["acknowledge_upscale_risk"] = acknowledge_upscale_risk
    enlarged = [value for value in upscale_factors if value > 1.0 + 1e-9]
    if enlarged and not allow_upscale:
        errors.append(
            "one or both portraits are enlarged without allow_upscale=true on the pair"
        )
    elif enlarged and not acknowledge_upscale_risk:
        errors.append(
            "approved portrait enlargement still requires top-level acknowledge_upscale_risk=true"
        )
    elif enlarged:
        audit.warn(
            f"pair.{pair_identifier}.upscale_manual_gate",
            "This diptych intentionally enlarges a portrait and requires manual softness review",
            upscale_factors=upscale_factors,
            manual_gate="Inspect both portraits individually before any upload",
        )
    if errors:
        audit.fail(
            f"pair.{pair_identifier}.geometry",
            "Diptych geometry validation failed",
            errors=errors,
            **facts,
        )
    else:
        audit.passed(f"pair.{pair_identifier}.geometry", **facts)
    return facts


def validate_contact_sheets(
    manifest: dict[str, Any], manifest_base: Path, pair_count: int, audit: Audit
) -> set[Path]:
    raw = manifest.get("contact_sheets")
    expected_count = math.ceil(pair_count / 2)
    if not isinstance(raw, list) or len(raw) != expected_count:
        audit.fail(
            "contact_sheets",
            "Contact-sheet count does not match the renderer's two-pairs-per-page schema",
            expected_count=expected_count,
            actual_count=len(raw) if isinstance(raw, list) else None,
        )
        return set()
    paths: list[Path] = []
    errors: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not entry.get("path"):
            errors.append({"index": index, "error": "invalid contact-sheet record"})
            continue
        path = resolve_path(entry["path"], manifest_base)
        expected_hash = valid_sha256(entry.get("sha256"))
        item: dict[str, Any] = {"index": index, "path": str(path)}
        if not path.is_file():
            errors.append({**item, "error": "contact sheet is missing"})
            continue
        try:
            actual_hash = sha256_file(path)
            with Image.open(path) as opened:
                opened.load()
                item.update(
                    {
                        "sha256": actual_hash,
                        "format": str(opened.format or ""),
                        "mode": opened.mode,
                        "width": opened.width,
                        "height": opened.height,
                    }
                )
        except Exception as error:
            errors.append({**item, "error": f"cannot hash/decode: {error}"})
            continue
        if not expected_hash or actual_hash != expected_hash:
            errors.append(
                {**item, "error": "contact-sheet SHA-256 differs from manifest"}
            )
        elif (
            integer(entry.get("width")) != item["width"]
            or integer(entry.get("height")) != item["height"]
        ):
            errors.append(
                {**item, "error": "contact-sheet dimensions differ from manifest"}
            )
        else:
            paths.append(path)
            facts.append(item)
    duplicate_paths = sorted({str(path) for path in paths if paths.count(path) > 1})
    if duplicate_paths:
        errors.append(
            {"error": "duplicate contact-sheet paths", "paths": duplicate_paths}
        )
    if errors:
        audit.fail(
            "contact_sheets",
            "One or more comparison contact sheets failed hash/decode validation",
            errors=errors,
            valid=facts,
        )
    else:
        audit.passed("contact_sheets", count=len(facts), sheets=facts)
    return set(paths)


def validate_output_inventory(
    render_records: list[dict[str, Any]], manifest_base: Path, audit: Audit
) -> None:
    expected = {
        resolve_path(record["output_path"], manifest_base)
        for record in render_records
        if record.get("output_path")
    }
    parents = {path.parent for path in expected}
    if not expected or len(parents) != 1:
        audit.fail(
            "render_output_inventory",
            "Render root cannot be derived from one shared output directory",
            expected_outputs=sorted(str(path) for path in expected),
            parents=sorted(str(path) for path in parents),
        )
        return
    root = next(iter(parents))
    if not root.is_dir():
        audit.fail(
            "render_output_inventory",
            "Render root is missing",
            root=str(root),
        )
        return
    actual = {
        path.resolve(strict=False)
        for path in root.iterdir()
        if path.is_file() and not path.name.startswith(".")
    }
    missing = sorted(str(path) for path in expected - actual)
    extra = sorted(str(path) for path in actual - expected)
    if missing or extra:
        audit.fail(
            "render_output_inventory",
            "Render root contains missing or undeclared output files",
            root=str(root),
            missing=missing,
            extra=extra,
        )
    else:
        audit.passed("render_output_inventory", root=str(root), count=len(actual))


def validate(args: argparse.Namespace, audit: Audit) -> dict[str, str]:
    paths = {
        "catalog": args.catalog.resolve(strict=False),
        "art_direction": args.art_direction.resolve(strict=False),
        "render_manifest": args.render_manifest.resolve(strict=False),
    }
    documents, hashes = load_inputs(paths, audit)
    if len(documents) != 3:
        return hashes
    catalog = documents["catalog"]
    art = documents["art_direction"]
    manifest = documents["render_manifest"]
    validate_input_bindings(manifest, paths, hashes, audit)
    catalog_map, art_map, render_map = validate_record_sets(
        catalog, art, manifest, audit
    )
    art_records = records(art)
    render_records = records(manifest)
    validate_pair_uniqueness(art_records, render_records, audit)
    validate_contact_sheets(
        manifest, paths["render_manifest"].parent, len(render_records), audit
    )
    validate_output_inventory(render_records, paths["render_manifest"].parent, audit)

    for identifier in [pair_id(record) for record in art_records if pair_id(record)]:
        art_record = art_map.get(identifier)
        render_record = render_map.get(identifier)
        failures_before = len(audit.failures)
        pair_audit: dict[str, Any] = {"asset_id": identifier, "status": "PASS"}
        if art_record is None or render_record is None:
            audit.fail(
                f"pair.{identifier}",
                "Pair is absent from art direction or render manifest",
            )
            pair_audit["status"] = "FAIL"
            audit.pairs.append(pair_audit)
            continue
        art_sources = source_ids(art_record)
        rendered_sources = source_ids(render_record)
        raw_render_sources = render_record.get("sources")
        render_sources = (
            raw_render_sources if isinstance(raw_render_sources, list) else []
        )
        identity_errors: list[str] = []
        if rendered_sources != art_sources:
            identity_errors.append("rendered source_asset_ids changed left/right order")
        if len(render_sources) != 2 or not all(
            isinstance(value, dict) for value in render_sources
        ):
            identity_errors.append(
                "render manifest does not contain exactly two source records"
            )
        elif [
            str(value.get("asset_id", "")) for value in render_sources
        ] != art_sources:
            identity_errors.append("renderer source records changed left/right order")
        if (
            art_record.get("treatment") != "diptych_portrait"
            or render_record.get("treatment") != "diptych_portrait"
        ):
            identity_errors.append(
                "treatment is not diptych_portrait in both manifests"
            )
        if identity_errors:
            audit.fail(
                f"pair.{identifier}.identity",
                "Diptych identity or left/right order changed",
                errors=identity_errors,
                art_direction_source_asset_ids=art_sources,
                render_source_asset_ids=rendered_sources,
            )
        else:
            audit.passed(
                f"pair.{identifier}.identity",
                source_asset_ids=art_sources,
            )
        loaded_sources: list[Image.Image | None] = []
        source_audits: list[dict[str, Any]] = []
        for index in range(2):
            source_identifier = art_sources[index] if index < len(art_sources) else ""
            render_source = (
                render_sources[index]
                if index < len(render_sources)
                and isinstance(render_sources[index], dict)
                else None
            )
            image, source_facts = validate_source(
                identifier,
                index,
                source_identifier,
                catalog_map.get(source_identifier),
                render_source,
                paths["catalog"].parent,
                audit,
            )
            loaded_sources.append(image)
            source_audits.append(source_facts)
        output_path, panel, output_facts = validate_output(
            identifier,
            render_record,
            paths["render_manifest"].parent,
            audit,
        )
        geometry = validate_geometry(
            identifier,
            art_record,
            render_record,
            loaded_sources,
            panel,
            art.get("acknowledge_upscale_risk") is True,
            audit,
        )
        pair_audit.update(
            {
                "source_asset_ids": art_sources,
                "sources": source_audits,
                "output_path": str(output_path) if output_path else None,
                "output": output_facts,
                "geometry": geometry,
            }
        )
        if len(audit.failures) > failures_before:
            pair_audit["status"] = "FAIL"
        audit.pairs.append(pair_audit)
    if len(audit.pairs) != len(render_records):
        audit.fail(
            "pairs_audited",
            "Not every rendered pair was audited end to end",
            rendered_count=len(render_records),
            audited_count=len(audit.pairs),
        )
    else:
        audit.passed("pairs_audited", count=len(audit.pairs))
    return hashes


def main() -> int:
    args = parse_args()
    inputs = {
        "catalog": str(args.catalog.resolve(strict=False)),
        "art_direction": str(args.art_direction.resolve(strict=False)),
        "render_manifest": str(args.render_manifest.resolve(strict=False)),
        "output": str(args.output.resolve(strict=False)),
    }
    audit = Audit(inputs)
    hashes: dict[str, str] = {}
    try:
        hashes = validate(args, audit)
    except Exception as error:
        audit.fail(
            "validator.internal",
            f"Unexpected validator error: {type(error).__name__}: {error}",
        )
    document = audit.document(hashes)
    try:
        atomic_save(args.output, document)
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "message": f"Could not write validation audit: {error}",
                    "audit": document,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2
    print(
        f"{document['status']} portrait-diptych validation: "
        f"{len(audit.pairs)} pairs, {len(audit.failures)} failures; audit={args.output}",
        flush=True,
    )
    return 0 if document["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
