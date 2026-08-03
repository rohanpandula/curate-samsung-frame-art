#!/usr/bin/env python3
"""Create a verified, portable source catalog from photo files or folders."""

from __future__ import annotations

import argparse
import colorsys
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterable

from PIL import Image, ImageOps, ImageStat


Image.MAX_IMAGE_PIXELS = 250_000_000
SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
ORIENTATIONS = {"all", "landscape", "portrait", "square"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hash and catalog stable photo sources without modifying them."
    )
    parser.add_argument(
        "--input",
        type=Path,
        action="append",
        required=True,
        help="Photo file or folder. Repeat for multiple inputs.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--orientation",
        choices=sorted(ORIENTATIONS),
        default="all",
        help="Keep only this decoded orientation in the output.",
    )
    parser.add_argument(
        "--stability-seconds",
        type=float,
        default=2.0,
        help="Seconds between two size/mtime snapshots. Use 0 only for immutable stages.",
    )
    parser.add_argument(
        "--redact-gps",
        action="store_true",
        help="Remove GPS coordinates from the generated catalog.",
    )
    return parser.parse_args()


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


def discover(inputs: Iterable[Path], output: Path) -> list[Path]:
    output_resolved = output.expanduser().resolve(strict=False)
    found: set[Path] = set()
    for raw in inputs:
        candidate = raw.expanduser()
        if not candidate.exists():
            raise RuntimeError(f"Input does not exist: {candidate}")
        paths = candidate.rglob("*") if candidate.is_dir() else (candidate,)
        for path in paths:
            if path.is_symlink():
                continue
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved != output_resolved:
                found.add(resolved)
    if not found:
        raise RuntimeError("No supported photo files were found")
    return sorted(found, key=lambda value: str(value).casefold())


def snapshot(paths: Iterable[Path]) -> dict[Path, tuple[int, int]]:
    return {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in paths}


def ensure_stable(paths: list[Path], delay: float) -> dict[Path, tuple[int, int]]:
    if delay < 0 or delay > 60:
        raise RuntimeError("--stability-seconds must be between 0 and 60")
    first = snapshot(paths)
    if delay:
        time.sleep(delay)
    second = snapshot(paths)
    changed = [str(path) for path in paths if first[path] != second[path]]
    if changed:
        raise RuntimeError(f"Files changed during the stability window: {changed}")
    return second


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def orientation(width: int, height: int) -> str:
    ratio = width / height
    if 0.97 <= ratio <= 1.03:
        return "square"
    return "landscape" if width > height else "portrait"


def text_exif(exif: Any, *tags: int) -> str | None:
    for tag in tags:
        value = exif.get(tag)
        if value:
            return str(value).strip() or None
    return None


def capture_time(exif: Any) -> str | None:
    """Prefer DateTimeOriginal/CreateDate from ExifIFD over IFD0 ModifyDate."""
    try:
        exif_ifd = exif.get_ifd(34665)
    except (AttributeError, KeyError, TypeError):
        exif_ifd = None
    original = text_exif(exif_ifd or {}, 36867, 36868)
    return normalized_capture_time(original or text_exif(exif, 306))


def normalized_capture_time(value: str | None) -> str | None:
    if not value:
        return None
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, pattern).isoformat()
        except ValueError:
            continue
    return value


def rational_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        numerator = float(getattr(value, "numerator"))
        denominator = float(getattr(value, "denominator"))
        return numerator / denominator


def coordinate(parts: Any, reference: str) -> float | None:
    try:
        degrees, minutes, seconds = (rational_float(part) for part in parts)
        result = degrees + minutes / 60 + seconds / 3600
        return -result if reference.upper() in {"S", "W"} else result
    except (TypeError, ValueError, ZeroDivisionError, AttributeError):
        return None


def gps_summary(exif: Any) -> dict[str, float] | None:
    try:
        gps = exif.get_ifd(34853)
    except (AttributeError, KeyError, TypeError):
        gps = None
    if not gps:
        return None
    latitude = coordinate(gps.get(2), str(gps.get(1, "")))
    longitude = coordinate(gps.get(4), str(gps.get(3, "")))
    if latitude is None or longitude is None:
        return None
    result = {"latitude": round(latitude, 7), "longitude": round(longitude, 7)}
    if 6 in gps:
        try:
            altitude = rational_float(gps[6])
            if int(gps.get(5, 0)) == 1:
                altitude = -altitude
            result["altitude_m"] = round(altitude, 1)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return result


def color_summary(image: Image.Image) -> dict[str, Any]:
    sample = image.convert("RGB")
    sample.thumbnail((96, 96), Image.Resampling.LANCZOS)
    mean = tuple(round(value) for value in ImageStat.Stat(sample).mean)
    pixels = list(sample.getdata())
    pixels.sort(key=lambda rgb: sum(rgb))
    median = pixels[len(pixels) // 2]
    r, g, b = (value / 255 for value in mean)
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    luminance = 0.2126 * mean[0] + 0.7152 * mean[1] + 0.0722 * mean[2]
    return {
        "mean_rgb": list(mean),
        "median_luminance_rgb": list(median),
        "mean_hue_degrees": round(hue * 360, 1),
        "mean_saturation": round(saturation, 4),
        "mean_luminance": round(luminance / 255, 4),
    }


def safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return cleaned[:40] or "photo"


def inspect_photo(path: Path, stat: tuple[int, int]) -> dict[str, Any]:
    digest = sha256_file(path)
    path_digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]
    with Image.open(path) as opened:
        exif = opened.getexif()
        decoded = ImageOps.exif_transpose(opened).convert("RGB")
        width, height = decoded.size
        record = {
            "asset_id": f"src_{safe_stem(path.stem)}_{digest[:12]}_{path_digest}",
            "source_name": path.name,
            "source_path": str(path),
            "source_sha256": digest,
            "file_size": stat[0],
            "mtime_ns": stat[1],
            "width": width,
            "height": height,
            "aspect_ratio": round(width / height, 6),
            "orientation": orientation(width, height),
            "captured_at": capture_time(exif),
            "camera_make": text_exif(exif, 271),
            "camera_model": text_exif(exif, 272),
            "gps": gps_summary(exif),
            "color_summary": color_summary(decoded),
        }
    return record


def main() -> int:
    args = parse_args()
    paths = discover(args.input, args.output)
    stable = ensure_stable(paths, args.stability_seconds)
    inspected = [inspect_photo(path, stable[path]) for path in paths]
    if args.redact_gps:
        for record in inspected:
            record["gps"] = None
            record["gps_redacted"] = True

    first_by_hash: dict[str, str] = {}
    for record in inspected:
        digest = record["source_sha256"]
        if digest in first_by_hash:
            record["duplicate_of"] = first_by_hash[digest]
        else:
            first_by_hash[digest] = record["asset_id"]

    if args.orientation == "all":
        records = inspected
    else:
        records = [
            record for record in inspected if record["orientation"] == args.orientation
        ]
    result = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "complete",
        "private_artifact": True,
        "orientation_filter": args.orientation,
        "gps_redacted": args.redact_gps,
        "discovered_count": len(inspected),
        "record_count": len(records),
        "excluded_count": len(inspected) - len(records),
        "records": records,
    }
    atomic_save(args.output, result)
    print(f"COMPLETE cataloged {len(records)} stable photos", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"ERROR {error}", file=__import__("sys").stderr, flush=True)
        raise SystemExit(1) from error
