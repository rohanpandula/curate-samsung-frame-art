# Local Frame environment template

Copy this file to `environment.local.md`, fill in the local values, and keep that file private. The repository ignores it by default.

Treat every value as a discovery hint. Re-read the latest completed audits and current device state before a live change.

## Storage

- Portfolio root: `/path/to/photo-portfolio`
- Orientation folders: `Landscape`, `Portrait`, `Square`
- High-resolution originals: `/path/to/original-photo-library`
- Working and audit directory: `/path/to/frame-workspace`
- Immutable replacement stages: `/path/to/frame-replacements`
- Immutable addition stages: `/path/to/frame-additions`

New photos may appear at the portfolio root or inside an orientation folder. Discover recursively and compare names and hashes with completed audits.

## TV and Home Assistant

- Samsung Frame host: `frame-tv.local`
- Home Assistant base URL: `http://homeassistant.local:8123`
- Samsung integration config entry: `<config-entry-id>`
- Rotation entity: `automation.frame_rotate_gallery`
- Media player: `media_player.frame_tv`
- Art mode switch: `switch.frame_tv_art_mode`
- Personal art category: `<discover-from-tv>`

Store credentials in a protected secret file or secret manager. Never put a Home Assistant token, Samsung credential, or other secret in this profile.

## Audit anchors

- Latest completed source-stage manifest: `/path/to/source-stage.json`
- Latest completed upload journal: `/path/to/upload-journal.json`
- Latest completed rotation backup: `/path/to/rotation-backup.json`
- Latest completed rotation test: `/path/to/rotation-test.json`
- Latest portfolio sort verification: `/path/to/orientation-sort-verification.json`

## Device notes

Record installation-specific behavior here, including firmware quirks and tested recovery steps. Keep uncertain causes labeled as hypotheses.

Common Samsung behavior to account for:

- The TV may reset the socket after accepting an upload. A missing acknowledgement is unresolved, not proof of failure.
- Reconcile an unresolved upload before retrying it.
- Build a rotation only from freshly inventoried and selected personal IDs.
- If native matte state drifts, bake the visible presentation into the image and request TV matte `none`.
