# Bone to Curve

Blender addon for generating connected bone chains from the active curve object's control points.

## Requirements

- Blender 4.2 or newer
- A curve object selected as the active object

## Usage

1. Install this repository as a Blender extension or zip it and install from Blender Preferences.
2. Select one curve object.
3. Open `View3D > Sidebar > Bone to Curve`.
4. Click `Generate Bones From Active Curve`.

The addon creates a new armature in the same collection as the curve. Each spline with at least two usable control points becomes one connected bone chain.

## Naming

For a single spline, the first bone uses the curve object name. Child bones append a three digit suffix to that full name.

For multiple splines, each chain gets a stable spline suffix before the bone number, for example `Hair_spline.001`.

## Notes

- The addon uses curve control points, not evaluated samples.
- Bone direction follows the curve point order.
- The curve is not converted, deleted, parented, or constrained.
