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

`Preview` shows a temporary ghost curve for selected points only. It does not change the original curve until an apply button is pressed. Preview refresh is debounced and skipped when the generated ghost would exceed the safe point cap, so heavy settings do not lock Blender. `Clear Preview` removes the ghost preview.

Raw Distribution:

- `Distribution`: chooses `Evenly` or `Curve`.
- `Curvature Bias`: controls how strongly `Curve` distribution concentrates points in curved areas. The slider is active only when `Curve` is selected.
- `Apply Distribution`: redistributes control point placement along the current evaluated path using shape-preserving sampling. Bezier handles follow evaluated tangents, small full NURBS splines use a safe interpolation fit, and high-density NURBS uses direct path samples to avoid folded control polygons.

Subdivide:

- `Subdivide Cuts`: controls how many new points are inserted between each selected segment.
- `Sub Distribution`: optionally redistributes the subdivided selection with `None`, `Evenly`, or `Curve`.
- `Subdivide Selected`: inserts data-level cuts between selected points, then applies the chosen sub distribution when it is not `None`. Full NURBS splines use knot refinement for safer shape preservation. Preview uses the same data path as Apply, not Blender's edit-mode subdivide operator.

Decimate:

- `Reduction Factor`: controls how much of each selected point run is removed.
- `Distribution`: chooses `Evenly` or `Curve` for the remaining points.
- `Decimate Selected`: rebuilds selected runs with fewer points sampled from the evaluated visual path, so the drawn curve is preserved instead of simply deleting raw controls.
- NURBS decimation keeps a conservative minimum count based on spline order when needed, because dropping below that point count can visibly collapse the drawn curve.

Auto:

- `Auto Detail`: controls how aggressively automatic subdivision adds cuts in curved areas.
- `Auto Subdivide`: detects curvature and chord deviation, inserts cuts only where needed, then applies a safe preserve pass. Full NURBS splines use uniform knot refinement so bevel surfaces keep their original shape much more reliably.

Fit:

- `Fit To Visual Path`: moves the control path closer to the current evaluated visual path without changing point count.

Distribution preview and apply require at least 2 contiguous selected points. `Subdivide Selected` also requires at least 2 contiguous selected points. `Decimate Selected` requires at least 3 contiguous selected points. `None` sub distribution keeps the cut operation minimal while using a NURBS safety refit for full selected splines when needed. `Evenly` and `Curve` sub distribution redistribute the selected subdivided range after cuts are inserted. `Auto Subdivide` uses integrated curvature and chord deviation so low-curvature intervals receive few cuts or no cuts. `Fit To Visual Path` remains separate and can still process the whole open spline when no points are selected because it intentionally projects controls to their current evaluated positions and can change shape. Segment Control keeps object transforms, bevel references, caps, and materials stable.

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

## Collection Manager

Collections:

- `Add Collection`: chooses a collection and registers it with the add button. Child collections are included.
- Registered collections can be removed from the list with the remove button.

Collection Control:

- `Object Type`: filters batch rename targets by `All`, `Curve`, `Mesh`, or `Armature`.
- `Prefix`, `Base Name`, and `Suffix`: define the generated object name.
- `Start Number` and `Padding`: define the sequence number. Example: `Hair_001`.
- `Batch Rename Objects`: renames objects in registered collections recursively. Objects found through multiple collections are renamed once. Only object names are changed, not shared data-block names.

Resolution Control:

- `Paths` and `Bevel Refs`: show how many path curves and bevel reference curves are currently detected.
- `Path Resolution`: live updates `resolution_u` and `render_resolution_u` for every detected path curve.
- `Bevel Reference Resolution`: live updates `resolution_u` and `render_resolution_u` for every unique curve used as a path bevel object.
- `Refresh`: reports the current target counts without changing any resolution values.

## LOD Tools

`Draft`, `Work`, and `Final` apply path resolution, bevel reference resolution, and cap presets to selected curves. If no curves are selected, they use Collection Manager collections.

## Selection Tools

- Point selection: roots, tips, all points, or clear point selection.
- Curve selection by length threshold: shorter or longer than the configured Length value.

## Validation

- `Check Curves`: checks selected curves, or all scene curves if none are selected.
- The report counts missing bevel objects, zero-length splines, overlapping points, non-applied object scale, closed splines, and active twist locks.
- `Select Problems`: selects curves found by the latest validation.

