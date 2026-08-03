#!/usr/bin/env python3
"""Run a clean-room offline smoke test for the Frame Art Curator helpers."""

from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from PIL import Image, ImageCms, ImageDraw


PANEL_SIZE = (1920, 1080)


class SelfTestError(RuntimeError):
    """A smoke-test assertion failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SelfTestError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected a JSON object: {path.name}")
    return value


def run_command(
    arguments: list[str],
    *,
    cwd: Path,
    should_succeed: bool,
    label: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if should_succeed and result.returncode != 0:
        raise SelfTestError(
            f"{label} failed with exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    if not should_succeed and result.returncode == 0:
        raise SelfTestError(f"{label} unexpectedly allowed an overwrite")
    return result


def synthetic_landscape(path: Path) -> tuple[int, int]:
    size = (2000, 1400)
    image = Image.new("RGB", size, (45, 83, 116))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1999, 45), fill=(19, 31, 43))
    draw.rectangle((0, 900, 1999, 1399), fill=(158, 116, 66))
    draw.polygon(
        [(0, 950), (420, 520), (820, 910), (1270, 390), (1999, 940)],
        fill=(73, 101, 72),
    )
    draw.ellipse((780, 260, 1160, 640), fill=(238, 198, 94))
    draw.line((0, 1050, 1999, 1050), fill=(235, 224, 197), width=18)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", compress_level=4)
    return size


def synthetic_portrait(
    path: Path,
    *,
    background: tuple[int, int, int],
    accent: tuple[int, int, int],
    reverse: bool,
) -> tuple[int, int]:
    size = (1000, 1500)
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 999, 34), fill=(24, 26, 31))
    if reverse:
        draw.polygon([(0, 1150), (1000, 420), (1000, 1500), (0, 1500)], fill=accent)
        draw.ellipse((110, 260, 600, 750), outline=(239, 232, 215), width=28)
    else:
        draw.polygon([(0, 430), (1000, 1150), (1000, 1500), (0, 1500)], fill=accent)
        draw.ellipse((400, 260, 890, 750), outline=(239, 232, 215), width=28)
    draw.rectangle((120, 1050, 880, 1110), fill=(232, 223, 204))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", compress_level=4)
    return size


def is_srgb_profile(profile_bytes: bytes) -> bool:
    try:
        profile = ImageCms.ImageCmsProfile(BytesIO(profile_bytes))
        description = ImageCms.getProfileDescription(profile)
        name = ImageCms.getProfileName(profile)
    except Exception:
        return False
    return "srgb" in f"{description} {name}".lower()


def verify_panel(path: Path, expected_hash: str) -> str:
    require(path.is_file(), f"Missing rendered panel: {path.name}")
    actual_hash = sha256_file(path)
    require(actual_hash == expected_hash, f"Rendered hash changed: {path.name}")
    with Image.open(path) as opened:
        opened.load()
        profile = opened.info.get("icc_profile")
        require(opened.format == "PNG", f"Rendered panel is not PNG: {path.name}")
        require(
            opened.size == PANEL_SIZE, f"Rendered panel is not 1920x1080: {path.name}"
        )
        require(opened.mode == "RGB", f"Rendered panel is not RGB: {path.name}")
        require(
            isinstance(profile, bytes) and is_srgb_profile(profile),
            f"Rendered panel lacks an sRGB ICC profile: {path.name}",
        )
    return actual_hash


def generic_test(root: Path, scripts: Path) -> None:
    source = root / "generic" / "inputs" / "synthetic-landscape.png"
    width, height = synthetic_landscape(source)
    asset_id = "src_synthetic_landscape"
    catalog = root / "generic" / "generic-catalog.json"
    art = root / "generic" / "generic-art-direction.json"
    output = root / "generic" / "proofs"
    audit = root / "generic" / "validation.json"
    write_json(
        catalog,
        {
            "schema_version": 1,
            "status": "complete",
            "private_artifact": True,
            "record_count": 1,
            "records": [
                {
                    "asset_id": asset_id,
                    "source_name": source.name,
                    "source_path": str(source),
                    "source_sha256": sha256_file(source),
                    "width": width,
                    "height": height,
                    "aspect_ratio": round(width / height, 6),
                    "orientation": "landscape",
                }
            ],
        },
    )
    write_json(
        art,
        {
            "schema_version": 1,
            "canvas": {"width": 1920, "height": 1080, "color_space": "sRGB"},
            "acknowledge_upscale_risk": False,
            "records": [
                {
                    "asset_id": asset_id,
                    "treatment": "museum_light",
                    "rationale": "Synthetic landscape used only by the offline smoke test.",
                    "focal_point": [0.5, 0.5],
                    "protected_edges": ["left", "right", "top", "bottom"],
                    "max_crop_fraction": 0.0,
                    "matte_strategy": "fixed",
                    "matte_hex": "#F1ECE2",
                    "margins": {"left": 48, "right": 48, "top": 48, "bottom": 48},
                    "vertical_bias": 0.0,
                    "allow_upscale": False,
                    "keyline": {
                        "enabled": True,
                        "color": "#9A968E",
                        "width": 2,
                        "opacity": 0.7,
                    },
                    "shadow": {
                        "enabled": True,
                        "opacity": 0.12,
                        "blur": 14,
                        "offset_x": 0,
                        "offset_y": 7,
                    },
                }
            ],
        },
    )
    render_command = [
        sys.executable,
        str(scripts / "render_frame_batch.py"),
        "--catalog",
        str(catalog),
        "--art-direction",
        str(art),
        "--output-dir",
        str(output),
    ]
    run_command(render_command, cwd=root, should_succeed=True, label="generic renderer")
    manifest_path = output / "render-manifest.json"
    manifest = read_json(manifest_path)
    render_records = manifest.get("records")
    require(
        isinstance(render_records, list) and len(render_records) == 1,
        "Generic manifest count differs",
    )
    render_record = render_records[0]
    require(isinstance(render_record, dict), "Generic render record is malformed")
    require(
        render_record.get("comparison_baseline") == "source",
        "Generic baseline is not source",
    )
    require(
        "current_payload_path" not in render_record,
        "Generic render invented a TV payload",
    )
    panel = Path(str(render_record.get("output_path", "")))
    panel_hash = verify_panel(panel, str(render_record.get("output_sha256", "")))
    require(
        len(list((output / "renders").glob("*.png"))) == 1,
        "Generic render directory does not contain exactly one panel",
    )
    require(
        len(list((output / "contact-sheets").glob("*.png"))) == 1,
        "Generic render did not produce exactly one comparison sheet",
    )
    run_command(
        render_command,
        cwd=root,
        should_succeed=False,
        label="generic no-overwrite rerun",
    )
    require(
        sha256_file(panel) == panel_hash, "Generic no-overwrite rerun altered the panel"
    )
    run_command(
        [
            sys.executable,
            str(scripts / "validate_rendered_batch.py"),
            "--catalog",
            str(catalog),
            "--art-direction",
            str(art),
            "--render-manifest",
            str(manifest_path),
            "--output",
            str(audit),
        ],
        cwd=root,
        should_succeed=True,
        label="generic validator",
    )
    validation = read_json(audit)
    require(validation.get("status") == "PASS", "Generic validation audit did not PASS")
    require(
        not validation.get("failures"), "Generic validation audit contains failures"
    )


def portrait_test(root: Path, scripts: Path) -> None:
    inputs = root / "diptych" / "inputs"
    left = inputs / "synthetic-left.png"
    right = inputs / "synthetic-right.png"
    left_size = synthetic_portrait(
        left,
        background=(41, 71, 92),
        accent=(176, 104, 58),
        reverse=False,
    )
    right_size = synthetic_portrait(
        right,
        background=(88, 54, 66),
        accent=(60, 112, 105),
        reverse=True,
    )
    left_id = "src_synthetic_portrait_left"
    right_id = "src_synthetic_portrait_right"
    pair_id = "diptych_synthetic_pair"
    catalog = root / "diptych" / "portrait-catalog.json"
    art = root / "diptych" / "diptych-art-direction.json"
    output = root / "diptych" / "proofs"
    audit = root / "diptych" / "validation.json"
    catalog_records = []
    for identifier, path, size in (
        (left_id, left, left_size),
        (right_id, right, right_size),
    ):
        catalog_records.append(
            {
                "asset_id": identifier,
                "source_name": path.name,
                "source_path": str(path),
                "source_sha256": sha256_file(path),
                "width": size[0],
                "height": size[1],
                "aspect_ratio": round(size[0] / size[1], 6),
                "orientation": "portrait",
            }
        )
    write_json(
        catalog,
        {
            "schema_version": 1,
            "status": "complete",
            "private_artifact": True,
            "record_count": 2,
            "records": catalog_records,
        },
    )
    write_json(
        art,
        {
            "schema_version": 1,
            "canvas": {"width": 1920, "height": 1080, "color_space": "sRGB"},
            "acknowledge_upscale_risk": False,
            "records": [
                {
                    "asset_id": pair_id,
                    "treatment": "diptych_portrait",
                    "source_asset_ids": [left_id, right_id],
                    "rationale": "Synthetic portraits used only by the offline smoke test.",
                    "pair_evidence": {"basis": ["synthetic_test_pair"]},
                    "left_right_reason": "Fixed test order verifies left and right source binding.",
                    "max_crop_fraction": 0.0,
                    "allow_upscale": False,
                    "matte_strategy": "fixed",
                    "matte_hex": "#E7E4DE",
                    "outer_margins": {
                        "left": 130,
                        "right": 130,
                        "top": 90,
                        "bottom": 90,
                    },
                    "gutter": 70,
                    "vertical_bias": 0.0,
                    "keyline": {"enabled": True, "color": "#8F8C86", "width": 2},
                    "shadow": {
                        "enabled": True,
                        "opacity": 0.12,
                        "blur": 14,
                        "offset_y": 7,
                    },
                }
            ],
        },
    )
    render_command = [
        sys.executable,
        str(scripts / "render_portrait_diptychs.py"),
        "--catalog",
        str(catalog),
        "--art-direction",
        str(art),
        "--output-dir",
        str(output),
    ]
    run_command(render_command, cwd=root, should_succeed=True, label="diptych renderer")
    manifest_path = output / "diptych-render-manifest.json"
    manifest = read_json(manifest_path)
    render_records = manifest.get("records")
    require(
        isinstance(render_records, list) and len(render_records) == 1,
        "Diptych manifest count differs",
    )
    render_record = render_records[0]
    require(isinstance(render_record, dict), "Diptych render record is malformed")
    source_order = render_record.get("source_asset_ids")
    require(
        source_order == [left_id, right_id], "Diptych renderer changed left/right order"
    )
    panel = Path(str(render_record.get("output_path", "")))
    panel_hash = verify_panel(panel, str(render_record.get("output_sha256", "")))
    require(
        len(list((output / "rendered").glob("*.png"))) == 1,
        "Diptych render directory does not contain exactly one panel",
    )
    require(
        len(list((output / "contact-sheets").glob("*.png"))) == 1,
        "Diptych render did not produce exactly one contact sheet",
    )
    run_command(
        render_command,
        cwd=root,
        should_succeed=False,
        label="diptych no-overwrite rerun",
    )
    require(
        sha256_file(panel) == panel_hash, "Diptych no-overwrite rerun altered the panel"
    )
    run_command(
        [
            sys.executable,
            str(scripts / "validate_portrait_diptychs.py"),
            "--catalog",
            str(catalog),
            "--art-direction",
            str(art),
            "--render-manifest",
            str(manifest_path),
            "--output",
            str(audit),
        ],
        cwd=root,
        should_succeed=True,
        label="diptych validator",
    )
    validation = read_json(audit)
    require(validation.get("status") == "PASS", "Diptych validation audit did not PASS")
    require(
        not validation.get("failures"), "Diptych validation audit contains failures"
    )


def main() -> int:
    scripts = Path(__file__).resolve().parent
    required = (
        "render_frame_batch.py",
        "validate_rendered_batch.py",
        "render_portrait_diptychs.py",
        "validate_portrait_diptychs.py",
    )
    for name in required:
        require((scripts / name).is_file(), f"Missing helper: {name}")
    try:
        with tempfile.TemporaryDirectory(prefix="frame-art-self-test-") as temporary:
            root = Path(temporary)
            generic_test(root, scripts)
            portrait_test(root, scripts)
    except (
        OSError,
        subprocess.SubprocessError,
        SelfTestError,
        json.JSONDecodeError,
    ) as error:
        print(
            f"FAIL offline Frame Art smoke test: {error}", file=sys.stderr, flush=True
        )
        return 1
    print(
        "PASS offline Frame Art smoke test: landscape + diptych rendered, validated, and no-clobber proven",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
