# Bone to Curve

Blender addon for generating connected bone chains from the active curve path.

## Requirements

- Blender 4.2 or newer
- A curve object selected as the active object

## Install

1. Zip the repository files so `blender_manifest.toml` is at the zip root.
2. In Blender, open `Edit > Preferences > Get Extensions`.
3. Use `Install from Disk` and select the zip.
4. Enable `Bone to Curve`.

## Usage

1. Select one curve object.
2. Open `View3D > Sidebar > Bone to Curve`.
3. Click `Generate Bones From Active Curve`.

The addon creates a new armature in the same collection as the curve. Each spline with at least two usable control points becomes one connected bone chain.

The number of bones follows the number of control points. Bone positions are resampled along Blender's evaluated curve path, so Bezier and NURBS path curves follow their visible shape instead of drawing straight lines between control points.

## Naming

For a single spline, the first bone uses the curve object name. Child bones append a three digit suffix to that full name.

For multiple splines, each chain gets a stable spline suffix before the bone number, for example `Hair_spline.001`.

## Notes

- The addon uses control point count for bone count.
- Bone positions follow the sampled curve path.
- Supported curve primitives: Bezier, Circle, NURBS Curve, NURBS Circle, and Path.
- Bone direction follows the curve point order.
- The curve is not converted, deleted, parented, or constrained.
