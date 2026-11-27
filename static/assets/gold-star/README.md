# Gold Star mascot asset

`star-hop-blink.glb` — the approved animated star used by the kids' Gold Star board
(`static/js/gold_star_mascot.js`) and the 3D lab (`/gold-stars/preview/character-lab`).

| | |
|---|---|
| Size | 12.0 MB, 1 mesh, ~317k triangles, 1 embedded 2048² PNG texture |
| Clip | `greet` (2.8 s): anticipation dip, hop (~0.31 units), squash on landing, blink (shape key `EyeClosure_TEST`) |
| Faces | front: baked reference face; back: plain gold (`StarFrontMask` attribute) |

## How it was made
1. Reference image of an original golden star character (`star_transparent.png`, image-generation tool; provenance/terms not recorded).
2. Hunyuan3D-2.1 Hugging Face Space, "Gen Shape" → `white_mesh.glb`. Settings: model `tencent/Hunyuan3D-2.1/hunyuan3d-dit-v2-1`, steps 30, guidance 5, **seed 4198390**, octree resolution 256.
3. Blender 5.3 (scripted): front projection of the reference, front-only mask, Cycles EMIT bake to 2048² PNG, Principled material (metallic 0, roughness 0.68).
4. Object keyframes on empty `StarMotionPreview` (hop) + shape key blink; glTF export, clip renamed to `greet`.

Editable sources live only on the owner's PC (`Documents\StarMascot\...`): `star-approved-hop-blink.blend` (master),
`star-baked.blend`, `star-base-color.png`, the original `white_mesh.glb` and `star_transparent.png`. **Back these up.**

## Known limits
- The blink squashes the painted pupil (the eye colour is baked into the texture); accepted because it reads well in motion.
- No open-mouth smile, dimples or extra clips yet; other adapter states reuse `greet` plus small code-driven accents.
- Not yet optimised for tablets. Measure load time and frame rate on each child's tablet in the 3D lab before
  decimating; keep this original and compare before/after.

## Licence review: NOT DONE
Check the Hunyuan3D-2.1 licence terms (and the reference image tool's terms) before distributing this asset
outside the family's own Home Hub.
