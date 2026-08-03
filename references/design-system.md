# Frame art direction system

## Principles

- Preserve photographic content and original aspect ratio whenever practical.
- Prefer a coherent gallery over 90 unrelated templates.
- Let composition and geometry choose the layout before color does.
- Use a constrained neutral palette; never use a raw dominant color as the matte.
- Keep Samsung's native matte set to `none`. Bake the visible presentation into the 1920×1080 image.

## Design families

### `float_pano`

Use for aspect ratios above 2.0 or intentionally panoramic compositions.

- Show the complete image.
- Use horizontal margins of 90–140 px and generous vertical breathing room.
- Place the print 1–3% above optical center.
- Use an edge-derived light neutral for daylight or charcoal for night.
- Add a 1–2 px keyline and a restrained soft shadow.

### `museum_light`

Use for standard landscapes, film scans, portraits of places, and images with important edge detail.

- Do not crop unless the manifest explicitly allows at most 3%.
- Use warm archival paper for warm/film images and cool gallery gray for cool images.
- Keep a minimum 32–48 px reveal on every side; accept wider side margins caused by aspect-ratio mismatch.
- Use a fine neutral keyline so white image edges remain legible.

### `museum_dark`

Use for night scenes, low-key photographs, neon, and dark interiors.

- Preserve the whole image.
- Use charcoal or a heavily desaturated edge-derived dark tone.
- Reduce shadow opacity; retain a subtle cool-gray keyline.

### `minimal_crop`

Use only when the source is already close to the inner-window aspect ratio and edge content is expendable.

- Protect faces, horizon, architecture, text, and intentional edge anchors.
- Limit total discarded area to the manifest's crop ceiling, normally 1–3% and never above 5% without approval.
- Center around the recorded focal point, not mechanically around image center.
- Retain a thin 24–40 px reveal.

### `full_bleed`

Use sparingly for images already composed at or extremely near 16:9.

- Allow at most 2% crop.
- Do not use merely to avoid visible matte space.

### `soft_extension`

Optional canary-only treatment for selected images where a hard matte feels distracting.

- Place the complete sharp photograph over a blurred enlargement of itself.
- Darken/desaturate the background so it cannot compete with the print.
- Never use this for the whole library without explicit approval.

### `diptych_portrait`

Use for two portrait-oriented photographs that form a real pair through time, place, subject, or color.

- Preserve both complete photographs unless the art-direction record gives each image its own small crop ceiling.
- Use a thick shared outer matte, normally 120 to 180 px, and a true center gutter of 48 to 80 px.
- Give both images equal optical height. Do not stretch one image to imitate the other's aspect ratio.
- Use one shared neutral matte derived from both images. Never give the two halves competing matte colors.
- Default visible keylines off. Apply the same restrained shadow treatment to both photographs; enable a matching keyline only when an image edge genuinely disappears into the shared matte and the user explicitly approves the outline.
- Record and respect left/right order. Prefer a layout where gaze, motion, architecture, or leading lines point into the pair.
- Reject a proposed pair when chronology is weak and the color or narrative relationship is merely generic.

Rank pair evidence in this order:

1. Same event or close capture time with matching location evidence.
2. Clear narrative sequence or complementary viewpoints.
3. Compatible palette, luminance, film stock, or camera character.
4. Compatible subject scale and aspect ratio.

Do not force every portrait into a diptych. A strong standalone image is better than a weak pairing.

### `diptych_square`

Use for exactly two 1:1 photographs that belong together through time, place, subject, or visual rhythm.

- Require both decoded sources to have equal width and height. A near-square source needs its own reviewed treatment.
- Preserve both photographs in full. Give them the same displayed side length and never stretch one to make it fit.
- Use one shared neutral matte. Keep at least 64 px around the pair and at least 32 px in the center gutter. The usual gallery values are 120 to 180 px outside and 48 to 80 px between images.
- Default visible keylines off. Apply the same restrained shadow, including horizontal and vertical offset, to both photographs.
- Record and respect left/right order. Let gaze, motion, or a repeated shape carry the eye through the pair.
- Do not pair two photographs only because both happen to be square.

## Matte palette

- Gallery white: `#F5F4EF`
- Warm archival: `#F1ECE2`
- Cool gallery gray: `#EDF0F2`
- Soft stone: `#E7E4DE`
- Charcoal: `#191B1E`
- Deep warm charcoal: `#201C1A`
- Edge-derived light: blend a median edge sample 12–18% into gallery white, then heavily desaturate.
- Edge-derived dark: blend a median edge sample 15–25% into charcoal, then heavily desaturate.

Keep light mattes at high luminance and low saturation. Avoid saturated greens, reds, and blues even when prominent in the photo.

## Vision checklist

For every photo record:

1. Subject and focal point.
2. Horizon or strong architectural axes.
3. Faces, text, and irreplaceable edge details.
4. Negative space that may tolerate a tiny crop.
5. Aspect-ratio family, especially panorama status.
6. Overall luminance and warm/cool character.
7. Film/scan character versus crisp digital geometry.
8. Chosen family and why it fits the shared gallery.

Manifest `protected_edges` values are geometric and must be exactly `left`, `right`, `top`, or `bottom`. Put names of people, text, buildings, or other anchors in `protected_subjects`.

For any diptych, run the checklist on both images and also record capture-time delta, location evidence, color relationship, compositional direction, and left/right order.

## Cohesion rules

- Use no more than five design families in one library.
- Use one margin scale within each family.
- Keep outline policy and shadow language consistent within each family.
- Avoid adjacent scheduled photos with violently different matte luminance when playlist ordering can be adjusted safely.
- Prefer a neutral fallback over a clever but uncertain treatment.
