# Supplemental linked design knowledge

`data/linked-library.json` is independent of the primary useAnimations corpus.
It covers the 33 other canonical destinations in `data/discovery.json`.
The two useAnimations destinations belong to the primary importer and are
explicitly excluded in `data/linked-import-report.json`.

Each destination has original editorial guidance and an original illustrative
example. Several entries also contain independent React or Flutter adaptations.
Fragments require integration and are not a promise that upstream APIs or
dependencies are bundled.

## Redistribution boundary

No proprietary gallery screenshots, articles, template sources, prompt packs,
paid skills, third-party artwork, model weights or audio are copied. Restricted
services remain factual metadata with independently written application guidance.
The archived source selections have their actual pinned upstream licenses in
`data/licenses/linked/`; immutable URLs, byte lengths and SHA-256 checksums are
recorded per file in the import report. Exact source files are retained unchanged;
the library-facing assets are JSON wrappers containing the source in an inert
`text` string with the original byte checksum and license provenance. The existing
JSON asset API can retrieve these references without rendering HTML or executing
JavaScript. Wrappers are explicitly marked as derived containers, not unmodified
upstream files.

MIT, Apache, Unlicense, LGPL and GPL are not interchangeable. In particular:

- Remotion has a custom commercial eligibility license, **not MIT**; no source
  from it is redistributed.
- p5 references are unmodified LGPL source, not a MIT relicense.
- OBS and FFmpeg contribute license evidence only, not binaries or large source
  trees. FFmpeg obligations depend on the actual configured build.
- OGL's actual README contains the license evidence; a missing `LICENSE` file
  has not been invented.
- Voice/transcription/editor projects contribute only license evidence; model,
  speaker, sample, footage and dependency rights remain separate.
- Original inline editorial guidance and examples are dedicated under CC0-1.0.
  This dedication does not supersede the upstream licenses.

## Reproduce the explicit selection

From the project root:

```sh
python3 data/assets/linked/import-builder.py --allow-network
```

This fetches only allowlisted public HTTPS files at fixed commits. It does not
clone repositories, fetch dependencies or execute upstream content. It limits
each response to 250,000 bytes and aggregate transferred bytes to 25,000,000.
The script checks public DNS addresses on each request and redirect. This is
not a DNS-pinned transport or an arbitrary-URL crawler. GitHub discovery API
rate limits do not affect reproduction because source revisions are already
pinned. No network runs without the explicit flag.

For offline integrity checks, compare every report file's SHA-256 and byte size
with its local data-root path, verify 33 linked source URLs against the canonical
manifest minus the two primary targets, and load the supplemental manifest
through the server's normal strict schema loader. Never open downloaded HTML
as an application just to inspect it: read it as inert source.

The reproducible zero-network validator performs those checks and exercises
the actual loader plus all 28 inert JSON source assets:

```sh
.venv/bin/python data/assets/linked/import-builder.py --validate-only
```
