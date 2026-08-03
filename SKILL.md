---
name: curate-frame-art
description: Art-direct, render, preview, upload, replace, rotate, or organize personal photography for a Samsung Frame TV. Use for requests such as adding photos from a local portfolio, syncing a Landscape folder, changing a photo's orientation, replacing cropped or soft versions, creating adaptive mattes or per-photo framing, testing Frame rotation, or safely reconciling Samsung Art API errors.
---

# Curate Frame Art

Preserve the photograph while using vision-based art direction to choose a coherent layout per image. Render deterministically, review before live changes, and treat every TV mutation as a journaled transaction.

## Route resources

- Read [references/design-system.md](references/design-system.md) before choosing treatments or judging previews.
- Read [references/art-direction-schema.md](references/art-direction-schema.md) before writing a single-photo, portrait-diptych, or square-diptych manifest.
- Read `references/environment.local.md` before accessing a portfolio, NAS, Home Assistant, or TV. If it does not exist, use [references/environment.example.md](references/environment.example.md) to discover the installation and create the ignored local profile.
- Use `scripts/build_catalog_from_audits.py` to bind current TV IDs to preserved source originals.
- Use `scripts/build_source_catalog.py` for new files that do not have TV upload audits yet.
- Use `scripts/render_frame_batch.py` to render exact 1920×1080 outputs and contact sheets.
- Use `scripts/render_portrait_diptychs.py` for approved two-portrait pairings.
- Use `scripts/render_square_diptychs.py` for approved pairs made from two exact 1:1 sources.
- Use `scripts/validate_rendered_batch.py` before any upload.
- Use `scripts/validate_portrait_diptychs.py` before uploading a diptych.
- Use `scripts/validate_square_diptychs.py` before uploading a square diptych.

## Workflow

### 1. Freeze the inputs

1. Discover additions recursively under the configured portfolio root.
2. Compare names and hashes with the latest completed source/upload audits; do not infer "new" from names alone.
3. Require at least two identical size/mtime snapshots before processing files that may still be copying.
4. Decode EXIF orientation. Route landscape, portrait, and square separately.
5. Keep existing TV IDs and source files immutable until replacements are verified.

### 2. Recover the best source

1. Prefer a verified NAS original over an SSD preview.
2. Match exact stems and capture metadata, then verify perceptually.
3. Reject ambiguous matches; never upscale when a larger source exists.
4. Copy selected originals into an immutable, dated SSD stage and verify SHA-256.

### 3. Art-direct each photograph

1. Inspect source contact sheets or individual images with vision.
2. Select one design family from the design-system reference.
3. Record focal point, protected edges, crop ceiling, matte strategy, margins, keyline, and shadow in an art-direction manifest.
4. Make per-photo decisions inside the shared system. Do not invent arbitrary one-off templates.
5. Never use generative alteration, outpainting, object removal, or content-aware fill unless the user explicitly opts in for named images.
6. For portrait diptychs, rank pairs by shared event, capture-time and location evidence, then visual or narrative fit. Record both source hashes and the left/right order. Leave weak matches unpaired.
7. For square diptychs, choose exactly two verified 1:1 sources. Keep both photos complete, give them the same displayed side length, use one shared matte, and record the left/right order. Leave the keyline off unless the user asks for one.

### 4. Render and review

1. Run the deterministic renderer; do not ask a generative image model to reproduce photography.
2. Validate panel dimensions, source/output hashes, crop ceilings, unique outputs, and contact-sheet creation.
3. Inspect every contact sheet. Open panoramas, faces, architecture, edge-sensitive compositions, and low-resolution sources individually.
4. Correct questionable art direction and rerender before touching the TV.

### 5. Test with canaries

1. Choose up to six varied images: panorama, bright landscape, dark/night, architecture, film/scan, and edge-sensitive composition. If the whole incoming batch has fewer than six photos, use the whole batch rather than inventing extra canaries.
2. Back up the exact live Home Assistant automation.
3. Turn rotation off with active actions stopped, disable the custom Samsung integration, and independently confirm both states.
4. Upload canaries additively with TV matte and portrait matte set to `none`; the visible framing must be baked into the panel image.
5. For each upload require exact inventory `+1`, a unique personal ID, 1920×1080 TV record, matching thumbnail, successful select/readback, and no visible TV matte.
6. Leave the existing rotation playlist unchanged until the user approves the canaries.

### 6. Commit an approved batch

1. Upload replacements additively and exercise every new ID.
2. Never delete an old photo until its replacement has survived selection, settling, and fresh-session verification.
3. Delete only exact mapped old IDs after the full replacement set is proven.
4. Rebuild Home Assistant from the final exact ID set.
5. Prove one manual trigger and two consecutive scheduled minute ticks.
6. Leave automation on only after all tests pass.

## Hard safety gates

- Never blindly retry an upload with a missing or ambiguous acknowledgement. Persist the unresolved attempt and reconcile only from exact `+1` inventory, timestamp, dimensions, thumbnail, selection, and fresh-session evidence.
- Never run direct Art API writes while Home Assistant rotation or the custom integration is active.
- Never use a broad delete, stale playlist ID, inferred content ID, or unresolved file glob.
- Require a timestamped selection after every matte repair. Treat repeated matte drift as a hard stop.
- Preserve an exact rollback copy of the Home Assistant automation before saving a new playlist.
- Keep live upload/replace steps separate from offline discovery, rendering, and review.
- Write catalogs, manifests, thumbnails, and contact sheets only to the configured private artifact root. If that root is inside a Git repository, verify it is ignored before rendering.

## Future shorthand

Interpret requests such as "sync my new Frame photos," "pair my new portraits," "pair these squares," "swap this rotated photo," or "run the Frame curator" as this complete workflow. Re-discover current state rather than trusting historical counts.
