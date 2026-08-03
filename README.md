# Frame Art Curator

Samsung's Frame is a 16:9 screen. Most cameras are not. If every photo gets the same cover crop, people lose their heads, panoramas lose their point, and anything near an edge is in danger.

Frame Art Curator is a Codex skill that makes that decision one photo at a time. A vision model looks at the composition and picks a treatment from a small design system. Pillow then renders the result from the verified source file at exactly 1920x1080. The model never redraws the photograph.

The skill also handles the less glamorous part: source hashes, comparison sheets, upload journals, Home Assistant rotation, and Samsung's occasional habit of accepting an upload while dropping the response.

> [!IMPORTANT]
> Rendering is offline. Live uploads and deletes are separate steps, with an approval gate between them. A larger batch uses up to six canaries; a smaller batch uses every incoming photo.

## What it does

- Finds new photos and waits until file copies are stable.
- Looks for a larger, verified original before using a preview or old TV export.
- Records the focal point, protected edges, crop limit, matte, and framing choice for each image.
- Renders an exact sRGB panel and a side-by-side comparison sheet.
- Tests up to six different types of photo before changing an existing library.
- Reconciles TV inventory after an unclear upload response instead of blindly retrying.
- Builds rotation from the IDs that are actually present on the TV.

Landscape, portrait, and square files can be sorted separately. The included renderer currently targets a 1920x1080 landscape Frame.

## Framing choices

The presets are deliberately limited. Per-photo judgment is useful; ninety unrelated templates are not.

| Treatment | When it fits | What the renderer does |
| --- | --- | --- |
| `float_pano` | Very wide photos | Shrinks the complete panorama into a floating print with generous space around it. |
| `museum_light` | Most landscapes, scans, and edge-sensitive shots | Contains the full photo on a warm, cool, or edge-derived neutral matte. |
| `museum_dark` | Night scenes and low-key interiors | Contains the photo on charcoal without turning the border into a sampled neon color. |
| `minimal_crop` | Photos already close to the panel ratio | Uses the recorded focal point and normally removes no more than 1 to 3 percent. |
| `full_bleed` | Images composed close to 16:9 | Fills the panel when it can do so without harming the composition. |
| `soft_extension` | Named experiments only | Places the complete sharp image over a subdued blur made from the same source. |
| `diptych_portrait` | Two portraits linked by time, place, story, or palette | Places both complete photos on one thick matte with a real center gutter. |

Adaptive mattes start with the image edges, then pull the result toward a small neutral palette and reduce its saturation. This gives the border a relationship to the photo without producing a bright green or red frame.

Diptychs use the same approach across both images. Capture time and location carry more weight than a loose color match, and left/right order can be flipped so a gaze or leading line points into the pair. Photos with no convincing partner stay on their own.

The full policy lives in [references/design-system.md](references/design-system.md).
Manifest fields and copyable examples live in [references/art-direction-schema.md](references/art-direction-schema.md).

## How a run works

1. Inventory the live library and freeze the input files.
2. Match each item to the best available original and verify its SHA-256 hash.
3. Let the vision model write an art-direction manifest.
4. Render the panels and comparison sheets with Pillow.
5. Validate every source hash, crop limit, output hash, and panel dimension.
6. Inspect the results. Open panoramas, faces, buildings, scans, and edge-heavy compositions individually.
7. Upload up to six canaries while leaving the old rotation untouched. If the batch is smaller, test the whole batch.
8. After approval, upload the rest and exercise every new TV ID.
9. Delete only the exact old IDs that have proven replacements.
10. Rebuild rotation from a fresh TV inventory, then test one manual change and two scheduled changes.

That pause after step 6 is intentional. A technically valid crop can still be the wrong artistic choice.

## Requirements

Offline preparation needs:

