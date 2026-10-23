# Resident texture sampling and visual detail — 2026-10-23

Read-only investigation of immutable `local/ratchet/source-snapshot-803/anyps3`, cross-checked against current render-scale/tree, and saved `native-virtual-803scale2quiet1` files. No tests/build/game launches or source edits.

**Finding: no confirmed current defect that quantizes ordinary resident RTT filtering back to native texels.** The obvious manual-filter failure is already addressed in snapshot803. This does not refute the confirmed native reconstruction losses in `resscale-quality-loss.md`.

## Exact generated path

`rsx_graphics.py:65–160` generates the graphics fragment shader (there is no `rsx_vk_fragment.py` in this checkout). It starts with the manual sampler in `rsx_graphics_texture.py`, specializes per texture unit, and substitutes resident-image texelFetch. At `rsx_graphics.py:103–113`, for `t.resident_image`, it rewrites dimensions **through the end of filter_, before gradient** to `textureSize(residentN,0).x/y`.

Consequently both nearest `floor(uv*dimensions)` and bilinear `floor(uv*dimensions-.5)` / fractional weights operate on the actual bound physical image; wrap/clamp limits in texel_ also use those dimensions. Native-lane descriptors bind the native parent, whereas visual duplicate binds each available child (`rsx_vk_graphics.py:1110`). The same shader therefore resolves native taps for native draws and fine taps for visual draws. A high-resolution source is not sampled only at its top-left native-sized portion or one fixed pixel per2×2 block.

Example: guest width8, physical child width16, normalized coordinate5/32. The visual filter uses2.5 texel coordinates (nearest index2), while native binding uses1.25 (index1). Rectangle coordinate1.25 is first divided by guest width8, then multiplied by actual width16, with the same fine result. Replacing rectangle normalization with physical width would instead break guest coordinate meaning.

The suffix after gradient intentionally retains guest dimensions for rectangle-coordinate normalization and gradient/LOD calculations (`rsx_graphics_texture.py:49–73`). Resident RTT candidates are restricted to2D, one mip level, noncube, no border storage (`rsx_graphics_rtt.py:69`). Thus there is no missing physical mip-chain adjustment in this supported resident path. Min-versus-mag selection can still differ according to guest LOD; that is not proof of quantization. Ordinary uploaded textures retain their native storage dimensions, correctly: rendering at higher resolution does not create new texture asset detail.

The hardware implicit sampler optimization (`rsx_graphics.py:139`) explicitly excludes `t.resident_image`, so it does not bypass this manual RTT logic. Texture remap/gamma/alpha-kill run after filtering; shadow comparison runs per tap before PCF. No new confirmed defect was found in these paths by source inspection.

Supported typed RTT conversion is a separate operation (`rsx_graphics_rtt.py:168–191`): its source fetch uses the mapped native cell plus fractional destination guest coordinate, multiplied by source actual/native ratio. At current integer2× it can propagate subpixel values into the converted child. That only helps if its source child still contains detail. Unsupported conversions explicitly reconstruct native output, as already documented. The fractional-scale cell-boundary hazard in the earlier contingency design remains separate; production currently uses integer scales.

## Existing evidence and its boundaries

`tests/test_rsx_render_scale.py:113–139` defines a generated producer/consumer oracle:16×16 source with a physical-coordinate color ramp, guest8×8 texture metadata, and a generated guest texture instruction. It compares all source/destination bytes with tolerance1 for normalized and rectangle coordinates. Nearest sampling exercises the exact high-resolution tap issue above. Inspection confirms that this test is present in snapshot803; this task did not rerun it.

Coverage gaps, **not confirmed bugs**: bilinear taps at edges and wrap modes; shadow/PCF; min-versus-mag selection; typed conversion followed by actual generated sampling in one chain; several sequential postprocessing stages. A compact ramp/checker oracle for linear filtering and one end-to-end typed RTT→sample pass would strengthen evidence if a real trace points here. Avoid adding a broad sampler rewrite without a failing independent case.

Saved803 quiet directory has frames, manifests, summary and stderr, but no generated shader dumps or surface/draw dependency trace. The searched log contains no MERGE BIND/BIND_TRACE/RTT-view detail suitable for linking the final world. Older shader dumps (for example native-virtual-311g) exist but cannot establish the803 world pipeline or texture bindings; using them as current evidence would be misleading. Aggregate conversion/direct/merge counts do not identify which shader or image feeds scanout.

## Remaining hypotheses and decisive evidence

Leading source-supported hypothesis remains visual-history collapse in native-only merge/equal-footprint/unsupported RTT before final composition. A second hypothesis is an original-game shader explicitly rounding guest coordinates, applying a fixed native-resolution blur, or sampling a deliberately lower-resolution postprocess source; generated sampler correctness cannot recover detail removed by the original algorithm. Neither is proven as the final-world cause yet.

The planned same-flip native/visual capture can establish whether final pixels contain additional detail. To localize a failure, retain the final scene chain's image IDs/generations, sampled texture units and shader identity, guest/physical texture extents, normalized/rectangle and min/mag settings, plus last native-reconstruction reason. If the visual source is detailed but its consumer output collapses, dump that generated shader and original guest fragment program and capture both sides. If the source is already collapsed, follow ownership transitions upstream. If only HUD has added detail, final “visual draw count” is not a world-quality certificate.

No new confirmed sampling defect was found. Do not infer quality from dimensions or counters, and do not change native guest shader behavior to manufacture sharpness. Any visual-only algorithm change requires an explicit correctness policy while native bytes/queries remain exact.
