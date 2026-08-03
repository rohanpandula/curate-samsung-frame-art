#!/usr/bin/env python3
"""Validate square diptych proofs and write a machine-readable PASS/FAIL audit."""

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

from PIL import Image, ImageChops, ImageCms, ImageOps

import render_square_diptychs as square_renderer


Image.MAX_IMAGE_PIXELS = 250_000_000
CANVAS = (1920, 1080)
MIN_OUTER_MATTE = 64
MIN_GUTTER = 32


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate rendered 1920x1080 square diptychs and write an audit."
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


def load_object(path: Path) -> tuple[dict[str, Any], str]:
    digest = sha256_file(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value, digest


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


def resolve_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def valid_sha256(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if len(candidate) != 64 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        return ""
    return candidate


def records(document: dict[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("records")
    if not isinstance(raw, list):
        return []
    return [record for record in raw if isinstance(record, dict)]


def asset_id(record: dict[str, Any]) -> str:
    value = record.get("asset_id")
    return str(value).strip() if value is not None else ""


def source_ids(record: dict[str, Any]) -> list[str]:
    raw = record.get("source_asset_ids")
    if not isinstance(raw, list):
        return []
    return [str(value).strip() for value in raw]


def parse_rects(value: Any) -> list[tuple[int, int, int, int]]:
    if not isinstance(value, list):
        return []
    result: list[tuple[int, int, int, int]] = []
    for raw in value:
        if not isinstance(raw, dict):
            return []
        rect = tuple(integer(raw.get(key)) for key in ("x", "y", "width", "height"))
        if any(part is None for part in rect):
            return []
        result.append(rect)  # type: ignore[arg-type]
    return result


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


def open_png(path: Path) -> tuple[Image.Image, dict[str, Any]]:
    with Image.open(path) as opened:
        opened.load()
        profile = opened.info.get("icc_profile")
        facts = {
            "format": str(opened.format or ""),
            "mode": opened.mode,
            "width": opened.width,
            "height": opened.height,
            "srgb_icc_profile": isinstance(profile, bytes)
            and profile_is_srgb(profile),
        }
        return opened.convert("RGB"), facts


class Audit:
    def __init__(self, inputs: dict[str, str]) -> None:
        self.inputs = inputs
        self.checks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.pairs: list[dict[str, Any]] = []

    def pass_check(self, name: str, **details: Any) -> None:
        self.checks.append({"name": name, "status": "PASS", **details})

    def fail(self, name: str, message: str, **details: Any) -> None:
        failure = {"name": name, "message": message, **details}
        self.failures.append(failure)
        self.checks.append({"status": "FAIL", **failure})

    def document(self, hashes: dict[str, str]) -> dict[str, Any]:
        return {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": "FAIL" if self.failures else "PASS",
            "inputs": self.inputs,
            "input_sha256": hashes,
            "summary": {
                "checks_passed": sum(
                    check["status"] == "PASS" for check in self.checks
                ),
                "checks_failed": len(self.failures),
                "pairs_audited": len(self.pairs),
            },
            "checks": self.checks,
            "failures": self.failures,
            "pairs": self.pairs,
        }


def validate_input_bindings(
    manifest: dict[str, Any], paths: dict[str, Path], hashes: dict[str, str], audit: Audit
) -> None:
    problems: dict[str, Any] = {}
    for label, prefix in (("catalog", "catalog"), ("art_direction", "art_direction")):
        raw_path = manifest.get(f"{prefix}_path")
        recorded_path = (
            resolve_path(raw_path, paths["render_manifest"].parent) if raw_path else None
        )
        recorded_hash = valid_sha256(manifest.get(f"{prefix}_sha256"))
        if recorded_path != paths[label] or recorded_hash != hashes.get(label):
            problems[label] = {
                "recorded_path": str(recorded_path) if recorded_path else None,
                "actual_path": str(paths[label]),
                "recorded_sha256": recorded_hash or None,
                "actual_sha256": hashes.get(label),
            }
    if problems:
        audit.fail(
            "render_manifest.input_bindings",
            "Render manifest is not bound to these exact catalog and art-direction files",
            problems=problems,
        )
    else:
        audit.pass_check("render_manifest.input_bindings")


def validate_record_sets(
    catalog: dict[str, Any],
    art: dict[str, Any],
    manifest: dict[str, Any],
    audit: Audit,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    catalog_records = records(catalog)
    art_records = records(art)
    render_records = records(manifest)
    catalog_ids = [asset_id(record) for record in catalog_records]
    art_ids = [asset_id(record) for record in art_records]
    render_ids = [asset_id(record) for record in render_records]
    problems: dict[str, Any] = {}
    for label, raw, clean, identifiers in (
        ("catalog", catalog.get("records"), catalog_records, catalog_ids),
        ("art_direction", art.get("records"), art_records, art_ids),
        ("render_manifest", manifest.get("records"), render_records, render_ids),
    ):
        if not isinstance(raw, list) or len(raw) != len(clean):
            problems[f"{label}_malformed_records"] = True
        if not identifiers or "" in identifiers:
            problems[f"{label}_missing_ids"] = True
        duplicates = sorted(
            {value for value in identifiers if value and identifiers.count(value) > 1}
        )
        if duplicates:
            problems[f"{label}_duplicate_ids"] = duplicates
    if catalog.get("status") != "complete":
        problems["catalog_status"] = catalog.get("status")
    if manifest.get("status") != "complete":
        problems["render_manifest_status"] = manifest.get("status")
    for label, document in (
        ("catalog", catalog),
        ("art_direction", art),
        ("render_manifest", manifest),
    ):
        if document.get("schema_version") != 1:
            problems[f"{label}_schema_version"] = document.get("schema_version")
    if manifest.get("private_artifact") is not True:
        problems["render_manifest_private_artifact"] = manifest.get(
            "private_artifact"
        )
    if catalog.get("record_count") != len(catalog_records):
        problems["catalog_record_count"] = catalog.get("record_count")
    if manifest.get("record_count") != len(render_records):
        problems["render_manifest_record_count"] = manifest.get("record_count")
    if art_ids != render_ids:
        problems["pair_id_order"] = {
            "art_direction": art_ids,
            "render_manifest": render_ids,
        }
    if manifest.get("acknowledge_upscale_risk") is not (
        art.get("acknowledge_upscale_risk") is True
    ):
        problems["acknowledge_upscale_risk"] = "changed between manifests"
    try:
        square_renderer.validate_canvas(art)
    except Exception as error:
        problems["canvas"] = str(error)
    if problems:
        audit.fail(
            "record_sets",
            "Catalog, art direction, or render records are incomplete, malformed, or out of order",
            problems=problems,
        )
    else:
        audit.pass_check(
            "record_sets", catalog_count=len(catalog_records), pair_order=art_ids
        )
    return (
        {asset_id(record): record for record in catalog_records if asset_id(record)},
        {asset_id(record): record for record in art_records if asset_id(record)},
        {asset_id(record): record for record in render_records if asset_id(record)},
    )


def validate_pair_definitions(
    art_records: list[dict[str, Any]], render_records: list[dict[str, Any]], audit: Audit
) -> None:
    invalid: dict[str, Any] = {}
    unordered: list[tuple[str, str]] = []
    for record in art_records:
        identifier = asset_id(record) or "<missing>"
        identifiers = source_ids(record)
        if (
            record.get("treatment") != "diptych_square"
            or len(identifiers) != 2
            or not all(identifiers)
            or identifiers[0] == identifiers[1]
        ):
            invalid[identifier] = {
                "treatment": record.get("treatment"),
                "source_asset_ids": identifiers,
            }
        else:
            unordered.append(tuple(sorted(identifiers)))
    duplicate_pairs = sorted(
        {pair for pair in unordered if unordered.count(pair) > 1}
    )
    output_paths = [str(record.get("output_path", "")) for record in render_records]
    duplicate_outputs = sorted(
        {path for path in output_paths if path and output_paths.count(path) > 1}
    )
    if invalid or duplicate_pairs or duplicate_outputs or "" in output_paths:
        audit.fail(
            "pair_definitions",
            "Every square diptych needs two distinct sources and a unique pair and output",
            invalid_pairs=invalid,
            duplicate_source_pairs=[list(pair) for pair in duplicate_pairs],
            duplicate_output_paths=duplicate_outputs,
        )
    else:
        audit.pass_check("pair_definitions", pair_count=len(art_records))


def validate_source(
    pair: str,
    side: int,
    identifier: str,
    catalog_record: dict[str, Any] | None,
    render_source: dict[str, Any] | None,
    catalog_base: Path,
    audit: Audit,
) -> tuple[Image.Image | None, dict[str, Any]]:
    label = "left" if side == 0 else "right"
    facts: dict[str, Any] = {"asset_id": identifier, "side": label}
    errors: list[str] = []
    if catalog_record is None or render_source is None:
        errors.append("source is absent from the catalog or renderer source list")
        audit.fail(
            f"pair.{pair}.source.{label}", "Square source verification failed", errors=errors
        )
        return None, facts
    raw_path = catalog_record.get("source_path")
    path = resolve_path(raw_path, catalog_base) if raw_path else None
    expected_hash = valid_sha256(catalog_record.get("source_sha256"))
    image: Image.Image | None = None
    actual_hash = ""
    if path is None or not path.is_file():
        errors.append("catalog source file is missing")
    else:
        try:
            actual_hash = sha256_file(path)
            image = open_source(path)
        except Exception as error:
            errors.append(f"source cannot be hashed or decoded: {error}")
    if not expected_hash or actual_hash != expected_hash:
        errors.append("catalog source SHA-256 is missing or changed")
    expected_size = (integer(catalog_record.get("width")), integer(catalog_record.get("height")))
    if image is not None and expected_size != image.size:
        errors.append("catalog source dimensions changed")
    if image is not None and image.width != image.height:
        errors.append("decoded source is not exactly 1:1")
    if catalog_record.get("orientation") != "square":
        errors.append("catalog orientation is not square")

    render_path_raw = render_source.get("source_path")
    render_path = resolve_path(render_path_raw, catalog_base) if render_path_raw else None
    if str(render_source.get("asset_id", "")) != identifier:
        errors.append("renderer source ID or order changed")
    if path is not None and render_path != path:
        errors.append("renderer source path differs from catalog")
    if valid_sha256(render_source.get("source_sha256")) != actual_hash:
        errors.append("renderer source hash differs from catalog source")
    if image is not None and (
        integer(render_source.get("source_width")),
        integer(render_source.get("source_height")),
    ) != image.size:
        errors.append("renderer source dimensions differ from decoded source")
    if render_source.get("orientation") != "square":
        errors.append("renderer source orientation is not square")
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
            f"pair.{pair}.source.{label}",
            "Square source verification failed",
            errors=errors,
            **facts,
        )
    else:
        audit.pass_check(f"pair.{pair}.source.{label}", **facts)
    return image, facts


def validate_output(
    pair: str, record: dict[str, Any], manifest_base: Path, audit: Audit
) -> tuple[Path | None, Image.Image | None, dict[str, Any]]:
    raw_path = record.get("output_path")
    path = resolve_path(raw_path, manifest_base) if raw_path else None
    expected_hash = valid_sha256(record.get("output_sha256"))
    panel: Image.Image | None = None
    actual_hash = ""
    facts: dict[str, Any] = {"path": str(path) if path else None}
    errors: list[str] = []
    if path is None or not path.is_file():
        errors.append("rendered output is missing")
    else:
        try:
            actual_hash = sha256_file(path)
            panel, image_facts = open_png(path)
            facts.update(image_facts)
        except Exception as error:
            errors.append(f"rendered output cannot be hashed or decoded: {error}")
    facts["sha256"] = actual_hash or None
    if not expected_hash or actual_hash != expected_hash:
        errors.append("output SHA-256 is missing or changed")
    if panel is not None and (
        facts.get("format") != "PNG"
        or facts.get("mode") != "RGB"
        or panel.size != CANVAS
        or not facts.get("srgb_icc_profile")
    ):
        errors.append("output is not an RGB 1920x1080 PNG with an sRGB ICC profile")
    if (
        integer(record.get("output_width")) != CANVAS[0]
        or integer(record.get("output_height")) != CANVAS[1]
        or record.get("output_mode") != "RGB"
        or record.get("color_space") != "sRGB"
    ):
        errors.append("output metadata differs from the decoded panel")
    if path is not None and path.name != f"{pair}__1920x1080.png":
        errors.append("output filename does not bind to the pair ID and canvas")
    if errors:
        audit.fail(
            f"pair.{pair}.output", "Rendered square diptych failed verification", errors=errors, **facts
        )
    else:
        audit.pass_check(f"pair.{pair}.output", **facts)
    return path, panel, facts


def expected_geometry(art_record: dict[str, Any]) -> dict[str, Any]:
    raw = art_record.get("outer_margins")
    if not isinstance(raw, dict):
        raise ValueError("outer_margins is missing")
    margins = {key: integer(raw.get(key)) for key in ("left", "right", "top", "bottom")}
    if any(value is None for value in margins.values()):
        raise ValueError("outer_margins contains a non-integer")
    clean_margins: dict[str, int] = margins  # type: ignore[assignment]
    if min(clean_margins.values()) < MIN_OUTER_MATTE:
        raise ValueError("outer_margins is below 64 px")
    gutter = integer(art_record.get("gutter", 64))
    if gutter is None or gutter < MIN_GUTTER:
        raise ValueError("gutter is missing or below 32 px")
    available_width = CANVAS[0] - clean_margins["left"] - clean_margins["right"] - gutter
    available_height = CANVAS[1] - clean_margins["top"] - clean_margins["bottom"]
    side = min(available_height, available_width // 2)
    if side <= 0:
        raise ValueError("margins and gutter leave no image area")
    bias = number(art_record.get("vertical_bias", 0.0))
    if bias is None:
        raise ValueError("vertical_bias is not finite")
    usable_width = CANVAS[0] - clean_margins["left"] - clean_margins["right"]
    group_width = side * 2 + gutter
    x = clean_margins["left"] + (usable_width - group_width) // 2
    y = round((CANVAS[1] - side) / 2 + bias * CANVAS[1])
    y = max(clean_margins["top"], min(CANVAS[1] - clean_margins["bottom"] - side, y))
    rects = [(x, y, side, side), (x + side + gutter, y, side, side)]
    actual = {
        "left": x,
        "right": CANVAS[0] - rects[1][0] - side,
        "top": y,
        "bottom": CANVAS[1] - y - side,
    }
    return {
        "declared_outer_margins": clean_margins,
        "actual_outer_margins": actual,
        "gutter": gutter,
        "square_size": side,
        "rects": rects,
    }


def validate_composition(
    pair: str,
    art_record: dict[str, Any],
    render_record: dict[str, Any],
    sources: list[Image.Image | None],
    panel: Image.Image | None,
    upscale_acknowledged: bool,
    audit: Audit,
) -> dict[str, Any]:
    errors: list[str] = []
    facts: dict[str, Any] = {}
    try:
        geometry = expected_geometry(art_record)
    except Exception as error:
        geometry = {}
        errors.append(str(error))
    rects = parse_rects(render_record.get("image_rects"))
    facts["image_rects"] = [list(rect) for rect in rects]
    facts.update(geometry)
    if geometry and rects != geometry["rects"]:
        errors.append("image rectangles differ from the independently calculated layout")
    if len(rects) != 2:
        errors.append("manifest does not contain exactly two integer image rectangles")
    else:
        left, right = rects
        if left[2] != left[3] or right[2] != right[3] or left[2:] != right[2:]:
            errors.append("the two rendered images are not equal-size squares")
        if left[1] != right[1] or left[0] + left[2] >= right[0]:
            errors.append("the square panels are misaligned, reversed, or overlapping")
        if any(
            x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > CANVAS[0] or y + height > CANVAS[1]
            for x, y, width, height in rects
        ):
            errors.append("one or both image rectangles fall outside the panel")

    if geometry:
        if render_record.get("declared_outer_margins") != geometry["declared_outer_margins"]:
            errors.append("declared outer margins changed in the render manifest")
        if render_record.get("actual_outer_margins") != geometry["actual_outer_margins"]:
            errors.append("actual outer margins differ from the rendered geometry")
        if integer(render_record.get("gutter")) != geometry["gutter"]:
            errors.append("rendered gutter differs from art direction")
        if integer(render_record.get("square_size")) != geometry["square_size"]:
            errors.append("recorded square size differs from the rendered geometry")
    if (
        number(art_record.get("max_crop_fraction", 0.0)) != 0.0
        or number(render_record.get("max_crop_fraction")) != 0.0
        or number(render_record.get("crop_fraction")) != 0.0
        or render_record.get("complete_sources") is not True
        or render_record.get("equal_size") is not True
    ):
        errors.append("square diptych is not recorded as complete, equal-size, and uncropped")

    expected_panel: Image.Image | None = None
    normalized: dict[str, Any] = {}
    if len(sources) == 2 and all(source is not None for source in sources):
        try:
            expected_panel, normalized = square_renderer.compose_pair(
                art_record,
                [source for source in sources if source is not None],
                upscale_acknowledged,
            )
        except Exception as error:
            errors.append(f"art direction cannot be deterministically rendered: {error}")
    if normalized:
        for field in (
            "matte_hex",
            "declared_outer_margins",
            "actual_outer_margins",
            "gutter",
            "square_size",
            "keyline",
            "shadow",
            "allow_upscale",
        ):
            if render_record.get(field) != normalized.get(field):
                errors.append(f"rendered {field} differs from normalized art direction")
        expected_scales = [round(value, 8) for value in normalized["scale_factors"]]
        actual_scales = render_record.get("scale_factors")
        if not isinstance(actual_scales, list) or len(actual_scales) != 2 or any(
            number(actual) is None or abs(float(actual) - expected) > 1e-8
            for actual, expected in zip(
                actual_scales if isinstance(actual_scales, list) else [],
                expected_scales,
            )
        ):
            errors.append("recorded scale factors differ from source geometry")
        facts["scale_factors"] = expected_scales
        if art_record.get("keyline") is None and normalized["keyline"] != {"enabled": False}:
            errors.append("omitted keyline did not default to off")
    if panel is not None and expected_panel is not None:
        difference = ImageChops.difference(panel, expected_panel)
        facts["pixel_exact_match"] = difference.getbbox() is None
        if difference.getbbox() is not None:
            errors.append("panel pixels differ from the deterministic square-diptych render")

    if errors:
        audit.fail(
            f"pair.{pair}.composition",
            "Square-diptych composition validation failed",
            errors=errors,
            **facts,
        )
    else:
        audit.pass_check(f"pair.{pair}.composition", **facts)
    return facts


def validate_contact_sheets(
    manifest: dict[str, Any], manifest_base: Path, render_records: list[dict[str, Any]], audit: Audit
) -> None:
    raw = manifest.get("contact_sheets")
    expected_count = math.ceil(len(render_records) / 2)
    errors: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    if not isinstance(raw, list) or len(raw) != expected_count:
        audit.fail(
            "contact_sheets",
            "Contact-sheet count does not match two pairs per page",
            expected_count=expected_count,
            actual_count=len(raw) if isinstance(raw, list) else None,
        )
        return
    paths: list[Path] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or not entry.get("path"):
            errors.append({"index": index, "error": "invalid manifest entry"})
            continue
        path = resolve_path(entry["path"], manifest_base)
        item: dict[str, Any] = {"index": index, "path": str(path)}
        if path.name != f"square-diptych-contact-sheet-{index + 1:02d}.png":
            errors.append({**item, "error": "unexpected contact-sheet filename"})
        if not path.is_file():
            errors.append({**item, "error": "contact sheet is missing"})
            continue
        try:
            digest = sha256_file(path)
            sheet, image_facts = open_png(path)
            item.update(image_facts)
            item["sha256"] = digest
        except Exception as error:
            errors.append({**item, "error": f"cannot hash or decode: {error}"})
            continue
        if valid_sha256(entry.get("sha256")) != digest:
            errors.append({**item, "error": "contact-sheet hash differs from manifest"})
        if (
            sheet.size != (1840, 1140)
            or image_facts["format"] != "PNG"
            or image_facts["mode"] != "RGB"
            or not image_facts["srgb_icc_profile"]
            or integer(entry.get("width")) != 1840
            or integer(entry.get("height")) != 1140
        ):
            errors.append({**item, "error": "contact-sheet format or dimensions are invalid"})
        for column, record in enumerate(render_records[index * 2 : index * 2 + 2]):
            output_path = resolve_path(record.get("output_path"), manifest_base)
            if not output_path.is_file():
                continue
            panel, _ = open_png(output_path)
            panel.thumbnail((880, 990), Image.Resampling.LANCZOS)
            x = 20 + column * 910 + (880 - panel.width) // 2
            y = 30 + (990 - panel.height) // 2
            actual = sheet.crop((x, y, x + panel.width, y + panel.height))
            if ImageChops.difference(panel, actual).getbbox() is not None:
                errors.append({**item, "error": f"preview {column + 1} differs from its panel"})
        paths.append(path)
        facts.append(item)
    if len(paths) != len(set(paths)):
        errors.append({"error": "contact-sheet paths are duplicated"})
    if paths:
        parent_dirs = {path.parent for path in paths}
        if len(parent_dirs) == 1:
            actual_files = {
                path.resolve(strict=False)
                for path in next(iter(parent_dirs)).iterdir()
                if path.is_file() and not path.name.startswith(".")
            }
            if actual_files != set(paths):
                errors.append({"error": "contact-sheet directory has undeclared files"})
    if errors:
        audit.fail(
            "contact_sheets",
            "One or more contact sheets failed validation",
            errors=errors,
            valid=facts,
        )
    else:
        audit.pass_check("contact_sheets", count=len(facts), sheets=facts)


def validate_output_inventory(
    render_records: list[dict[str, Any]], manifest_base: Path, audit: Audit
) -> None:
    expected = {
        resolve_path(record["output_path"], manifest_base)
        for record in render_records
        if record.get("output_path")
    }
    parents = {path.parent for path in expected}
    if not expected or len(parents) != 1 or not next(iter(parents)).is_dir():
        audit.fail(
            "render_output_inventory",
            "One shared rendered-output directory could not be verified",
            expected=sorted(str(path) for path in expected),
        )
        return
    root = next(iter(parents))
    actual = {
        path.resolve(strict=False)
        for path in root.iterdir()
        if path.is_file() and not path.name.startswith(".")
    }
    if actual != expected:
        audit.fail(
            "render_output_inventory",
            "Rendered-output directory contains missing or undeclared files",
            missing=sorted(str(path) for path in expected - actual),
            extra=sorted(str(path) for path in actual - expected),
        )
    else:
        audit.pass_check("render_output_inventory", root=str(root), count=len(actual))


def validate(args: argparse.Namespace, audit: Audit) -> dict[str, str]:
    paths = {
        "catalog": args.catalog.resolve(strict=False),
        "art_direction": args.art_direction.resolve(strict=False),
        "render_manifest": args.render_manifest.resolve(strict=False),
    }
    documents: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for label, path in paths.items():
        try:
            documents[label], hashes[label] = load_object(path)
            audit.pass_check(f"input.{label}", path=str(path), sha256=hashes[label])
        except Exception as error:
            audit.fail(f"input.{label}", f"Cannot hash or read JSON input: {error}")
    if len(documents) != 3:
        return hashes
    catalog = documents["catalog"]
    art = documents["art_direction"]
    manifest = documents["render_manifest"]
    validate_input_bindings(manifest, paths, hashes, audit)
    catalog_map, art_map, render_map = validate_record_sets(catalog, art, manifest, audit)
    art_records = records(art)
    render_records = records(manifest)
    validate_pair_definitions(art_records, render_records, audit)
    validate_contact_sheets(manifest, paths["render_manifest"].parent, render_records, audit)
    validate_output_inventory(render_records, paths["render_manifest"].parent, audit)

    for identifier in [asset_id(record) for record in art_records if asset_id(record)]:
        pair_errors_before = len(audit.failures)
        art_record = art_map.get(identifier)
        render_record = render_map.get(identifier)
        pair_audit: dict[str, Any] = {"asset_id": identifier, "status": "PASS"}
        if art_record is None or render_record is None:
            audit.fail(f"pair.{identifier}", "Pair is missing from one manifest")
            pair_audit["status"] = "FAIL"
            audit.pairs.append(pair_audit)
            continue
        art_sources = source_ids(art_record)
        rendered_sources = source_ids(render_record)
        raw_render_sources = render_record.get("sources")
        render_sources = raw_render_sources if isinstance(raw_render_sources, list) else []
        identity_errors: list[str] = []
        if rendered_sources != art_sources:
            identity_errors.append("source_asset_ids changed or were reordered")
        if len(render_sources) != 2 or not all(isinstance(value, dict) for value in render_sources):
            identity_errors.append("render manifest does not contain two source records")
        elif [str(value.get("asset_id", "")) for value in render_sources] != art_sources:
            identity_errors.append("renderer source records changed left-to-right order")
        if art_record.get("treatment") != "diptych_square" or render_record.get("treatment") != "diptych_square":
            identity_errors.append("treatment is not diptych_square in both manifests")
        if identity_errors:
            audit.fail(
                f"pair.{identifier}.identity",
                "Square-diptych identity or source order changed",
                errors=identity_errors,
            )
        else:
            audit.pass_check(f"pair.{identifier}.identity", source_asset_ids=art_sources)

        loaded: list[Image.Image | None] = []
        source_facts: list[dict[str, Any]] = []
        for index in range(2):
            source_identifier = art_sources[index] if index < len(art_sources) else ""
            render_source = (
                render_sources[index]
                if index < len(render_sources) and isinstance(render_sources[index], dict)
                else None
            )
            image, facts = validate_source(
                identifier,
                index,
                source_identifier,
                catalog_map.get(source_identifier),
                render_source,
                paths["catalog"].parent,
                audit,
            )
            loaded.append(image)
            source_facts.append(facts)
        output_path, panel, output_facts = validate_output(
            identifier, render_record, paths["render_manifest"].parent, audit
        )
        composition = validate_composition(
            identifier,
            art_record,
            render_record,
            loaded,
            panel,
            art.get("acknowledge_upscale_risk") is True,
            audit,
        )
        pair_audit.update(
            {
                "source_asset_ids": art_sources,
                "sources": source_facts,
                "output_path": str(output_path) if output_path else None,
                "output": output_facts,
                "composition": composition,
            }
        )
        if len(audit.failures) > pair_errors_before:
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
        audit.pass_check("pairs_audited", count=len(audit.pairs))
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
        print(f"FAIL could not write validation audit: {error}", flush=True)
        return 2
    print(
        f"{document['status']} square-diptych validation: "
        f"{len(audit.pairs)} pairs, {len(audit.failures)} failures; audit={args.output}",
        flush=True,
    )
    return 0 if document["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
