# Curve Toolkit

Blender addon for curve modeling helpers and bone chain generation.

## Requirements

- Blender 4.2 or newer
- A curve object selected as the active object

## Install

1. Zip the repository files so `blender_manifest.toml` is at the zip root.
2. In Blender, open `Edit > Preferences > Get Extensions`.
3. Use `Install from Disk` and select the zip.
4. Enable `Curve Toolkit`.

Uninstall the old `Bone to Curve` or `Hair Modeling Toolkit` extension before installing this version because the extension id changed.

## Curve Controls

The panel is available at `View3D > Sidebar > Curve Toolkit`.

- `Reset Path`: straightens open curve splines from root toward tip, preserves current evaluated path length, and resets twist.
- `X Axis`: moves all open curve points to the curve X center while preserving each point's Y and Z position, then resets twist.
- `Switch Direction`: reverses open curve spline direction.
- `Origin To` Root, Tip, Center: moves the curve origin without moving the visible curve. Center uses the midpoint by evaluated path length.
- `3D Cursor To` Root, Tip, Center: snaps the 3D cursor to the same curve positions.

Closed curve splines are protected from destructive edit operations.

## Resolution Batch

- `Add Collection`: chooses a collection and registers it with the add button. Child collections are included.
- Registered collections can be removed from the list with the remove button.
- `Paths` and `Bevel Refs`: show how many path curves and bevel reference curves are currently detected.
- `Path Resolution`: live updates `resolution_u` and `render_resolution_u` for every detected path curve.
- `Bevel Reference Resolution`: live updates `resolution_u` and `render_resolution_u` for every unique curve used as a path bevel object.
- `Refresh`: reports the current target counts without changing any resolution values.

## Smooth / Reset

- `Factor` and `Steps` control smoothing strength.
- `Smooth Scale`: smooths curve point radius values.
- `Smooth Curve`: smooths selected curve points when any points are selected; otherwise smooths the full curve. Each contiguous selected range keeps its first and last selected point fixed as reference points.
- `Smooth Twist`: smooths curve point tilt values.
- `Reset Scale`: sets curve point radius values to `1.0`.
- `Reset Curve`: uses the same behavior as `Reset Path`.
- `Reset Twist`: sets curve point tilt values to `0`.

## Locks

- `Lock Twist`: stores the current twist state and preserves it when curve points move without changing Blender's twist method.
- `Unlock Twist`: releases the toolkit twist lock without changing the current curve shape.
- `Flip Twist`: adds 180 degrees to the current curve point twist values.
- `Lock Root` and `Lock Tip`: store the current endpoint world position for each open spline and keep it fixed while the lock is active.
- `Unlock Root` and `Unlock Tip`: bake the locked endpoint position into the curve before releasing the stored endpoint lock.
- Lock buttons show their active state in the panel.

## Mirror

`Duplicate Mirror` duplicates all selected curve objects and mirrors them across global X center. Object and data names swap common side tokens such as `.L` and `.R`; names without a side token receive `_mirror`.

## Caps

`Root`, `Tip`, and `Ends` close curve geometry caps without changing point radius scale. `Open Caps` disables cap filling.
Caps buttons show the current cap state in the panel.

## Rigging

`Generate Bones From Active Curve` creates a new armature in the same collection as the curve. Each spline with at least two usable control points becomes one connected bone chain.

The number of bones follows the number of control points. Bone positions are resampled along Blender's evaluated curve path, so Bezier and NURBS path curves follow their visible shape instead of drawing straight lines between control points.

`Generate Custom Bones` creates a new armature with the chosen `Bone Count`. `0` follows the target node interval count.

- `End To End`: ignores target nodes and fills from curve root to tip.
- `From Root`: starts from the root side of the target node range.
- `From Tip`: starts from the tip side of the target node range while keeping every bone tail pointed toward the tip.
- `Start Node` and `End Node` use 1-based node numbers. `0` means auto, so `0-0` uses the full spline.

`Invert Selected Bones` reverses selected armature bones and flips connected parent order. It is available when the active object is an armature.

For a single spline, the first bone uses the curve object name. Child bones append a three digit suffix to that full name.

For multiple splines, each chain gets a stable spline suffix before the bone number, for example `Curve_spline.001`.

Supported curve primitives: Bezier, Circle, NURBS Curve, NURBS Circle, and Path.
