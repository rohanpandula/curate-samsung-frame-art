# Art-direction manifests

Art direction is a reviewable contract between visual analysis and deterministic rendering. The model chooses the treatment and explains its decision; the renderer handles pixels.

## Record identity

- Existing TV catalogs use `content_id`.
- New source catalogs use `asset_id`.
- Never invent a Samsung content ID for a photo that has not been uploaded.
- The art-direction record must use the same identifier as its catalog record.

## Single-photo document

Required top-level fields:

- `schema_version`
- `canvas` with width `1920`, height `1080`, and color space `sRGB`
- `records`, with one record per selected catalog item

Each record needs:

- `content_id` or `asset_id`
- `treatment`
- `rationale`
- normalized `focal_point` as `[x, y]`
- `protected_edges`, using only `left`, `right`, `top`, and `bottom`
- `max_crop_fraction`
- `matte_strategy`, either `adaptive` or `fixed`
- `margins` with left, right, top, and bottom pixels
- `keyline` and `shadow` settings

A fixed matte also needs `matte_hex`. Set `allow_upscale` only after the user approves enlargement of a named low-resolution source. Do not use it as a batch default.

Put descriptive notes such as `right-side face` or `bottom foreground figure` in `protected_subjects`. Unknown edge tokens are errors; the crop engine never guesses which side a prose label means.

See [single-photo.example.json](single-photo.example.json).

## Portrait diptych document

Use a synthetic `asset_id` for the combined panel and exactly two catalog `source_asset_ids` in left-to-right order. Add pair evidence, the reason for the order, a shared matte, thick outer margins, and a center gutter.

The normal minimums are 64 px outer margins and a 32 px gutter. The preferred gallery range is 120 to 180 px outside and 48 to 80 px between photos.

See [portrait-diptych.example.json](portrait-diptych.example.json).

## Enlargement gate

Rendering fails when a source would be enlarged. A named exception requires both:

1. `allow_upscale: true` on that art-direction record.
2. `acknowledge_upscale_risk: true` at the top level.

The validator records the scale factor. Recovering a larger original remains the preferred fix.