## Mirror

`Mirror Axis` chooses the global axis center used by `Duplicate Mirror`: X, Y, or Z. `Duplicate Mirror` duplicates all selected curve objects and mirrors them across that axis center. Object and data names swap common side tokens such as `.L` and `.R`. Names without a side token are treated as left-side sources, so the original receives `.L` and the mirrored duplicate receives `.R`. Mirrored curves also preserve visual twist orientation for asymmetric bevel or drawn curve profiles.

`Select L` and `Select R` select left-side or right-side curve objects in the current mirror collection scope. If curve objects are selected, the scope is their direct collections and child collections are not included. If no curve is selected, the selected or active collection is used as a root and child collections are scanned recursively. Non-curve objects in the scope are ignored.

`Remove Duplicate` removes the opposite-side mirror duplicate for selected curve objects and keeps the selected side intact. If both sides are selected, the selected counterpart is skipped so the command does not delete user-selected curves.

## Convert / Bridge

- `Export Mesh Copy`: creates evaluated mesh copies of selected curves in a separate collection without deleting or converting the originals.
- `Curves Object to Curve`: converts an active Blender Curves object into a legacy Curve object so the toolkit can edit it.

## Rigging

`Generate Bones From Active Curve` creates a new armature in the same collection as the curve. Each spline with at least two usable control points becomes one connected bone chain.

The number of bones follows the number of control points. Bone positions are resampled along Blender's evaluated curve path, so Bezier and NURBS path curves follow their visible shape instead of drawing straight lines between control points.

In the panel this is shown under `From Control Points` with the `Generate From Points` button.

`Generate Custom Bones` creates a new armature with the chosen `Bone Count`. `0` follows the target segment range.

In the panel this is shown under `Custom Count` with the `Generate Custom Count` button.

- `End To End`: ignores segment range and fills from curve root to tip.
- `From Root`: reads `Start Segment` and `End Segment` from root toward tip.
- `From Tip`: reads `Start Segment` and `End Segment` from tip toward root.
- `Start Segment` and `End Segment` use direction-relative segment boundaries. Start `0` means the fill start endpoint. End `0` means the fill end endpoint, so `0-0` uses the full spline.
- Example: `From Tip` with `Start Segment` 0, `End Segment` 3, and `Bone Count` 0 creates 3 bones starting from the tip toward the root.
- If `Bone Count` is greater than 0, the selected range stays the same and is resampled to that exact bone count.

`Generate Custom Count` uses the `Distribution` and `Curvature Bias` values from Bone Control.

`Invert Selected Bones` reverses selected armature bones and flips connected parent order. It is available when the active object is an armature.

`Bind Hooks` adds Hook modifiers from selected curve points to the nearest selected or active armature bones. `Clear Hooks` removes Hook modifiers from selected curves.

For a single spline, the first bone uses the curve object name. Child bones append a three digit suffix to that full name.

For multiple splines, each chain gets a stable spline suffix before the bone number, for example `Curve_spline.001`.

Supported curve primitives: Bezier, Circle, NURBS Curve, NURBS Circle, and Path.

## Bone Control

`Bone Control` applies the same visual path sampling to existing armatures. Keep the curve as the active object, select the target armature too, and select a linear connected bone chain before running these tools.

- `Preview`: shows a temporary ghost armature for the selected bone chain. It does not change the original armature until an apply button is pressed.
- `Clear Preview`: removes the ghost bone preview.
- `Distribution`: chooses `Evenly` or `Curve` for generated custom bones and selected bone chains.
- `Curvature Bias`: controls how strongly `Curve` distribution concentrates joints in curved areas.
- `Apply Bone Distribution`: repositions the selected bone chain onto the active drawn curve without adding bones.
- `Subdivide Cuts`: controls how many new bones are inserted into each selected bone.
- `Subdivide Selected Bones`: subdivides the selected chain, then resamples the result onto the active drawn curve immediately.
- `Reduction Factor`: controls how much of the selected bone chain is removed by decimation.
- `Decimate Selected Bones`: removes bones from the selected chain, then resamples the remaining chain onto the active drawn curve immediately.

Preview is off by default so normal selection or transform work does not create any automatic visual changes. When preview is enabled, selection and setting changes rebuild only the ghost object. Pressing an apply button commits the current result to the original object and clears the ghost.
