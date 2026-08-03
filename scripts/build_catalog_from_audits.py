#!/usr/bin/env python3
"""Bind the verified current Frame IDs to immutable staged source originals."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from PIL import Image, ImageOps


Image.MAX_IMAGE_PIXELS = 250_000_000


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return value


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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        oriented = ImageOps.exif_transpose(image)
        return oriented.width, oriented.height


def stage_by_old_id(stage: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = stage.get("records")
    if stage.get("status") != "complete" or not isinstance(records, list):
        raise RuntimeError("A replacement stage is incomplete")
    result = {
        str(record.get("old_content_id", "")): record
        for record in records
        if str(record.get("old_content_id", ""))
    }
    if len(result) != len(
        [record for record in records if record.get("old_content_id")]
    ):
        raise RuntimeError("A replacement stage contains duplicate old IDs")
    return result


def select_source(stage_record: dict[str, Any]) -> tuple[Path, str]:
    candidates = (
        ("staged_original_path", "nas_sha256"),
        ("lowres_backup_path", "source_sha256"),
    )
    for path_key, hash_key in candidates:
        path = Path(str(stage_record.get(path_key, "")))
        expected_hash = str(stage_record.get(hash_key, ""))
        if path.is_file() and expected_hash and sha256_file(path) == expected_hash:
            return path, expected_hash
    raise RuntimeError(f"No immutable staged source survived: {stage_record!r}")


def replacement_group(
    *,
    cohort: str,
    stage_path: Path,
    upload_path: Path,
) -> dict[str, dict[str, Any]]:
    stage = load_json(stage_path)
    upload = load_json(upload_path)
    additions = upload.get("replacements")
    if upload.get("status") != "complete" or not isinstance(additions, list):
        raise RuntimeError(f"Replacement upload is incomplete: {upload_path}")
    staged = stage_by_old_id(stage)
    result: dict[str, dict[str, Any]] = {}
    for addition in additions:
        old_id = str(addition.get("old_content_id", ""))
        new_id = str(addition.get("new_content_id", ""))
        stage_record = staged.get(old_id)
        if not old_id or not new_id or not isinstance(stage_record, dict):
            raise RuntimeError(f"Replacement mapping is incomplete: {addition!r}")
        if str(stage_record.get("source_name", "")) != str(
            addition.get("source_name", "")
        ):
            raise RuntimeError(f"Source mapping changed for {old_id}")
        source_path, source_hash = select_source(stage_record)
        width, height = dimensions(source_path)
        current_payload = Path(str(addition.get("payload_path", "")))
        if not current_payload.is_file() or sha256_file(
            current_payload
        ) != addition.get("payload_sha256"):
            raise RuntimeError(f"Current payload changed: {current_payload}")
        result[new_id] = {
            "content_id": new_id,
            "cohort": cohort,
            "source_name": str(stage_record["source_name"]),
            "source_path": str(source_path),
            "source_sha256": source_hash,
            "source_width": width,
            "source_height": height,
            "current_payload_path": str(current_payload),
            "current_payload_sha256": str(addition["payload_sha256"]),
        }
    if len(result) != len(additions):
        raise RuntimeError(f"Duplicate new IDs in {upload_path}")
    return result


def additions_group(stage_path: Path, upload_path: Path) -> dict[str, dict[str, Any]]:
    stage = load_json(stage_path)
    upload = load_json(upload_path)
    stage_records = stage.get("records")
    additions = upload.get("additions")
    if (
        stage.get("status") != "complete"
        or not isinstance(stage_records, list)
        or upload.get("status") != "complete"
        or not isinstance(additions, list)
    ):
        raise RuntimeError("The additions stage/upload is incomplete")
    by_name = {str(record.get("source_name", "")): record for record in stage_records}
    if len(by_name) != len(stage_records):
        raise RuntimeError("The additions stage contains duplicate source names")
    result: dict[str, dict[str, Any]] = {}
    for addition in additions:
        source_name = str(addition.get("source_name", ""))
        content_id = str(addition.get("new_content_id", ""))
        stage_record = by_name.get(source_name)
        if not content_id or not isinstance(stage_record, dict):
            raise RuntimeError(f"Addition mapping is incomplete: {addition!r}")
        source_path, source_hash = select_source(stage_record)
        width, height = dimensions(source_path)
        current_payload = Path(str(addition.get("payload_path", "")))
        if not current_payload.is_file() or sha256_file(
            current_payload
        ) != addition.get("payload_sha256"):
            raise RuntimeError(f"Current addition payload changed: {current_payload}")
        result[content_id] = {
            "content_id": content_id,
            "cohort": "new_additions",
            "source_name": source_name,
            "source_path": str(source_path),
            "source_sha256": source_hash,
            "source_width": width,
            "source_height": height,
            "current_payload_path": str(current_payload),
            "current_payload_sha256": str(addition["payload_sha256"]),
        }
    if len(result) != len(additions):
        raise RuntimeError("The additions upload contains duplicate new IDs")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bind verified Samsung Frame IDs to immutable source originals."
    )
    parser.add_argument("--first-stage", type=Path, required=True)
    parser.add_argument("--remaining-stage", type=Path, required=True)
    parser.add_argument("--new-stage", type=Path, required=True)
    parser.add_argument("--first-upload", type=Path, required=True)
    parser.add_argument("--remaining-upload", type=Path, required=True)
    parser.add_argument("--new-upload", type=Path, required=True)
    parser.add_argument("--final-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    mapped: dict[str, dict[str, Any]] = {}
    for group in (
        replacement_group(
            cohort="first_replacements",
            stage_path=args.first_stage,
            upload_path=args.first_upload,
        ),
        replacement_group(
            cohort="remaining_replacements",
            stage_path=args.remaining_stage,
            upload_path=args.remaining_upload,
        ),
        additions_group(args.new_stage, args.new_upload),
    ):
        overlap = set(mapped) & set(group)
        if overlap:
            raise RuntimeError(
                f"Content IDs occur in multiple cohorts: {sorted(overlap)}"
            )
        mapped.update(group)

    final_audit = load_json(args.final_audit)
    final_ids = [str(value) for value in final_audit.get("final_ids_in_order", [])]
    if (
        final_audit.get("status") != "complete"
        or not final_ids
        or len(final_ids) != len(set(final_ids))
        or set(final_ids) != set(mapped)
    ):
        raise RuntimeError(
            "The final ordered ID set does not exactly match the source mappings: "
            f"final={len(final_ids)}, mapped={len(mapped)}, "
            f"missing={sorted(set(final_ids) - set(mapped))}, "
            f"extra={sorted(set(mapped) - set(final_ids))}"
        )
    records: list[dict[str, Any]] = []
    for position, content_id in enumerate(final_ids, start=1):
        record = dict(mapped[content_id])
        record["position"] = position
        record["source_aspect_ratio"] = round(
            record["source_width"] / record["source_height"], 6
        )
        records.append(record)
    result = {
        "created_at": __import__("datetime")
        .datetime.now()
        .astimezone()
        .isoformat(timespec="seconds"),
        "status": "complete",
        "source_final_audit": str(args.final_audit),
        "record_count": len(records),
        "records": records,
    }
    atomic_save(args.output, result)
    print(f"COMPLETE cataloged {len(records)} verified Frame sources", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
