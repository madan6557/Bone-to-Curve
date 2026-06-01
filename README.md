# Hair Modeling Toolkit

Blender addon for curve-based hair modeling helpers and bone chain generation.

## Requirements

- Blender 4.2 or newer
- A curve object selected as the active object

## Install

1. Zip the repository files so `blender_manifest.toml` is at the zip root.
2. In Blender, open `Edit > Preferences > Get Extensions`.
3. Use `Install from Disk` and select the zip.
4. Enable `Hair Modeling Toolkit`.

Uninstall the old `Bone to Curve` extension before installing this version because the extension id changed.

## Curve Controls

The panel is available at `View3D > Sidebar > Hair Toolkit`.

- `Reset Path`: straightens open curve splines from root toward tip, preserves current evaluated path length, and resets twist.
- `X Axis`: moves all open curve points to the curve X center while preserving each point's Y and Z position, then resets twist.
- `Switch Direction`: reverses open curve spline direction.
- `Origin To` Root, Tip, Center: moves the curve origin without moving the visible curve. Center uses the midpoint by evaluated path length.
- `3D Cursor To` Root, Tip, Center: snaps the 3D cursor to the same curve positions.

Closed curve splines are protected from destructive edit operations.

## Smooth / Reset

- `Factor` and `Steps` control smoothing strength.
- `Smooth Scale`: smooths curve point radius values.
- `Smooth Curve`: smooths selected curve points when any points are selected; otherwise smooths the full curve. Each contiguous selected range keeps its first and last selected point fixed as reference points.
- `Smooth Twist`: smooths curve point tilt values.
- `Reset Scale`: sets curve point radius values to `1.0`.
- `Reset Curve`: uses the same behavior as `Reset Path`.
- `Reset Twist`: sets curve point tilt values to `0`.
- `Lock Twist`: uses Blender's Z-Up twist mode to prevent unwanted automatic twisting while preserving manual point tilt.
- `Unlock Twist`: restores Blender's default Minimum twist mode while preserving manual point tilt.

## Mirror

`Duplicate Mirror` duplicates all selected curve objects and mirrors them across global X center. Object and data names swap common side tokens such as `.L` and `.R`; names without a side token receive `_mirror`.

## Caps

`Root`, `Tip`, and `Ends` close curve geometry caps without changing point radius scale. `Open Caps` disables cap filling.

## Rigging

`Generate Bones From Active Curve` creates a new armature in the same collection as the curve. Each spline with at least two usable control points becomes one connected bone chain.

The number of bones follows the number of control points. Bone positions are resampled along Blender's evaluated curve path, so Bezier and NURBS path curves follow their visible shape instead of drawing straight lines between control points.

For a single spline, the first bone uses the curve object name. Child bones append a three digit suffix to that full name.

For multiple splines, each chain gets a stable spline suffix before the bone number, for example `Hair_spline.001`.

Supported curve primitives: Bezier, Circle, NURBS Curve, NURBS Circle, and Path.
