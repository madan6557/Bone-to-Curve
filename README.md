# Curve Toolkit

Blender addon for curve modeling helpers and bone chain generation.

## Requirements

- Blender 4.2 or newer
- A curve object selected as the active object

## Install

1. Zip the repository files so `blender_manifest.toml` is at the zip root.
2. In Blender, open `Edit > Preferences > Addon`.
3. Use `Install from Disk` and select the zip.
4. Enable `Curve Toolkit`.

Uninstall older builds before installing this version if the extension id changed.

## Curve Controls

The panel is available at `View3D > Sidebar > Curve Toolkit`.

- `Reset Path`: straightens open curve splines from root toward tip, preserves current evaluated path length, and resets twist.
- `X Axis`: moves all open curve points to the curve X center while preserving each point's Y and Z position, then resets twist.
- `Switch Direction`: reverses open curve spline direction.
- `Flip Twist`: adds 180 degrees to the current curve point twist values.
- `Origin To` Root, Tip, Center: moves the curve origin without moving the visible curve. Center uses the midpoint by evaluated path length.
- `3D Cursor To` Root, Tip, Center: snaps the 3D cursor to the same curve positions.

Closed curve splines are protected from destructive edit operations.

## Segment Control

Raw Distribution:

- `Distribution`: chooses `Evenly` or `Curve`.
- `Curvature Bias`: controls how strongly `Curve` distribution concentrates points in curved areas. The slider is active only when `Curve` is selected.
- `Apply Distribution`: redistributes control point placement along the current evaluated path using the selected distribution mode.

Subdivide:

- `Subdivide Cuts`: controls how many new points are inserted between each selected segment.
- `Sub Distribution`: optionally redistributes the subdivided selection with `None`, `Evenly`, or `Curve`.
- `Subdivide Selected`: inserts conventional cuts between selected points, then applies the chosen sub distribution when it is not `None`.

Auto:

- `Auto Detail`: controls how aggressively automatic subdivision adds cuts in curved areas.
- `Auto Subdivide`: detects the strongest curvature intervals, inserts cuts only where needed, then applies curve distribution from the pre-subdivide visual path.

Fit:

- `Fit To Visual Path`: moves the control path closer to the current evaluated visual path without changing point count.

Distribution and fit tools use selected contiguous point ranges when points are selected. If no points are selected, they process the whole open spline. Distribution modes preserve radius and twist profiles while moving control point placement along the current evaluated path. `Subdivide Selected` requires at least 2 contiguous selected points. `None` sub distribution keeps Blender's conventional subdivision. `Evenly` and `Curve` sub distribution redistribute the selected subdivided range after cuts are inserted. `Auto Subdivide` uses integrated curvature so low-curvature intervals receive few cuts or no cuts. `Fit To Visual Path` is separate because it intentionally projects controls to their current evaluated positions and can change shape. Segment Control keeps object transforms, bevel references, caps, and materials stable.

## Smooth / Reset

- `Factor` and `Steps` control smoothing strength.
- `Smooth Scale`: smooths selected point radius values when any points are selected; otherwise smooths the full curve.
- `Smooth Curve`: smooths selected curve points when any points are selected; otherwise smooths the full curve.
- `Smooth Twist`: smooths selected point tilt values when any points are selected; otherwise smooths the full curve.
- Smooth selected mode uses contiguous selected ranges and keeps each range's first and last selected point fixed as reference points.
- `Reset Scale`: sets curve point radius values to `1.0`.
- `Reset Curve`: uses the same behavior as `Reset Path`.
- `Reset Twist`: sets curve point tilt values to `0`.

## Length Tools

- `Length` and `Set Length`: scale each spline from its root to a target length.
- `Match Active Length`: applies the active curve's first spline length to selected curves.
- `Trim Root` and `Trim Tip`: shorten splines by the Trim distance while keeping the same control point count.
- `Store Length` and `Restore Length`: save and restore per-spline lengths on each curve object.

## Surface Tools

- `Surface`: registered mesh object used for snap and collision helpers.
- `Offset`: distance from the surface normal used by snap and push tools.
- `Snap Root`: moves each spline root to the nearest surface point plus offset.
- `Snap Curve`: moves all control points to the nearest surface point plus offset.
- `Offset Curve`: moves the whole curve so the root reaches the surface while preserving the curve shape.
- `Push Out`: only moves points that are closer than the offset distance along the surface normal.

## Locks

