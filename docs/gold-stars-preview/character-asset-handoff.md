# Character renderer adapter contract and asset handoff

This document describes the **current lab renderer contract** and the requirements for replacing the temporary static face fallback with a final character asset.

## Current renderer adapter contract

The lab renderer exposes `window.__goldStarCharacterAdapter` with this contract:

- `idle()`
- `noticeNewStar()`
- `tapReaction()`
- `collecting()`
- `celebrate()`
- `rewardApproved()`
- `setReducedMotion(enabled: boolean)`
- `dispose()`
- `getState()`

### Contract behavior (current fallback)

- The approved connected five-point star body remains the base mesh.
- The active fallback face is static and simple:
  - one symmetric closed smile contour
  - two friendly eyes
  - no open mouth interior
  - no animated eyelids
  - no dimple/crease layers
- Action methods currently map to lightweight body pose hints only.
- `setReducedMotion(true)` suppresses body pose offsets and keeps static presentation.
- `dispose()` releases renderer and scene resources.

## Asset replacement requirements

### Required

1. **Format and loader**
   - Use a locally available format supported by Three.js without paid services.
   - Preferred: GLB/GLTF loaded via Three.js `GLTFLoader`.

2. **Action mapping**
   - The replacement must map these adapter methods:
     - `idle`
     - `noticeNewStar`
     - `tapReaction`
     - `collecting`
     - `celebrate`
     - `rewardApproved`

3. **Reduced motion**
   - Must support `setReducedMotion(enabled)` and provide static/minimal-motion output when enabled.

4. **Coordinate and framing conventions**
   - Character should face camera at default front view.
   - Root pivot near center-bottom for stable grounding.
   - Must fit existing lab camera presets (front, three-quarter, side) without clipping.

5. **Material and texture expectations**
   - Warm golden character look under current scene lighting.
   - Textures/materials must be local project assets.
   - If textures are used, include normal/roughness/metalness maps only as needed; avoid heavy, unnecessary map stacks.

6. **License and attribution**
   - Asset source, license type, and required attribution must be documented in this folder.
   - Any usage restrictions must be explicitly listed before integration.

### Optional

- Facial morph targets (blendshapes) for richer expressions.
- Skeletal animation clips for body acting.
- Separate eye controls for gaze/blink.
- Multiple LODs for performance tuning.
- Alpha/transparency textures where genuinely needed (must not create edge artifacts).

## Preview workflow before integration

Before wiring a new asset into child board screens:

1. Load the candidate only in the isolated character lab.
2. Verify each adapter method produces a visible, distinct result.
3. Verify reduced-motion behavior.
4. Capture a filmstrip and short recording in `docs/gold-stars-preview/`.
5. Confirm no changes to rewards accounting APIs or production data paths.
