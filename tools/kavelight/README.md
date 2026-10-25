# Room scan → KaveLight set

`kl.py` turns a measured room into a [KaveLight](https://kavelight.com) previs set through
KaveLight's MCP connector. It reuses `scan-to-threejs`'s `bundle_to_room.py` for the measuring
and adds the KaveLight side.

```sh
python3 tools/kavelight/kl.py spec <scan> -o set.json --name "Garage" --plan set.png
#   ...review set.json: merge walls, fix heights, add what RoomPlan missed...
python3 tools/kavelight/kl.py plan set.json -o steps.json --room-side 1
python3 tools/kavelight/kl.py run steps.json --out run/      # on the Mac running KaveLight
python3 -m unittest tools/kavelight/test_kl.py
```

## Inputs

- **Polycam capture**: its `optimized_roomplan.json`. Polycam writes Apple RoomPlan under every
  LiDAR room scan, along with the capture's GPS and compass heading, so the set's sun is placed
  correctly with no manual north. A capture shared by link exposes it with the mesh at
  `https://poly.cam/api/capture/<id>/artifacts/optimized_roomplan.json` (and `raw.gltf`,
  `raw_geometry.bin`, `textures/*`; the viewer's network log lists them).
- **3D Model Anything session bundle**: no GPS or heading, so set `site` by hand.
- **room.json** from `bundle_to_room.py`.

## set.json

Set coordinates are metres, +Y up and the floor at 0. Walls are interior-face lines with the room
on the right of `a → b`. Openings are a point `at` on that line plus `width`, `height`, `sill`
and optional `design` (passed to `add_opening`). Props are boxes with `center`, `bottom`, `size`
and `heading_degrees`. `site.minus_z_bearing` is the compass bearing of the set's −Z axis.
`extra_steps` holds raw KaveLight calls such as gables, roofs or stand-ins.

`spec` reports spans of the floor outline that have no wall. RoomPlan leaves an open garage door
as a gap, for example.

## What plan does

Kit walls are centred on their `from → to` line. `plan` pushes each interior line out half a
thickness and re-intersects joined corners, so interior faces stay where the scan measured them
and corner endpoints coincide. `add_opening.along` is measured from the wall's centre toward
`to`. `--room-side` is the `add_opening.side` value that means "room on the right of from → to".

## What run does

`run` serves the steps on `127.0.0.1` and has OpenCode's `creative` agent call `execute` once with
a fixed loop: fetch the steps, call `tools.kavelight[tool](args)` for each one, and post every
result back. No model writes geometry. Results go to `--out/results.jsonl`, and any images
(`render_plan`, `render_monitor`, `build_prop` pictures) are saved next to it. A failing step
stops the run unless that step has `continue_on_error`.

It needs `opencode2` with the `kavelight` MCP connected and authed on that Mac. KaveLight must be
open with a project, signed in, with Controls › AI Assistants on. `run` sets `$PWD` for the child,
because opencode2 picks its location from `$PWD` rather than from the process cwd.
