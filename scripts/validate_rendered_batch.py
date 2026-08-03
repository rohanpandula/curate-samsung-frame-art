#!/usr/bin/env python3
"""Fail-closed validation for a Frame Art Curator render batch.

The validator deliberately re-reads the catalog, art-direction manifest, render
manifest, every source/current/rendered image, and every comparison contact
sheet.  It writes a machine-readable audit even when validation fails.
"""

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
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageCms, ImageOps, ImageStat


Image.MAX_IMAGE_PIXELS = 250_000_000
PANEL_SIZE = (1920, 1080)
DEFAULT_FAMILY_LIMIT = 5
EDGE_NAMES = {"left", "right", "top", "bottom"}
FRAMED_TREATMENTS = {
    "float_pano",
    "museum_light",
    "museum_dark",
    "minimal_crop",
    "soft_extension",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a rendered Samsung Frame batch and write a PASS/FAIL audit."
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


def resolve_path(value: Any, base: Path) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


def load_json(path: Path) -> tuple[dict[str, Any], str]:
    digest = sha256_file(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value, digest


def first_value(record: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def record_id(record: dict[str, Any]) -> str:
    value = first_value(record, ("content_id", "asset_id", "record_id"))
    return str(value).strip() if value is not None else ""


def treatment(record: dict[str, Any]) -> str:
    value = first_value(record, ("treatment", "design_family"))
    return str(value).strip() if value is not None else ""


def protection_fields(record: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    raw_edges = record.get("protected_edges", [])
    edges: list[str] = []
    if not isinstance(raw_edges, list):
        errors.append("protected_edges must be a list")
    else:
        for index, value in enumerate(raw_edges):
            if not isinstance(value, str):
                errors.append(f"protected_edges[{index}] must be a string")
            elif value not in EDGE_NAMES:
                errors.append(
                    f"protected_edges[{index}] must be exactly one of left/right/top/bottom"
                )
            else:
                edges.append(value)

    raw_subjects = record.get("protected_subjects", [])
    subjects: list[str] = []
    if not isinstance(raw_subjects, list):
        errors.append("protected_subjects must be a list when present")
    else:
        for index, value in enumerate(raw_subjects):
            if not isinstance(value, str) or not value.strip():
                errors.append(
                    f"protected_subjects[{index}] must be a non-empty informational string"
                )
            else:
                subjects.append(value)
    return edges, subjects, errors


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rendered_upscale_factor(record: dict[str, Any]) -> float | None:
    direct = as_number(record.get("upscale_factor"))
    if direct is not None:
        return direct
    scale = record.get("scale")
    if isinstance(scale, dict):
        axes = [as_number(scale.get(axis)) for axis in ("x", "y")]
        if all(value is not None for value in axes):
            return max(value for value in axes if value is not None)
    return as_number(scale)


def expected_hash(record: dict[str, Any], names: Iterable[str]) -> str:
    value = first_value(record, names)
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if len(candidate) != 64 or any(
        char not in "0123456789abcdef" for char in candidate
    ):
        return ""
    return candidate


def dimensions(record: dict[str, Any], prefix: str) -> tuple[int, int] | None:
    width = as_number(record.get(f"{prefix}_width"))
    height = as_number(record.get(f"{prefix}_height"))
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    if not width.is_integer() or not height.is_integer():
        return None
    return int(width), int(height)


def source_dimensions(record: dict[str, Any]) -> tuple[int, int] | None:
    """Accept audit-backed source_* dimensions and portable catalog width/height."""
    prefixed = dimensions(record, "source")
    if prefixed is not None:
        return prefixed
    width = as_number(record.get("width"))
    height = as_number(record.get("height"))
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    if not width.is_integer() or not height.is_integer():
        return None
    return int(width), int(height)


def extract_records(
    label: str,
    document: dict[str, Any],
    audit: "Audit",
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    raw = document.get("records")
    if not isinstance(raw, list):
        audit.fail(f"{label}.records", "Top-level 'records' must be a list")
        return [], {}
    records: list[dict[str, Any]] = []
    ids: list[str] = []
    malformed: list[int] = []
    conflicting: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            malformed.append(index)
            continue
        records.append(value)
        ids.append(record_id(value))
        declared = {
            key: str(value[key]).strip()
            for key in ("content_id", "asset_id", "record_id")
            if value.get(key) is not None and str(value[key]).strip()
        }
        if len(set(declared.values())) > 1:
            conflicting.append({"index": index, "identities": declared})
    missing = [index for index, value in enumerate(ids) if not value]
    duplicates = sorted({value for value in ids if value and ids.count(value) > 1})
    if malformed or missing or duplicates or conflicting:
        audit.fail(
            f"{label}.record_ids",
            "Records need one unique identity via content_id, asset_id, or record_id",
            malformed_indices=malformed,
            missing_id_indices=missing,
            duplicate_ids=duplicates,
            conflicting_identity_fields=conflicting,
        )
    else:
        audit.pass_check(f"{label}.record_ids", count=len(records))
    mapping = {record_id(value): value for value in records if record_id(value)}
    return records, mapping


def inspect_oriented(path: Path) -> tuple[tuple[int, int], str, str]:
    with Image.open(path) as image:
        image_format = str(image.format or "")
        oriented = ImageOps.exif_transpose(image)
        oriented.load()
        return oriented.size, oriented.mode, image_format


def icc_is_srgb(profile_bytes: bytes) -> bool:
    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(profile_bytes))
        description = ImageCms.getProfileDescription(profile)
        name = ImageCms.getProfileName(profile)
    except Exception:
        return False
    return "srgb" in f"{description} {name}".lower()


def inspect_rendered(path: Path) -> tuple[dict[str, Any], Image.Image]:
    with Image.open(path) as source:
        source.load()
        info = dict(source.info)
        profile = info.get("icc_profile")
        explicit_srgb = "srgb" in {str(key).lower() for key in info}
        if isinstance(profile, bytes):
            explicit_srgb = explicit_srgb or icc_is_srgb(profile)
        facts = {
            "format": str(source.format or ""),
            "mode": source.mode,
            "width": source.width,
            "height": source.height,
            "explicit_srgb": explicit_srgb,
            "icc_profile_bytes": len(profile) if isinstance(profile, bytes) else 0,
        }
        return facts, source.convert("RGB")


def parse_rect(value: Any) -> tuple[int, int, int, int] | None:
    x: float | None
    y: float | None
    width: float | None
    height: float | None
    if isinstance(value, (list, tuple)) and len(value) == 4:
        x, y, width, height = (as_number(item) for item in value)
    elif isinstance(value, dict):
        x = as_number(first_value(value, ("x", "left")))
        y = as_number(first_value(value, ("y", "top")))
        width = as_number(first_value(value, ("width", "w")))
        height = as_number(first_value(value, ("height", "h")))
        if width is None and x is not None:
            right = as_number(value.get("right"))
            width = right - x if right is not None else None
        if height is None and y is not None:
            bottom = as_number(value.get("bottom"))
            height = bottom - y if bottom is not None else None
    else:
        return None
    numbers = (x, y, width, height)
    if any(number is None or not number.is_integer() for number in numbers):
        return None
    return tuple(int(number) for number in numbers)  # type: ignore[arg-type,return-value]


def rect_from_record(record: dict[str, Any]) -> tuple[int, int, int, int] | None:
    value = first_value(
        record,
        ("image_rect", "photo_rect", "sharp_image_rect", "inner_rect"),
    )
    return parse_rect(value)


def crop_fraction_for_rect(
    source_size: tuple[int, int], rect: tuple[int, int, int, int]
) -> float:
    source_ratio = source_size[0] / source_size[1]
    rect_ratio = rect[2] / rect[3]
    if source_ratio > rect_ratio:
        kept = rect_ratio / source_ratio
    else:
        kept = source_ratio / rect_ratio
    return max(0.0, 1.0 - min(1.0, kept))


def strip_difference(first: Image.Image, second: Image.Image) -> float:
    difference = ImageChops.difference(first, second)
    means = ImageStat.Stat(difference).mean
    return sum(means) / len(means) if means else 0.0


def boundary_contrast(image: Image.Image, rect: tuple[int, int, int, int]) -> float:
    """Return the strongest mean pixel discontinuity around a declared photo edge."""
    x, y, width, height = rect
    right = x + width
    bottom = y + height
    inset_x = max(1, min(width // 10, 80))
    inset_y = max(1, min(height // 10, 80))
    comparisons: list[float] = []
    if x >= 2 and height - 2 * inset_y > 0:
        edge = image.crop((x, y + inset_y, x + 1, bottom - inset_y))
        outside = image.crop((x - 2, y + inset_y, x - 1, bottom - inset_y))
        comparisons.append(strip_difference(edge, outside))
    if right + 2 <= image.width and height - 2 * inset_y > 0:
        edge = image.crop((right - 1, y + inset_y, right, bottom - inset_y))
        outside = image.crop((right + 1, y + inset_y, right + 2, bottom - inset_y))
        comparisons.append(strip_difference(edge, outside))
    if y >= 2 and width - 2 * inset_x > 0:
        edge = image.crop((x + inset_x, y, right - inset_x, y + 1))
        outside = image.crop((x + inset_x, y - 2, right - inset_x, y - 1))
        comparisons.append(strip_difference(edge, outside))
    if bottom + 2 <= image.height and width - 2 * inset_x > 0:
        edge = image.crop((x + inset_x, bottom - 1, right - inset_x, bottom))
        outside = image.crop((x + inset_x, bottom + 1, right - inset_x, bottom + 2))
        comparisons.append(strip_difference(edge, outside))
    return max(comparisons, default=0.0)


def family_limit(document: dict[str, Any]) -> tuple[int, bool, str]:
    for key in (
        "design_family_limit_override",
        "max_design_families_override",
        "max_design_families",
    ):
        candidate = as_number(document.get(key))
        if (
            candidate is not None
            and candidate.is_integer()
            and candidate > DEFAULT_FAMILY_LIMIT
        ):
            return int(candidate), True, key
    for key in (
        "allow_more_than_five_design_families",
        "allow_more_than_five_families",
    ):
        if document.get(key) is True:
            return 2**31 - 1, True, key
    return DEFAULT_FAMILY_LIMIT, False, "default"


def contact_sheet_entries(document: dict[str, Any]) -> list[Any]:
    explicit = document.get("comparison_contact_sheets")
    if isinstance(explicit, list):
        return explicit
    generic = document.get("contact_sheets")
    if not isinstance(generic, list):
        return []
    dictionaries = [value for value in generic if isinstance(value, dict)]
    if dictionaries:
        return [
            value
            for value in dictionaries
            if "comparison" in str(value.get("kind", "comparison")).lower()
        ]
    return generic


def entry_path(entry: Any) -> Any:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return first_value(entry, ("path", "output_path", "file"))
    return None


class Audit:
    def __init__(self, inputs: dict[str, str]) -> None:
        self.inputs = inputs
        self.checks: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    def pass_check(self, name: str, **details: Any) -> None:
        self.checks.append({"name": name, "status": "PASS", **details})

    def fail(self, name: str, message: str, **details: Any) -> None:
        failure = {"name": name, "message": message, **details}
        self.failures.append(failure)
        self.checks.append(
            {"name": name, "status": "FAIL", "message": message, **details}
        )

    def warn(self, name: str, message: str, **details: Any) -> None:
        warning = {"name": name, "message": message, **details}
        self.warnings.append(warning)

    def document(self, manifest_hashes: dict[str, str] | None = None) -> dict[str, Any]:
        status = "FAIL" if self.failures else "PASS"
        return {
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "status": status,
            "inputs": self.inputs,
            "input_sha256": manifest_hashes or {},
            "summary": {
                "checks_passed": sum(
                    check["status"] == "PASS" for check in self.checks
                ),
                "checks_failed": len(self.failures),
                "warnings": len(self.warnings),
                "records_audited": len(self.records),
            },
            "checks": self.checks,
            "failures": self.failures,
            "warnings": self.warnings,
            "records": self.records,
        }


def validate_catalog_record(
    content_id: str,
    record: dict[str, Any],
    render_record: dict[str, Any],
    catalog_base: Path,
    audit: Audit,
) -> tuple[Path | None, tuple[int, int] | None, str]:
    source_raw = record.get("source_path")
    source_expected = expected_hash(record, ("source_sha256",))
    expected_source_dimensions = source_dimensions(record)
    source_path = resolve_path(source_raw, catalog_base) if source_raw else None
    source_actual_hash = ""
    source_actual_dimensions: tuple[int, int] | None = None
    source_error = ""
    if source_path is None or not source_path.is_file():
        source_error = f"Missing catalog source: {source_path}"
    elif not source_expected or expected_source_dimensions is None:
        source_error = "Catalog source hash/dimensions are missing or invalid"
    else:
        try:
            source_actual_hash = sha256_file(source_path)
            source_actual_dimensions, _, _ = inspect_oriented(source_path)
        except Exception as exc:
            source_error = f"Cannot read catalog source: {exc}"
        if not source_error and source_actual_hash != source_expected:
            source_error = "Catalog source SHA-256 changed"
        if not source_error and source_actual_dimensions != expected_source_dimensions:
            source_error = "Catalog source dimensions changed"
    if source_error:
        audit.fail(
            f"record.{content_id}.catalog_source",
            source_error,
            path=str(source_path) if source_path else None,
            expected_sha256=source_expected or None,
            actual_sha256=source_actual_hash or None,
            expected_dimensions=(
                list(expected_source_dimensions) if expected_source_dimensions else None
            ),
            actual_dimensions=list(source_actual_dimensions)
            if source_actual_dimensions
            else None,
        )
    else:
        audit.pass_check(
            f"record.{content_id}.catalog_source",
            path=str(source_path),
            sha256=source_actual_hash,
            dimensions=list(source_actual_dimensions or ()),
        )

    payload_path_declared = "current_payload_path" in record
    payload_hash_declared = "current_payload_sha256" in record
    comparison_baseline = str(render_record.get("comparison_baseline", "")).strip()
    if payload_path_declared != payload_hash_declared:
        audit.fail(
            f"record.{content_id}.catalog_current_payload",
            "current_payload_path and current_payload_sha256 must be declared together",
            path_field_declared=payload_path_declared,
            hash_field_declared=payload_hash_declared,
            comparison_baseline=comparison_baseline or None,
        )
    elif not payload_path_declared:
        if comparison_baseline != "source":
            audit.fail(
                f"record.{content_id}.catalog_current_payload",
                "A catalog without a current TV payload requires comparison_baseline='source'",
                comparison_baseline=comparison_baseline or None,
            )
        else:
            audit.pass_check(
                f"record.{content_id}.catalog_current_payload",
                present=False,
                comparison_baseline="source",
            )
    else:
        payload_raw = record.get("current_payload_path")
        payload_expected = expected_hash(record, ("current_payload_sha256",))
        payload_path = resolve_path(payload_raw, catalog_base) if payload_raw else None
        payload_actual_hash = ""
        payload_dimensions: tuple[int, int] | None = None
        payload_error = ""
        if payload_path is None or not payload_path.is_file():
            payload_error = f"Missing current payload: {payload_path}"
        elif not payload_expected:
            payload_error = "Catalog current-payload hash is missing or invalid"
        else:
            try:
                payload_actual_hash = sha256_file(payload_path)
                payload_dimensions, _, _ = inspect_oriented(payload_path)
            except Exception as exc:
                payload_error = f"Cannot read current payload: {exc}"
            if not payload_error and payload_actual_hash != payload_expected:
                payload_error = "Catalog current-payload SHA-256 changed"
            expected_payload_dimensions = (
                dimensions(record, "current_payload") or PANEL_SIZE
            )
            if not payload_error and payload_dimensions != expected_payload_dimensions:
                payload_error = (
                    "Catalog current-payload dimensions changed or are not 1920x1080"
                )
        if payload_error:
            audit.fail(
                f"record.{content_id}.catalog_current_payload",
                payload_error,
                path=str(payload_path) if payload_path else None,
                expected_sha256=payload_expected or None,
                actual_sha256=payload_actual_hash or None,
                expected_dimensions=list(
                    dimensions(record, "current_payload") or PANEL_SIZE
                ),
                actual_dimensions=(
                    list(payload_dimensions) if payload_dimensions else None
                ),
                comparison_baseline=comparison_baseline or None,
            )
        else:
            audit.pass_check(
                f"record.{content_id}.catalog_current_payload",
                present=True,
                path=str(payload_path),
                sha256=payload_actual_hash,
                dimensions=list(payload_dimensions or ()),
                comparison_baseline=comparison_baseline or None,
            )
    return source_path, source_actual_dimensions, source_actual_hash


def validate_contact_sheets(
    document: dict[str, Any],
    manifest_base: Path,
    audit: Audit,
) -> set[Path]:
    entries = contact_sheet_entries(document)
    if not entries:
        audit.fail(
            "comparison_contact_sheets",
            "Render manifest contains no comparison contact sheets",
        )
        return set()
    paths: set[Path] = set()
    facts: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        raw_path = entry_path(entry)
        path = resolve_path(raw_path, manifest_base) if raw_path else None
        if path is None or not path.is_file():
            audit.fail(
                f"comparison_contact_sheet.{index}",
                "Comparison contact sheet is missing",
                path=str(path) if path else None,
            )
            continue
        try:
            actual_hash = sha256_file(path)
            size, mode, image_format = inspect_oriented(path)
        except Exception as exc:
            audit.fail(
                f"comparison_contact_sheet.{index}",
                f"Cannot hash/decode comparison contact sheet: {exc}",
                path=str(path),
            )
            continue
        recorded_hash = (
            expected_hash(entry, ("sha256", "output_sha256"))
            if isinstance(entry, dict)
            else ""
        )
        if recorded_hash and recorded_hash != actual_hash:
            audit.fail(
                f"comparison_contact_sheet.{index}",
                "Comparison contact-sheet SHA-256 changed",
                path=str(path),
                expected_sha256=recorded_hash,
                actual_sha256=actual_hash,
            )
            continue
        paths.add(path)
        facts.append(
            {
                "path": str(path),
                "sha256": actual_hash,
                "width": size[0],
                "height": size[1],
                "mode": mode,
                "format": image_format,
            }
        )
        audit.pass_check(f"comparison_contact_sheet.{index}", **facts[-1])
    if len(paths) != len(entries):
        audit.fail(
            "comparison_contact_sheets.complete",
            "One or more comparison contact sheets failed validation",
            declared=len(entries),
            valid=len(paths),
        )
    else:
        audit.pass_check("comparison_contact_sheets.complete", count=len(paths))
    return paths


def render_root(
    document: dict[str, Any], manifest_base: Path, outputs: set[Path]
) -> Path | None:
    raw = first_value(
        document,
        ("rendered_dir", "render_output_dir", "output_dir", "output_root"),
    )
    if raw:
        return resolve_path(raw, manifest_base)
    if not outputs:
        return None
    parents = [str(path.parent) for path in outputs]
    return Path(os.path.commonpath(parents))


def validate_documents(args: argparse.Namespace, audit: Audit) -> dict[str, str]:
    manifest_hashes: dict[str, str] = {}
    loaded: dict[str, dict[str, Any]] = {}
    paths = {
        "catalog": args.catalog.resolve(strict=False),
        "art_direction": args.art_direction.resolve(strict=False),
        "render_manifest": args.render_manifest.resolve(strict=False),
    }
    for label, path in paths.items():
        try:
            document, digest = load_json(path)
        except Exception as exc:
            audit.fail(
                f"input.{label}", f"Cannot hash/read JSON input: {exc}", path=str(path)
            )
            continue
        loaded[label] = document
        manifest_hashes[label] = digest
        audit.pass_check(f"input.{label}", path=str(path), sha256=digest)
    if len(loaded) != len(paths):
        return manifest_hashes

    catalog = loaded["catalog"]
    art = loaded["art_direction"]
    render = loaded["render_manifest"]
    catalog_records, catalog_map = extract_records("catalog", catalog, audit)
    art_records, art_map = extract_records("art_direction", art, audit)
    render_records, render_map = extract_records("render_manifest", render, audit)
    del catalog_records, art_records
    id_sets = {
        "catalog": set(catalog_map),
        "art_direction": set(art_map),
        "render_manifest": set(render_map),
    }
    missing_from_catalog = id_sets["art_direction"] - id_sets["catalog"]
    art_only = id_sets["art_direction"] - id_sets["render_manifest"]
    render_only = id_sets["render_manifest"] - id_sets["art_direction"]
    if missing_from_catalog or art_only or render_only:
        audit.fail(
            "record_set_alignment",
            "Art-direction/render records differ or reference IDs absent from the catalog",
            counts={key: len(value) for key, value in id_sets.items()},
            missing_from_catalog=sorted(missing_from_catalog),
            missing_from_render_manifest=sorted(art_only),
            missing_from_art_direction=sorted(render_only),
        )
    else:
        unused_catalog_ids = id_sets["catalog"] - id_sets["art_direction"]
        audit.pass_check(
            "record_set_alignment",
            batch_count=len(art_map),
            catalog_count=len(catalog_map),
            unused_catalog_count=len(unused_catalog_ids),
        )

    families = sorted(
        {treatment(value) for value in art_map.values() if treatment(value)}
    )
    blank_family_ids = sorted(
        content_id for content_id, value in art_map.items() if not treatment(value)
    )
    limit, overridden, override_key = family_limit(art)
    if blank_family_ids:
        audit.fail(
            "design_families",
            "Every art-direction record needs a treatment/design_family",
            missing_treatment_ids=blank_family_ids,
        )
    elif len(families) > limit:
        audit.fail(
            "design_families",
            "Design-family count exceeds the allowed limit",
            count=len(families),
            families=families,
            limit=limit,
            override=False,
        )
    else:
        audit.pass_check(
            "design_families",
            count=len(families),
            families=families,
            limit=None if limit > 1_000_000 else limit,
            override=overridden,
            override_key=override_key,
        )

    output_paths: list[Path] = []
    missing_output_path_ids: list[str] = []
    for content_id, record in render_map.items():
        raw = first_value(record, ("output_path", "rendered_path", "panel_path"))
        if raw:
            output_paths.append(resolve_path(raw, paths["render_manifest"].parent))
        else:
            missing_output_path_ids.append(content_id)
    duplicate_outputs = sorted(
        {
            str(path)
            for path in output_paths
            if sum(candidate == path for candidate in output_paths) > 1
        }
    )
    if missing_output_path_ids or duplicate_outputs:
        audit.fail(
            "unique_output_paths",
            "Every render record needs one unique output path",
            missing_output_path_ids=sorted(missing_output_path_ids),
            duplicate_paths=duplicate_outputs,
        )
    else:
        audit.pass_check("unique_output_paths", count=len(output_paths))

    contact_paths = validate_contact_sheets(
        render, paths["render_manifest"].parent, audit
    )
    expected_outputs = set(output_paths)
    root = render_root(render, paths["render_manifest"].parent, expected_outputs)
    actual_outputs: set[Path] = set()
    if root is None or not root.is_dir():
        audit.fail(
            "render_output_inventory",
            "Rendered output directory is missing or cannot be derived",
            root=str(root) if root else None,
        )
    else:
        actual_outputs = {
            path.resolve(strict=False)
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() == ".png"
        } - contact_paths
        missing = sorted(str(path) for path in expected_outputs - actual_outputs)
        extra = sorted(str(path) for path in actual_outputs - expected_outputs)
        if missing or extra:
            audit.fail(
                "render_output_inventory",
                "Rendered PNG inventory differs from the manifest",
                root=str(root),
                missing=missing,
                extra=extra,
            )
        else:
            audit.pass_check(
                "render_output_inventory",
                root=str(root),
                count=len(actual_outputs),
            )

    common_ids = sorted(set(catalog_map) & set(art_map) & set(render_map))
    for content_id in common_ids:
        catalog_record = catalog_map[content_id]
        art_record = art_map[content_id]
        render_record = render_map[content_id]
        identity_field = next(
            (
                field
                for field in ("content_id", "asset_id", "record_id")
                if str(catalog_record.get(field, "")).strip() == content_id
            ),
            "record_id",
        )
        record_audit: dict[str, Any] = {
            "record_id": content_id,
            "identity_field": identity_field,
            identity_field: content_id,
            "status": "PASS",
        }
        failures_before = len(audit.failures)
        source_path, source_size, source_actual_hash = validate_catalog_record(
            content_id,
            catalog_record,
            render_record,
            paths["catalog"].parent,
            audit,
        )
        record_audit["source_path"] = str(source_path) if source_path else None
        record_audit["source_sha256"] = source_actual_hash or None
        record_audit["source_dimensions"] = list(source_size) if source_size else None

        rendered_source_hash = expected_hash(render_record, ("source_sha256",))
        rendered_source_raw = render_record.get("source_path")
        rendered_source_path = (
            resolve_path(rendered_source_raw, paths["render_manifest"].parent)
            if rendered_source_raw
            else None
        )
        if (
            not rendered_source_hash
            or not source_actual_hash
            or rendered_source_hash != source_actual_hash
            or (
                source_path is not None
                and rendered_source_path is not None
                and rendered_source_path != source_path
            )
        ):
            audit.fail(
                f"record.{content_id}.renderer_source",
                "Renderer source path/hash does not bind to the verified catalog source",
                catalog_path=str(source_path) if source_path else None,
                renderer_path=str(rendered_source_path)
                if rendered_source_path
                else None,
                catalog_sha256=source_actual_hash or None,
                renderer_sha256=rendered_source_hash or None,
            )
        else:
            audit.pass_check(
                f"record.{content_id}.renderer_source",
                path=str(rendered_source_path or source_path),
                sha256=rendered_source_hash,
            )

        art_family = treatment(art_record)
        render_family = treatment(render_record)
        if not render_family or render_family != art_family:
            audit.fail(
                f"record.{content_id}.treatment",
                "Renderer treatment differs from art direction",
                art_direction=art_family or None,
                renderer=render_family or None,
            )
        else:
            audit.pass_check(
                f"record.{content_id}.treatment",
                treatment=art_family,
            )

        art_edges, art_subjects, art_protection_errors = protection_fields(art_record)
        render_edges, render_subjects, render_protection_errors = protection_fields(
            render_record
        )
        protection_errors = [
            *(f"art direction: {message}" for message in art_protection_errors),
            *(f"renderer: {message}" for message in render_protection_errors),
        ]
        if art_edges != render_edges:
            protection_errors.append(
                "renderer protected_edges differ from canonical art-direction sides/order"
            )
        if art_subjects != render_subjects:
            protection_errors.append(
                "renderer did not preserve informational protected_subjects verbatim"
            )
        record_audit["protected_edges"] = art_edges
        record_audit["protected_subjects"] = art_subjects
        if protection_errors:
            audit.fail(
                f"record.{content_id}.protected_regions",
                "Protected regions are malformed or changed by the renderer",
                errors=protection_errors,
                art_direction_protected_edges=art_record.get("protected_edges", []),
                renderer_protected_edges=render_record.get("protected_edges", []),
                art_direction_protected_subjects=art_record.get(
                    "protected_subjects", []
                ),
                renderer_protected_subjects=render_record.get("protected_subjects", []),
            )
        else:
            audit.pass_check(
                f"record.{content_id}.protected_regions",
                protected_edges=art_edges,
                protected_subjects=art_subjects,
                informational_subjects=True,
            )

        max_crop = as_number(
            first_value(art_record, ("max_crop_fraction", "crop_ceiling", "crop_limit"))
        )
        rendered_max_crop = as_number(
            first_value(
                render_record,
                ("max_crop_fraction", "crop_ceiling", "crop_limit"),
            )
        )
        actual_crop = as_number(
            first_value(render_record, ("crop_fraction", "actual_crop_fraction"))
        )
        if (
            max_crop is None
            or rendered_max_crop is None
            or actual_crop is None
            or max_crop < 0
            or max_crop > 1
            or abs(rendered_max_crop - max_crop) > 1e-9
            or actual_crop < -1e-9
            or actual_crop > max_crop + 1e-6
        ):
            audit.fail(
                f"record.{content_id}.crop_limit",
                "Rendered crop/ceiling is missing, changed, invalid, or above art direction",
                crop_fraction=actual_crop,
                art_direction_max_crop_fraction=max_crop,
                renderer_max_crop_fraction=rendered_max_crop,
            )
        else:
            audit.pass_check(
                f"record.{content_id}.crop_limit",
                crop_fraction=actual_crop,
                max_crop_fraction=max_crop,
            )

        upscale_factor = rendered_upscale_factor(render_record)
        upscale_flag = render_record.get("upscaled")
        allow_upscale = art_record.get("allow_upscale") is True
        acknowledge_upscale = art.get("acknowledge_upscale_risk") is True
        record_audit["upscale_factor"] = upscale_factor
        record_audit["allow_upscale"] = allow_upscale
        if upscale_factor is None or upscale_factor <= 0:
            audit.fail(
                f"record.{content_id}.upscale",
                "Renderer must record a positive upscale_factor or deterministic scale",
                upscale_factor=upscale_factor,
            )
        elif upscale_flag is not None and (
            not isinstance(upscale_flag, bool)
            or upscale_flag != (upscale_factor > 1.0 + 1e-9)
        ):
            audit.fail(
                f"record.{content_id}.upscale",
                "Renderer upscaled flag disagrees with its scale factor",
                upscale_factor=upscale_factor,
                upscaled=upscale_flag,
            )
        elif upscale_factor <= 1.0 + 1e-9:
            audit.pass_check(
                f"record.{content_id}.upscale",
                upscale_factor=upscale_factor,
                upscaled=False,
            )
        elif not allow_upscale:
            audit.fail(
                f"record.{content_id}.upscale",
                "Upscaling is forbidden unless this art-direction record sets allow_upscale=true",
                upscale_factor=upscale_factor,
                allow_upscale=False,
            )
        elif not acknowledge_upscale:
            audit.fail(
                f"record.{content_id}.upscale",
                "Approved per-photo upscaling still requires top-level acknowledge_upscale_risk=true",
                upscale_factor=upscale_factor,
                allow_upscale=True,
                acknowledge_upscale_risk=False,
                manual_gate="Explicitly acknowledge softness risk before upload",
            )
        else:
            audit.pass_check(
                f"record.{content_id}.upscale",
                upscale_factor=upscale_factor,
                allow_upscale=True,
                acknowledge_upscale_risk=True,
                manual_gate="acknowledged",
            )
            audit.warn(
                f"record.{content_id}.upscale_manual_gate",
                "This render intentionally enlarges its source and requires manual softness review",
                upscale_factor=upscale_factor,
                manual_gate="Inspect the individual render before any upload",
            )

        output_raw = first_value(
            render_record, ("output_path", "rendered_path", "panel_path")
        )
        output_path = (
            resolve_path(output_raw, paths["render_manifest"].parent)
            if output_raw
            else None
        )
        output_expected_hash = expected_hash(
            render_record,
            ("output_sha256", "rendered_sha256", "panel_sha256"),
        )
        output_actual_hash = ""
        output_facts: dict[str, Any] = {}
        rendered_image: Image.Image | None = None
        if output_path is None or not output_path.is_file():
            audit.fail(
                f"record.{content_id}.rendered_file",
                "Rendered output is missing",
                path=str(output_path) if output_path else None,
            )
        else:
            try:
                output_actual_hash = sha256_file(output_path)
                output_facts, rendered_image = inspect_rendered(output_path)
            except Exception as exc:
                audit.fail(
                    f"record.{content_id}.rendered_file",
                    f"Cannot hash/decode rendered output: {exc}",
                    path=str(output_path),
                )
            else:
                violations: list[str] = []
                if (
                    output_facts["format"].upper() != "PNG"
                    or output_path.suffix.lower() != ".png"
                ):
                    violations.append("not a PNG")
                if (output_facts["width"], output_facts["height"]) != PANEL_SIZE:
                    violations.append("not 1920x1080")
                if output_facts["mode"] != "RGB":
                    violations.append("not RGB")
                if not output_facts["explicit_srgb"]:
                    violations.append("does not contain an explicit sRGB chunk/profile")
                if not output_expected_hash:
                    violations.append("manifest output SHA-256 is missing or invalid")
                elif output_expected_hash != output_actual_hash:
                    violations.append("manifest output SHA-256 differs")
                if violations:
                    audit.fail(
                        f"record.{content_id}.rendered_file",
                        "Rendered output failed panel validation",
                        path=str(output_path),
                        violations=violations,
                        expected_sha256=output_expected_hash or None,
                        actual_sha256=output_actual_hash,
                        **output_facts,
                    )
                else:
                    audit.pass_check(
                        f"record.{content_id}.rendered_file",
                        path=str(output_path),
                        sha256=output_actual_hash,
                        **output_facts,
                    )
        record_audit["output_path"] = str(output_path) if output_path else None
        record_audit["output_sha256"] = output_actual_hash or None
        record_audit["crop_fraction"] = actual_crop
        record_audit["max_crop_fraction"] = max_crop
        record_audit["treatment"] = art_family or None

        rect = rect_from_record(render_record)
        if art_family in FRAMED_TREATMENTS:
            rect_problem = ""
            margins: dict[str, int] | None = None
            contrast: float | None = None
            if rect is None:
                rect_problem = "Framed treatment has no valid integer image_rect"
            else:
                x, y, width, height = rect
                margins = {
                    "left": x,
                    "top": y,
                    "right": PANEL_SIZE[0] - x - width,
                    "bottom": PANEL_SIZE[1] - y - height,
                }
                if (
                    width <= 0
                    or height <= 0
                    or any(value <= 0 for value in margins.values())
                ):
                    rect_problem = "Framed image rectangle is invalid or lacks margins"
                elif x + width > PANEL_SIZE[0] or y + height > PANEL_SIZE[1]:
                    rect_problem = "Framed image rectangle extends outside the panel"
                elif rendered_image is not None:
                    contrast = boundary_contrast(rendered_image, rect)
                    if contrast < 2.0:
                        rect_problem = "No clearly visible image boundary was measured"
            if rect_problem:
                audit.fail(
                    f"record.{content_id}.framing",
                    rect_problem,
                    image_rect=list(rect) if rect else None,
                    margins=margins,
                    boundary_contrast=contrast,
                )
            else:
                audit.pass_check(
                    f"record.{content_id}.framing",
                    image_rect=list(rect or ()),
                    margins=margins,
                    boundary_contrast=contrast,
                )
            record_audit["image_rect"] = list(rect) if rect else None
            record_audit["margins"] = margins
            record_audit["boundary_contrast"] = contrast

        if source_size is not None:
            crop_rect = rect
            if crop_rect is None and art_family == "full_bleed":
                crop_rect = (0, 0, PANEL_SIZE[0], PANEL_SIZE[1])
            if (
                crop_rect is not None
                and crop_rect[2] > 0
                and crop_rect[3] > 0
                and actual_crop is not None
            ):
                calculated_crop = crop_fraction_for_rect(source_size, crop_rect)
                record_audit["calculated_crop_fraction"] = calculated_crop
                if abs(calculated_crop - actual_crop) > 0.002:
                    audit.fail(
                        f"record.{content_id}.crop_geometry",
                        "Recorded crop fraction does not match source and image-rectangle geometry",
                        recorded_crop_fraction=actual_crop,
                        calculated_crop_fraction=calculated_crop,
                        tolerance=0.002,
                    )
                else:
                    audit.pass_check(
                        f"record.{content_id}.crop_geometry",
                        recorded_crop_fraction=actual_crop,
                        calculated_crop_fraction=calculated_crop,
                        tolerance=0.002,
                    )
        if len(audit.failures) > failures_before:
            record_audit["status"] = "FAIL"
        audit.records.append(record_audit)
    if len(render_records) != len(audit.records):
        audit.fail(
            "records_audited",
            "Not every render-manifest record could be audited end to end",
            declared=len(render_records),
            audited=len(audit.records),
        )
    else:
        audit.pass_check("records_audited", count=len(audit.records))
    return manifest_hashes


def main() -> int:
    args = parse_args()
    inputs = {
        "catalog": str(args.catalog.resolve(strict=False)),
        "art_direction": str(args.art_direction.resolve(strict=False)),
        "render_manifest": str(args.render_manifest.resolve(strict=False)),
        "output": str(args.output.resolve(strict=False)),
    }
    audit = Audit(inputs)
    manifest_hashes: dict[str, str] = {}
    try:
        manifest_hashes = validate_documents(args, audit)
    except Exception as exc:
        audit.fail(
            "validator.internal",
            f"Unexpected validator error: {type(exc).__name__}: {exc}",
        )
    document = audit.document(manifest_hashes)
    try:
        atomic_save(args.output, document)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "message": f"Could not write validation audit: {exc}",
                    "audit": document,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2
    print(
        f"{document['status']} rendered-batch validation: "
        f"{len(audit.records)} records, {len(audit.failures)} failures; "
        f"audit={args.output}",
        flush=True,
    )
    return 0 if document["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