- `Lock Twist`: stores the current twist state and preserves it when curve points move without changing Blender's twist method.
- `Unlock Twist`: releases the toolkit twist lock without changing the current curve shape.
- `Lock Root` and `Lock Tip`: store the current endpoint world position for each open spline and keep it fixed while the lock is active.
- `Unlock Root` and `Unlock Tip`: bake the locked endpoint position into the curve before releasing the stored endpoint lock.
- Lock buttons show their active state in the panel.

## Profile / Taper

- Radius presets: `Flat`, `Root Thick`, `Tip Thin`, `Both Thin`, and `Sharp Taper`.
- `Apply Preset`: loads the selected preset into the numeric controls and applies it to selected curves or selected point ranges.
- `Root`, `Middle`, and `Tip` are live adjustment controls. Editing these values reapplies a custom Root-Middle-Tip profile immediately.
- `Copy Profile` and `Paste Profile`: copy the active curve's radius profile and resample it onto selected curves.

## Bevel Manager

- `Assign Bevel`: assigns the chosen curve object as the bevel object for selected curves.
- `Copy Active Bevel`: copies the active curve's bevel object to selected curves.
- `Clear Bevel`: clears bevel object references from selected curves.
- `Select Same Bevel`: selects all scene curves using the active curve's bevel object.

## Caps

`Root`, `Tip`, and `Ends` close curve geometry caps without changing point radius scale. `Open Caps` disables cap filling.
Caps buttons show the current cap state in the panel.

## Resolution Batch

- `Add Collection`: chooses a collection and registers it with the add button. Child collections are included.
- Registered collections can be removed from the list with the remove button.
- `Paths` and `Bevel Refs`: show how many path curves and bevel reference curves are currently detected.
- `Path Resolution`: live updates `resolution_u` and `render_resolution_u` for every detected path curve.
- `Bevel Reference Resolution`: live updates `resolution_u` and `render_resolution_u` for every unique curve used as a path bevel object.
- `Refresh`: reports the current target counts without changing any resolution values.

## LOD Tools

`Draft`, `Work`, and `Final` apply path resolution, bevel reference resolution, and cap presets to selected curves. If no curves are selected, they use Resolution Batch collections.

## Selection Tools

- Point selection: roots, tips, all points, or clear point selection.
- Curve selection by length threshold: shorter or longer than the configured Length value.

## Validation

- `Check Curves`: checks selected curves, or all scene curves if none are selected.
- The report counts missing bevel objects, zero-length splines, overlapping points, non-applied object scale, closed splines, and active twist locks.
- `Select Problems`: selects curves found by the latest validation.

## Mirror

`Duplicate Mirror` duplicates all selected curve objects and mirrors them across global X center. Object and data names swap common side tokens such as `.L` and `.R`; names without a side token receive `_mirror`.

## Convert / Bridge

- `Export Mesh Copy`: creates evaluated mesh copies of selected curves in a separate collection without deleting or converting the originals.
- `Curves Object to Curve`: converts an active Blender Curves object into a legacy Curve object so the toolkit can edit it.

## Rigging

`Generate Bones From Active Curve` creates a new armature in the same collection as the curve. Each spline with at least two usable control points becomes one connected bone chain.

The number of bones follows the number of control points. Bone positions are resampled along Blender's evaluated curve path, so Bezier and NURBS path curves follow their visible shape instead of drawing straight lines between control points.

In the panel this is shown under `From Control Points` with the `Generate From Points` button.

`Generate Custom Bones` creates a new armature with the chosen `Bone Count`. `0` follows the target node interval count.

In the panel this is shown under `Custom Count` with the `Generate Custom Count` button.

- `End To End`: ignores target nodes and fills from curve root to tip.
- `From Root`: starts from the root side of the target node range.
- `From Tip`: starts from the tip side of the target node range while keeping every bone tail pointed toward the tip.
- `Start Node` and `End Node` use 1-based node numbers. `0` means auto, so `0-0` uses the full spline.

`Invert Selected Bones` reverses selected armature bones and flips connected parent order. It is available when the active object is an armature.

`Bind Hooks` adds Hook modifiers from selected curve points to the nearest selected or active armature bones. `Clear Hooks` removes Hook modifiers from selected curves.

For a single spline, the first bone uses the curve object name. Child bones append a three digit suffix to that full name.

For multiple splines, each chain gets a stable spline suffix before the bone number, for example `Curve_spline.001`.

Supported curve primitives: Bezier, Circle, NURBS Curve, NURBS Circle, and Path.