- Codex with local skill support
- Python 3.10 or newer
- [Pillow](https://pillow.readthedocs.io/)
- read access to the photo library and audit files
- enough disk space for staged originals, rendered panels, and rollback data

Live synchronization also needs a Samsung Frame with a reachable local Art API. Home Assistant rotation can use [TheFab21/ha-samsungtv-smart](https://github.com/TheFab21/ha-samsungtv-smart).

## Install

Clone the repository into the Codex skills directory:

```bash
git clone https://github.com/rohanpandula/curate-frame-art.git "$CODEX_HOME/skills/curate-frame-art"
cd "$CODEX_HOME/skills/curate-frame-art"
python3 -m pip install Pillow
```

If `CODEX_HOME` is not set, replace it with the path to your Codex home. Restart Codex if the skill does not appear, then invoke it as `$curate-frame-art`.

## Set up the local profile

Copy the example and fill in paths, device addresses, Home Assistant entities, and current audit anchors:

```bash
cp references/environment.example.md references/environment.local.md
```

`environment.local.md` is ignored by Git. Keep tokens and passwords somewhere else, such as a protected secret file or secret manager.

The catalog helper currently reads the three-batch audit shape described under [Current limits](#current-limits). It requires explicit paths, so a checkout cannot silently inherit somebody else's machine settings:

```bash
python3 scripts/build_catalog_from_audits.py \
  --first-stage ./local/stages/first/stage-manifest.json \
  --remaining-stage ./local/stages/remaining/stage-manifest.json \
  --new-stage ./local/stages/additions/stage-manifest.json \
  --first-upload ./local/audits/first-upload.json \
  --remaining-upload ./local/audits/remaining-upload.json \
  --new-upload ./local/audits/additions-upload.json \
  --final-audit ./local/audits/final-library.json \
  --output ./run-artifacts/source-catalog.json
```

It stops on incomplete audits, duplicate IDs, changed payloads, missing staged originals, or a final ID set that does not match the mapped library.

New files do not need upload history. Build a private source catalog directly from a folder:

```bash
python3 scripts/build_source_catalog.py \
  --input /path/to/portfolio/Portrait \
  --orientation portrait \
  --output ./run-artifacts/portrait-sources.json
```

That command takes two size and modification-time snapshots before hashing. It records decoded orientation, dimensions, capture time, GPS when present, camera details, and a simple color summary without changing the source files.

Add `--redact-gps` when location is unnecessary or when the catalog may leave the private workstation.

Render and validate a single-photo batch:

```bash
python3 scripts/render_frame_batch.py \
  --catalog ./run-artifacts/source-catalog.json \
  --art-direction ./run-artifacts/art-direction.json \
  --output-dir ./run-artifacts/single-photos

python3 scripts/validate_rendered_batch.py \
  --catalog ./run-artifacts/source-catalog.json \
  --art-direction ./run-artifacts/art-direction.json \
  --render-manifest ./run-artifacts/single-photos/render-manifest.json \
  --output ./run-artifacts/single-photos/validation.json
```

Portrait pairs use the generic source catalog and their own multi-source renderer:

```bash
python3 scripts/render_portrait_diptychs.py \
  --catalog ./run-artifacts/portrait-sources.json \
  --art-direction ./run-artifacts/portrait-diptychs.json \
  --output-dir ./run-artifacts/diptychs

python3 scripts/validate_portrait_diptychs.py \
  --catalog ./run-artifacts/portrait-sources.json \
  --art-direction ./run-artifacts/portrait-diptychs.json \
  --render-manifest ./run-artifacts/diptychs/diptych-render-manifest.json \
  --output ./run-artifacts/diptychs/validation.json
```

## Use it

Start offline:

```text
Use $curate-frame-art to inspect the new landscape photos, find the best originals, and make comparison sheets. Stop before any TV upload.
```

Test the presentation:

```text
Use $curate-frame-art to prepare six varied canaries from the approved previews. Leave the current rotation playlist unchanged.
```

Commit an approved batch:

```text
Use $curate-frame-art to sync the approved photos, verify every new TV ID, and rebuild the one-minute Home Assistant rotation with a rollback copy.
```

Recover an unclear upload:

```text
Use $curate-frame-art to reconcile the last upload attempt. Do not retry unless the inventory proves that the TV did not accept it.
```

## Run artifacts

Keep each run in its own private directory:

```text
run-artifacts/
├── source-catalog.json
├── art-direction.json
├── single-photos/
│   ├── renders/
│   ├── contact-sheets/
│   ├── render-manifest.json
│   └── validation.json
├── diptychs/
│   ├── rendered/
│   ├── contact-sheets/
│   ├── diptych-render-manifest.json
│   └── validation.json
├── upload-journal.json
└── rollback/
```

These files can contain photo names, hashes, local paths, thumbnails, TV IDs, and network details. The repository ignores `run-artifacts/` by default.

If you put artifacts inside a different repository, add that directory to its ignore file before the first run.

## Safety rules

The skill treats live work as a transaction:

- A missing socket acknowledgement means unresolved. It does not mean failed.
- An unresolved upload is not retried until a fresh inventory proves what happened.
- Home Assistant rotation and the custom Samsung integration are stopped before direct Art API writes.
- Existing photos stay on the TV until their replacements pass selection, readback, thumbnail, and fresh-session checks.
- Deletes use an exact recorded ID map. There is no broad delete step.
- The old automation is saved before a new playlist is written.

Backups still matter. The checks reduce the chance of a bad mutation; they do not replace copies of the original photos.

## Troubleshooting

### Error 4000

Error 4000 can appear when rotation points at a stale personal-art ID. Stop rotation, open a fresh Art session, and inventory the IDs that exist now. Confirm the target's dimensions and thumbnail, then select it and read the state back. Rebuild the playlist only from IDs that pass those checks.

A power cycle may clear the screen, but it cannot repair a stale playlist.

### The upload response disappeared

The TV may save an image and reset the socket before returning a clean response. Record the attempt as unresolved. Adopt the new item only if the inventory increased by exactly one and the candidate has the expected timestamp, dimensions, thumbnail, and selection behavior in a fresh session.

If two candidates appeared or the evidence is incomplete, stop for manual review.

### The Samsung matte will not stick

Set the TV matte to `none` and bake the visible border into the 1920x1080 image. Reselect the image after a matte repair and verify it again after the TV settles.

### A photo looks soft or cropped

Check the source path and hash in the render manifest. The renderer should start from the staged original, not an old TV payload. A 3:2 or 4:3 source usually needs `museum_light`, `museum_dark`, or `float_pano` if the full composition matters.

## Privacy

Do not commit the local profile, run artifacts, photos, thumbnails, tokens, content IDs, or audit logs. Photo analysis may send images to the configured vision provider, so choose a provider and data policy that fit the library.

Keep TV and Home Assistant access on a trusted network. Internet blocking and firewall policy are outside this skill.

## Test before publishing

```bash
python3 "$CODEX_HOME/skills/.system/skill-creator/scripts/quick_validate.py" .
python3 -m py_compile scripts/*.py
python3 scripts/self_test.py
```

The self-test creates synthetic photos in a temporary directory, runs the single-photo and diptych paths, checks no-overwrite behavior, and removes the fixture when it finishes.

## Current limits

- The audit-backed catalog adapter models two historical replacement batches plus one additions batch. New folders use the generic source cataloger instead.
- Live TV and Home Assistant control depends on installation-specific helpers and entity names rather than one portable command.
- The renderer targets landscape 1920x1080 panels. Portrait and square files can be sorted, but they need a separate display profile.
- The folder cataloger accepts JPEG, PNG, TIFF, and WebP. HEIC needs a Pillow-compatible decoder and is not enabled by default.
- Samsung Art API behavior varies by model and firmware. Error 4000 does not have a sufficiently clear public definition to assign one universal cause.
- `soft_extension` remains a canary-only treatment.
- Generative outpainting, object removal, and content-aware fill stay off unless the user names the images and asks for them.

## License

[MIT](LICENSE)
