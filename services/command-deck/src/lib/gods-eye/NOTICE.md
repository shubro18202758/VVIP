# Third-party notices — God's-eye canvas

## gods-eye-view (MIT)

Parts of `src/lib/gods-eye/` are source-level ports from
**gods-eye-view** — https://github.com/bilawalsidhu/gods-eye-view —
MIT License © bilawalsidhu. Not a fork: the borrowed mechanics were
re-implemented against Veer's fixed-endpoint security contract and the
SvelteKit deck, with the origin named at each port site:

| Veer file | Ported from (gods-eye-view) | What was kept |
| --- | --- | --- |
| `registry.ts` | `src/layers/streetLevel/registry.js` | the layer-provider contract (id/name/requiresKeyId/pickPrefix/create + status/init/activate/deactivate/refresh/handlePick/coverageStats) and `validateProviders`' integrity rules (id slug grammar, duplicate ids, pick-prefix overlap) |
| `shaders/flir.ts` | `src/styles/thermal.js` | the FLIR/thermal fragment shader — temperature mapping, Ironbow palette, coherent temporal noise (fbm), hot-spot bloom, sensor pixelation, lens vignette, crosshair HUD, scale bar |
| `share-link.ts` | `src/sharelink.js` | the v=2 camera hash encode (`_buildHashParams`) and the parse-on-load non-finite guards (`parseFloat('Infinity')` is not NaN and must be rejected) |
| `layers/basemap.ts` | `src/sources/capability.js` (pattern) | capability-gated keyed-provider switch (keyless default, `requiresKeyId: 'ion'` upgrade) |

Trimmed vs upstream (documented at each site): the shader's 7-segment digit
renderer and frame counter (the DOM HUD carries that state) and the bloom
kernel (5×5 → 3×3) so software WebGL stays usable on the demo box; the
share link drops the bloom/detection fields Veer does not have.

## OpenStreetMap (ODbL)

The keyless basemap tiles come from `https://tile.openstreetmap.org/{z}/{x}/{y}.png`
— © OpenStreetMap contributors, ODbL. The credit is wired into the imagery
provider and shown in the HUD.
