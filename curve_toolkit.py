# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Curve Toolkit",
    "author": "madan6557",
    "version": (1, 6, 5),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Curve Toolkit",
    "description": "Curve modeling tools and bone chain generation.",
    "category": "Curve",
}

import json
from math import pi

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty
from mathutils import Quaternion, Vector


MIN_BONE_LENGTH = 1.0e-6
CTK_ENDPOINT_LOCKS_KEY = "ctk_endpoint_locks"
CTK_TWIST_LOCKS_KEY = "ctk_twist_locks"
LEGACY_ENDPOINT_LOCKS_KEY = "hmt_endpoint_locks"
_CURVE_LOCK_HANDLER_RUNNING = False
_RESOLUTION_BATCH_UPDATE_RUNNING = False


def _unique_name(base_name, existing_names):
    if base_name not in existing_names:
        return base_name

    index = 1
    while True:
        candidate = f"{base_name}.{index:03d}"
        if candidate not in existing_names:
            return candidate
        index += 1


def _mirror_side_name(name):
    replacements = (
        (".L", ".R"),
        (".R", ".L"),
        ("_L", "_R"),
        ("_R", "_L"),
        ("-L", "-R"),
        ("-R", "-L"),
        ("Left", "Right"),
        ("Right", "Left"),
        ("left", "right"),
        ("right", "left"),
    )

    for source, target in replacements:
        if source in name:
            return name.replace(source, target)

    return f"{name}_mirror"


def _control_point_count(spline):
    if spline.type == "BEZIER":
        return len(spline.bezier_points)
    if spline.type in {"POLY", "NURBS"}:
        return len(spline.points)
    return 0


def _active_curve(context):
    obj = context.view_layer.objects.active
    if obj is None:
        return None
    if obj.type != "CURVE":
        return None
    return obj


def _active_armature(context):
    obj = context.view_layer.objects.active
    if obj is None:
        return None
    if obj.type != "ARMATURE":
        return None
    return obj


def _spline_points(spline):
    if spline.type == "BEZIER":
        return spline.bezier_points
    return spline.points


def _is_supported_spline(spline):
    return spline.type in {"BEZIER", "POLY", "NURBS"}


def _is_closed_spline(spline):
    return bool(getattr(spline, "use_cyclic_u", False))


def _has_closed_spline(curve_obj):
    return any(_is_closed_spline(spline) for spline in curve_obj.data.splines if _is_supported_spline(spline))


def _editable_splines(curve_obj):
    return [
        spline
        for spline in curve_obj.data.splines
        if _is_supported_spline(spline) and _control_point_count(spline) >= 2
    ]


def _point_local_co(point, spline):
    if spline.type == "BEZIER":
        return point.co.copy()
    return Vector((point.co[0], point.co[1], point.co[2]))


def _set_point_local_co(point, spline, co):
    if spline.type == "BEZIER":
        point.co = co
        return

    point.co = (co.x, co.y, co.z, point.co[3])


def _point_radius(point):
    return float(getattr(point, "radius", 1.0))


def _set_point_radius(point, value):
    if hasattr(point, "radius"):
        point.radius = value


def _point_tilt(point):
    return float(getattr(point, "tilt", 0.0))


def _set_point_tilt(point, value):
    if hasattr(point, "tilt"):
        point.tilt = value


def _transform_point_co(point, spline, old_matrix, new_matrix_inverted):
    old_world = old_matrix @ _point_local_co(point, spline)
    _set_point_local_co(point, spline, new_matrix_inverted @ old_world)


def _transform_bezier_handles(point, old_matrix, new_matrix_inverted):
    point.handle_left = new_matrix_inverted @ (old_matrix @ point.handle_left)
    point.handle_right = new_matrix_inverted @ (old_matrix @ point.handle_right)


def _reset_bezier_handles_to_path(spline):
    points = list(spline.bezier_points)
    if len(points) < 2:
        return

    for index, point in enumerate(points):
        point.handle_left_type = "FREE"
        point.handle_right_type = "FREE"

        if index == 0:
            point.handle_left = point.co
        else:
            point.handle_left = point.co - (point.co - points[index - 1].co) / 3.0

        if index == len(points) - 1:
            point.handle_right = point.co
        else:
            point.handle_right = point.co + (points[index + 1].co - point.co) / 3.0


def _smooth_values(values, factor, steps):
    if len(values) < 3:
        return values

    smoothed = list(values)
    for _ in range(steps):
        next_values = list(smoothed)
        for index in range(1, len(smoothed) - 1):
            target = (smoothed[index - 1] + smoothed[index + 1]) * 0.5
            next_values[index] = smoothed[index] + (target - smoothed[index]) * factor
        smoothed = next_values

    return smoothed


def _point_is_selected(point, spline):
    if spline.type == "BEZIER":
        return bool(getattr(point, "select_control_point", False))
    return bool(getattr(point, "select", False))


def _selected_index_runs(spline):
    runs = []
    current_run = []

    for index, point in enumerate(_spline_points(spline)):
        if _point_is_selected(point, spline):
            current_run.append(index)
            continue

        if current_run:
            runs.append(current_run)
            current_run = []

    if current_run:
        runs.append(current_run)

    return runs


def _has_selected_points(splines):
    return any(_selected_index_runs(spline) for spline in splines)


def _reset_bezier_handles_for_indices(spline, indices):
    points = list(spline.bezier_points)
    for index in sorted(indices):
        if index < 0 or index >= len(points):
            continue

        point = points[index]
        point.handle_left_type = "FREE"
        point.handle_right_type = "FREE"

        if index == 0:
            point.handle_left = point.co
        else:
            point.handle_left = point.co - (point.co - points[index - 1].co) / 3.0

        if index == len(points) - 1:
            point.handle_right = point.co
        else:
            point.handle_right = point.co + (points[index + 1].co - point.co) / 3.0


def _require_editable_open_curve(operator, context):
    curve_obj = _active_curve(context)
    if curve_obj is None:
        operator.report({"ERROR"}, "Active object must be a Curve.")
        return None, []

    if _has_closed_spline(curve_obj):
        operator.report({"ERROR"}, "Closed curve splines are not supported for this operation.")
        return None, []

    splines = _editable_splines(curve_obj)
    if not splines:
        operator.report({"ERROR"}, "Active curve has no editable open spline with at least 2 points.")
        return None, []

    return curve_obj, splines


def _copy_attr(source, target, name):
    if hasattr(source, name) and hasattr(target, name):
        try:
            setattr(target, name, getattr(source, name))
        except TypeError:
            pass


def _copy_curve_settings(source_curve, target_curve):
    for attr_name in (
        "dimensions",
        "resolution_u",
        "render_resolution_u",
        "twist_mode",
        "twist_smooth",
        "use_path",
        "path_duration",
    ):
        _copy_attr(source_curve, target_curve, attr_name)

    target_curve.bevel_depth = 0.0
    target_curve.bevel_resolution = 0
    target_curve.extrude = 0.0
    target_curve.offset = 0.0
    target_curve.bevel_object = None
    target_curve.taper_object = None


def _copy_bezier_point(source_point, target_point):
    for attr_name in (
        "co",
        "handle_left",
        "handle_right",
        "handle_left_type",
        "handle_right_type",
        "tilt",
        "radius",
        "weight_softbody",
    ):
        _copy_attr(source_point, target_point, attr_name)


def _copy_curve_point(source_point, target_point):
    for attr_name in ("co", "tilt", "radius", "weight_softbody"):
        _copy_attr(source_point, target_point, attr_name)


def _copy_spline(source_spline, target_curve):
    target_spline = target_curve.splines.new(source_spline.type)

    if source_spline.type == "BEZIER":
        point_count = len(source_spline.bezier_points)
        target_spline.bezier_points.add(point_count - 1)
        for source_point, target_point in zip(source_spline.bezier_points, target_spline.bezier_points):
            _copy_bezier_point(source_point, target_point)
    else:
        point_count = len(source_spline.points)
        target_spline.points.add(point_count - 1)
        for source_point, target_point in zip(source_spline.points, target_spline.points):
            _copy_curve_point(source_point, target_point)

    for attr_name in (
        "resolution_u",
        "order_u",
        "use_endpoint_u",
        "use_bezier_u",
        "use_cyclic_u",
        "use_smooth",
    ):
        _copy_attr(source_spline, target_spline, attr_name)

    return target_spline


def _component_paths_from_mesh(mesh, matrix_world):
    if not mesh.vertices:
        return []

    adjacency = {index: [] for index in range(len(mesh.vertices))}
    for edge in mesh.edges:
        first, second = edge.vertices
        adjacency[first].append(second)
        adjacency[second].append(first)

    if not mesh.edges:
        return [[matrix_world @ vertex.co for vertex in mesh.vertices]]

    visited_vertices = set()
    paths = []

    for start_index in range(len(mesh.vertices)):
        if start_index in visited_vertices:
            continue

        stack = [start_index]
        component = set()
        while stack:
            vertex_index = stack.pop()
            if vertex_index in component:
                continue
            component.add(vertex_index)
            stack.extend(adjacency[vertex_index])

        visited_vertices.update(component)
        endpoints = [index for index in component if len(adjacency[index]) <= 1]
        current = min(endpoints or component)
        previous = None
        ordered_indices = [current]
        visited_edges = set()

        while True:
            next_index = None
            for neighbor in sorted(adjacency[current]):
                edge_key = tuple(sorted((current, neighbor)))
                if neighbor == previous or edge_key in visited_edges:
                    continue
                next_index = neighbor
                visited_edges.add(edge_key)
                break

            if next_index is None:
                break

            previous = current
            current = next_index
            ordered_indices.append(current)

            if current == ordered_indices[0]:
                break

        paths.append([matrix_world @ mesh.vertices[index].co for index in ordered_indices])

    return sorted(paths, key=lambda path: len(path), reverse=True)


def _evaluated_spline_path_points(context, curve_obj, spline):
    temp_curve = bpy.data.curves.new(f"{curve_obj.name}_path_eval", "CURVE")
    temp_obj = None
    eval_obj = None

    try:
        _copy_curve_settings(curve_obj.data, temp_curve)
        _copy_spline(spline, temp_curve)

        temp_obj = bpy.data.objects.new(f"{curve_obj.name}_path_eval", temp_curve)
        temp_obj.matrix_world = curve_obj.matrix_world.copy()
        _link_target_collection(context, curve_obj).objects.link(temp_obj)
        context.view_layer.update()

        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = temp_obj.evaluated_get(depsgraph)
        mesh = eval_obj.to_mesh()
        try:
            paths = _component_paths_from_mesh(mesh, temp_obj.matrix_world)
        finally:
            eval_obj.to_mesh_clear()

        if not paths:
            return []

        return paths[0]
    finally:
        if temp_obj is not None:
            bpy.data.objects.remove(temp_obj, do_unlink=True)
        bpy.data.curves.remove(temp_curve, do_unlink=True)


def _has_valid_segment(points):
    return any((points[index + 1] - points[index]).length > MIN_BONE_LENGTH for index in range(len(points) - 1))


def _polyline_length(points):
    return sum((points[index + 1] - points[index]).length for index in range(len(points) - 1))


def _point_at_distance(points, distance):
    walked_distance = 0.0

    for index in range(len(points) - 1):
        start = points[index]
        end = points[index + 1]
        segment_length = (end - start).length

        if segment_length <= MIN_BONE_LENGTH:
            continue

        next_distance = walked_distance + segment_length
        if distance <= next_distance:
            factor = (distance - walked_distance) / segment_length
            return start.lerp(end, factor)

        walked_distance = next_distance

    return points[-1]


def _resample_path_by_bone_count(path_points, bone_count):
    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH:
        return []

    return [
        _point_at_distance(path_points, total_length * index / bone_count)
        for index in range(bone_count + 1)
    ]


def _resample_path_segment_by_bone_count(path_points, start_distance, end_distance, bone_count):
    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH or bone_count < 1:
        return []

    start_distance = max(0.0, min(total_length, start_distance))
    end_distance = max(0.0, min(total_length, end_distance))
    if end_distance - start_distance <= MIN_BONE_LENGTH:
        return []

    return [
        _point_at_distance(path_points, start_distance + (end_distance - start_distance) * index / bone_count)
        for index in range(bone_count + 1)
    ]


def _resample_path_by_point_count(path_points, point_count):
    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH or point_count < 2:
        return []

    return [
        _point_at_distance(path_points, total_length * index / (point_count - 1))
        for index in range(point_count)
    ]


def _path_endpoint_data(context, curve_obj, spline):
    path_points = _evaluated_spline_path_points(context, curve_obj, spline)
    if len(path_points) < 2:
        return None

    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH:
        return None

    return path_points[0], path_points[-1], total_length, path_points


def _set_spline_positions(spline, positions):
    points = _spline_points(spline)
    for point, position in zip(points, positions):
        _set_point_local_co(point, spline, position)

    if spline.type == "BEZIER":
        _reset_bezier_handles_to_path(spline)


def _reset_spline_path_to_direction(context, curve_obj, spline, direction):
    endpoint_data = _path_endpoint_data(context, curve_obj, spline)
    if endpoint_data is None:
        return False

    root_world, _tip_world, total_length, _path_points = endpoint_data
    if direction.length <= MIN_BONE_LENGTH:
        return False

    direction = direction.normalized()
    point_count = _control_point_count(spline)
    matrix_inverted = curve_obj.matrix_world.inverted()
    positions = [
        matrix_inverted @ (root_world + direction * (total_length * index / (point_count - 1)))
        for index in range(point_count)
    ]

    _set_spline_positions(spline, positions)
    for point in _spline_points(spline):
        _set_point_tilt(point, 0.0)

    return True


def _reset_spline_path(context, curve_obj, spline):
    endpoint_data = _path_endpoint_data(context, curve_obj, spline)
    if endpoint_data is None:
        return False

    root_world, tip_world, _total_length, _path_points = endpoint_data
    return _reset_spline_path_to_direction(context, curve_obj, spline, tip_world - root_world)


def _flatten_spline_to_x_center(context, curve_obj, spline):
    endpoint_data = _path_endpoint_data(context, curve_obj, spline)
    if endpoint_data is None:
        return False

    _root_world, _tip_world, _total_length, path_points = endpoint_data
    center_x = (min(point.x for point in path_points) + max(point.x for point in path_points)) * 0.5
    matrix_world = curve_obj.matrix_world
    matrix_inverted = matrix_world.inverted()

    for point in _spline_points(spline):
        world_co = matrix_world @ _point_local_co(point, spline)
        world_co.x = center_x
        _set_point_local_co(point, spline, matrix_inverted @ world_co)

        if spline.type == "BEZIER":
            handle_left = matrix_world @ point.handle_left
            handle_right = matrix_world @ point.handle_right
            handle_left.x = center_x
            handle_right.x = center_x
            point.handle_left = matrix_inverted @ handle_left
            point.handle_right = matrix_inverted @ handle_right

        _set_point_tilt(point, 0.0)

    return True


def _reverse_spline_direction(spline):
    points = _spline_points(spline)

    if spline.type == "BEZIER":
        states = [
            (
                point.co.copy(),
                point.handle_left.copy(),
                point.handle_right.copy(),
                _point_radius(point),
                _point_tilt(point),
                getattr(point, "weight_softbody", 0.0),
            )
            for point in points
        ]

        for point, state in zip(points, reversed(states)):
            co, handle_left, handle_right, radius, tilt, weight_softbody = state
            point.handle_left_type = "FREE"
            point.handle_right_type = "FREE"
            point.co = co
            point.handle_left = handle_right
            point.handle_right = handle_left
            _set_point_radius(point, radius)
            _set_point_tilt(point, tilt)
            if hasattr(point, "weight_softbody"):
                point.weight_softbody = weight_softbody
        return

    states = [
        (
            point.co.copy(),
            _point_radius(point),
            _point_tilt(point),
            getattr(point, "weight_softbody", 0.0),
        )
        for point in points
    ]

    for point, state in zip(points, reversed(states)):
        co, radius, tilt, weight_softbody = state
        point.co = co
        _set_point_radius(point, radius)
        _set_point_tilt(point, tilt)
        if hasattr(point, "weight_softbody"):
            point.weight_softbody = weight_softbody


def _smooth_spline_positions(spline, factor, steps):
    points = _spline_points(spline)
    positions = [_point_local_co(point, spline) for point in points]
    _set_spline_positions(spline, _smooth_values(positions, factor, steps))


def _smooth_selected_spline_positions(spline, factor, steps):
    points = list(_spline_points(spline))
    positions = [_point_local_co(point, spline) for point in points]
    changed_indices = set()

    for run in _selected_index_runs(spline):
        if len(run) < 3:
            continue

        run_positions = {index: positions[index] for index in run}
        for _ in range(steps):
            next_positions = dict(run_positions)
            for offset, index in enumerate(run[1:-1], start=1):
                previous_index = run[offset - 1]
                next_index = run[offset + 1]
                target = (run_positions[previous_index] + run_positions[next_index]) * 0.5
                next_positions[index] = run_positions[index] + (target - run_positions[index]) * factor
            run_positions = next_positions

        for index in run[1:-1]:
            _set_point_local_co(points[index], spline, run_positions[index])
            changed_indices.add(index)

        if spline.type == "BEZIER":
            _reset_bezier_handles_for_indices(spline, set(run))

    return len(changed_indices)


def _smooth_spline_radius(spline, factor, steps):
    points = _spline_points(spline)
    values = [_point_radius(point) for point in points]
    for point, value in zip(points, _smooth_values(values, factor, steps)):
        _set_point_radius(point, value)


def _smooth_spline_tilt(spline, factor, steps):
    points = _spline_points(spline)
    values = [_point_tilt(point) for point in points]
    for point, value in zip(points, _smooth_values(values, factor, steps)):
        _set_point_tilt(point, value)


def _reset_spline_radius(spline):
    for point in _spline_points(spline):
        _set_point_radius(point, 1.0)


def _reset_spline_tilt(spline):
    for point in _spline_points(spline):
        _set_point_tilt(point, 0.0)


def _path_tangents(points):
    tangents = []
    fallback = Vector((0.0, 1.0, 0.0))

    for index in range(len(points)):
        if index == 0:
            tangent = points[1] - points[0]
        elif index == len(points) - 1:
            tangent = points[-1] - points[-2]
        else:
            tangent = points[index + 1] - points[index - 1]

        if tangent.length <= MIN_BONE_LENGTH:
            tangent = tangents[-1].copy() if tangents else fallback.copy()
        else:
            tangent.normalize()

        tangents.append(tangent)

    return tangents


def _project_normal(preferred_normal, tangent):
    normal = preferred_normal - tangent * preferred_normal.dot(tangent)
    if normal.length > MIN_BONE_LENGTH:
        normal.normalize()
        return normal

    for fallback in (Vector((0.0, 0.0, 1.0)), Vector((0.0, 1.0, 0.0)), Vector((1.0, 0.0, 0.0))):
        normal = fallback - tangent * fallback.dot(tangent)
        if normal.length > MIN_BONE_LENGTH:
            normal.normalize()
            return normal

    return tangent.orthogonal().normalized()


def _minimum_twist_normals(tangents):
    normals = [_project_normal(Vector((0.0, 0.0, 1.0)), tangents[0])]

    for index in range(1, len(tangents)):
        previous_tangent = tangents[index - 1]
        tangent = tangents[index]
        normal = normals[-1]
        axis = previous_tangent.cross(tangent)

        if axis.length > MIN_BONE_LENGTH:
            axis.normalize()
            normal = Quaternion(axis, previous_tangent.angle(tangent, 0.0)) @ normal

        normals.append(_project_normal(normal, tangent))

    return normals


def _signed_angle_around_axis(source, target, axis):
    source = _project_normal(source, axis)
    target = _project_normal(target, axis)
    angle = source.angle(target, 0.0)
    if axis.dot(source.cross(target)) < 0.0:
        return -angle
    return angle


def _base_normals_for_twist_mode(curve_obj, tangents):
    twist_mode = getattr(curve_obj.data, "twist_mode", "MINIMUM")
    if twist_mode == "Z_UP":
        return [_project_normal(Vector((0.0, 0.0, 1.0)), tangent) for tangent in tangents]
    return _minimum_twist_normals(tangents)


def _visual_normal_from_tilt(base_normal, tangent, tilt):
    axis = tangent.copy()
    if axis.length <= MIN_BONE_LENGTH:
        axis = Vector((0.0, 1.0, 0.0))
    else:
        axis.normalize()

    normal = Quaternion(axis, tilt) @ base_normal
    return _project_normal(normal, axis)


def _twist_lock_data(curve_obj):
    raw_data = curve_obj.get(CTK_TWIST_LOCKS_KEY, "[]")
    try:
        data = json.loads(raw_data)
    except (TypeError, ValueError):
        data = []

    if not isinstance(data, list):
        return []
    return data


def _twist_lock_state_from_curve(curve_obj):
    state = []

    for spline in _editable_splines(curve_obj):
        points = list(_spline_points(spline))
        positions = [_point_world_co(curve_obj, spline, point) for point in points]
        if len(positions) < 2 or not _has_valid_segment(positions):
            continue

        tangents = _path_tangents(positions)
        base_normals = _base_normals_for_twist_mode(curve_obj, tangents)
        tilts = [_point_tilt(point) for point in points]
        visual_normals = [
            _visual_normal_from_tilt(base_normal, tangent, tilt)
            for base_normal, tangent, tilt in zip(base_normals, tangents, tilts)
        ]
        state.append(
            {
                "points": [list(position) for position in positions],
                "tilts": tilts,
                "normals": [list(normal) for normal in visual_normals],
            }
        )

    return state


def _store_twist_lock_state(curve_obj, enabled):
    curve_obj["ctk_lock_twist"] = enabled
    if enabled:
        curve_obj[CTK_TWIST_LOCKS_KEY] = json.dumps(_twist_lock_state_from_curve(curve_obj))


def _points_changed(current_positions, stored_positions):
    return any((current - Vector(stored)).length > 1.0e-5 for current, stored in zip(current_positions, stored_positions))


def _tilts_changed(current_tilts, stored_tilts):
    return any(abs(current - float(stored)) > 1.0e-5 for current, stored in zip(current_tilts, stored_tilts))


def _apply_twist_lock_to_curve(curve_obj):
    if curve_obj.type != "CURVE":
        return False
    if not _custom_bool(curve_obj, "ctk_lock_twist", "hmt_lock_twist"):
        return False

    data = _twist_lock_data(curve_obj)
    splines = _editable_splines(curve_obj)
    if not splines:
        return False
    if len(data) != len(splines):
        curve_obj[CTK_TWIST_LOCKS_KEY] = json.dumps(_twist_lock_state_from_curve(curve_obj))
        return False

    changed = False
    refresh_state = False

    for spline_index, spline in enumerate(splines):
        entry = data[spline_index]
        if not isinstance(entry, dict):
            refresh_state = True
            continue

        points = list(_spline_points(spline))
        current_positions = [_point_world_co(curve_obj, spline, point) for point in points]
        current_tilts = [_point_tilt(point) for point in points]
        stored_positions = entry.get("points", [])
        stored_tilts = entry.get("tilts", [])
        stored_normals = entry.get("normals", [])

        if not (
            len(current_positions) == len(stored_positions)
            and len(current_tilts) == len(stored_tilts)
            and len(current_positions) == len(stored_normals)
        ):
            refresh_state = True
            continue

        if _points_changed(current_positions, stored_positions):
            tangents = _path_tangents(current_positions)
            base_normals = _base_normals_for_twist_mode(curve_obj, tangents)

            for point, tangent, base_normal, stored_normal in zip(points, tangents, base_normals, stored_normals):
                target_normal = _project_normal(Vector(stored_normal), tangent)
                target_tilt = _signed_angle_around_axis(base_normal, target_normal, tangent)
                if abs(_point_tilt(point) - target_tilt) > 1.0e-5:
                    _set_point_tilt(point, target_tilt)
                    changed = True

            refresh_state = True
        elif _tilts_changed(current_tilts, stored_tilts):
            refresh_state = True

    if refresh_state:
        curve_obj[CTK_TWIST_LOCKS_KEY] = json.dumps(_twist_lock_state_from_curve(curve_obj))

    return changed


def _set_curve_fill_caps(curve_obj, enabled):
    if not hasattr(curve_obj.data, "use_fill_caps"):
        return False

    curve_obj.data.use_fill_caps = enabled
    return True


def _collection_objects_recursive(collection):
    if collection is None:
        return []

    objects = []
    seen_collections = set()
    seen_objects = set()

    def visit(current_collection):
        collection_key = current_collection.as_pointer()
        if collection_key in seen_collections:
            return

        seen_collections.add(collection_key)
        for obj in current_collection.objects:
            object_key = obj.as_pointer()
            if object_key not in seen_objects:
                objects.append(obj)
                seen_objects.add(object_key)

        for child_collection in current_collection.children:
            visit(child_collection)

    visit(collection)
    return objects


def _resolution_batch_targets_from_collections(collections):
    seen_curve_objects = set()
    curve_objects = []

    for collection in collections:
        for obj in _collection_objects_recursive(collection):
            if obj.type != "CURVE":
                continue

            object_key = obj.as_pointer()
            if object_key in seen_curve_objects:
                continue

            curve_objects.append(obj)
            seen_curve_objects.add(object_key)

    bevel_reference_keys = {
        obj.data.bevel_object.as_pointer()
        for obj in curve_objects
        if getattr(obj.data, "bevel_object", None) is not None
        and obj.data.bevel_object.type == "CURVE"
    }
    path_curves = [
        obj
        for obj in curve_objects
        if obj.as_pointer() not in bevel_reference_keys
    ]
    bevel_references = []
    seen_references = set()

    for obj in path_curves:
        bevel_obj = getattr(obj.data, "bevel_object", None)
        if bevel_obj is None or bevel_obj.type != "CURVE":
            continue

        reference_key = bevel_obj.as_pointer()
        if reference_key in seen_references:
            continue

        bevel_references.append(bevel_obj)
        seen_references.add(reference_key)

    path_curves.sort(key=lambda obj: obj.name)
    bevel_references.sort(key=lambda obj: obj.name)
    return path_curves, bevel_references


def _resolution_batch_targets(collection):
    if collection is None:
        return [], []

    return _resolution_batch_targets_from_collections([collection])


def _resolution_batch_collections(settings):
    collections = []
    seen_collections = set()

    for item in settings.resolution_collections:
        collection = item.collection
        if collection is None:
            continue

        collection_key = collection.as_pointer()
        if collection_key in seen_collections:
            continue

        collections.append(collection)
        seen_collections.add(collection_key)

    if not collections and settings.resolution_collection is not None:
        collections.append(settings.resolution_collection)

    return collections


def _resolution_batch_targets_from_settings(settings):
    return _resolution_batch_targets_from_collections(_resolution_batch_collections(settings))


def _set_curve_data_resolution(curve_obj, value):
    resolution = max(0, min(64, int(value)))
    if hasattr(curve_obj.data, "resolution_u"):
        curve_obj.data.resolution_u = resolution
    if hasattr(curve_obj.data, "render_resolution_u"):
        curve_obj.data.render_resolution_u = resolution


def _apply_resolution_batch(settings, target):
    collections = _resolution_batch_collections(settings)
    if not collections:
        return 0

    path_curves, bevel_references = _resolution_batch_targets_from_collections(collections)
    target_objects = path_curves if target == "PATH" else bevel_references
    value = settings.path_resolution if target == "PATH" else settings.bevel_reference_resolution

    for obj in target_objects:
        _set_curve_data_resolution(obj, value)

    return len(target_objects)


def _update_path_resolution(settings, _context):
    global _RESOLUTION_BATCH_UPDATE_RUNNING

    if _RESOLUTION_BATCH_UPDATE_RUNNING:
        return

    _RESOLUTION_BATCH_UPDATE_RUNNING = True
    try:
        _apply_resolution_batch(settings, "PATH")
    finally:
        _RESOLUTION_BATCH_UPDATE_RUNNING = False


def _update_bevel_reference_resolution(settings, _context):
    global _RESOLUTION_BATCH_UPDATE_RUNNING

    if _RESOLUTION_BATCH_UPDATE_RUNNING:
        return

    _RESOLUTION_BATCH_UPDATE_RUNNING = True
    try:
        _apply_resolution_batch(settings, "BEVEL")
    finally:
        _RESOLUTION_BATCH_UPDATE_RUNNING = False


def _point_world_co(curve_obj, spline, point):
    return curve_obj.matrix_world @ _point_local_co(point, spline)


def _move_point_to_world(curve_obj, spline, point, world_position):
    old_local = _point_local_co(point, spline)
    new_local = curve_obj.matrix_world.inverted() @ Vector(world_position)
    delta = new_local - old_local
    _set_point_local_co(point, spline, new_local)

    if spline.type == "BEZIER":
        point.handle_left += delta
        point.handle_right += delta


def _endpoint_lock_data(curve_obj):
    raw_data = curve_obj.get(CTK_ENDPOINT_LOCKS_KEY, curve_obj.get(LEGACY_ENDPOINT_LOCKS_KEY, "{}"))
    try:
        data = json.loads(raw_data)
    except (TypeError, ValueError):
        data = {}

    if not isinstance(data, dict):
        data = {}

    data.setdefault("root", [])
    data.setdefault("tip", [])
    return data


def _custom_bool(curve_obj, key, legacy_key):
    return bool(curve_obj.get(key, curve_obj.get(legacy_key, False)))


def _store_endpoint_lock_positions(curve_obj, mode, enabled):
    curve_obj[f"ctk_lock_{mode.lower()}"] = enabled
    data = _endpoint_lock_data(curve_obj)
    splines = _editable_splines(curve_obj)

    if enabled:
        endpoint_positions = []
        for spline in splines:
            points = _spline_points(spline)
            point = points[0] if mode == "ROOT" else points[-1]
            endpoint_positions.append(list(_point_world_co(curve_obj, spline, point)))
        data[mode.lower()] = endpoint_positions

    curve_obj[CTK_ENDPOINT_LOCKS_KEY] = json.dumps(data)


def _apply_endpoint_locks_to_curve(curve_obj):
    if curve_obj.type != "CURVE":
        return False
    if _has_closed_spline(curve_obj):
        return False

    lock_root = _custom_bool(curve_obj, "ctk_lock_root", "hmt_lock_root")
    lock_tip = _custom_bool(curve_obj, "ctk_lock_tip", "hmt_lock_tip")
    if not lock_root and not lock_tip:
        return False

    data = _endpoint_lock_data(curve_obj)
    splines = _editable_splines(curve_obj)

    for index, spline in enumerate(splines):
        points = _spline_points(spline)
        if lock_root and index < len(data["root"]):
            _move_point_to_world(curve_obj, spline, points[0], data["root"][index])
        if lock_tip and index < len(data["tip"]):
            _move_point_to_world(curve_obj, spline, points[-1], data["tip"][index])

    return True


@persistent
def _ctk_curve_lock_handler(_scene, _depsgraph):
    global _CURVE_LOCK_HANDLER_RUNNING

    if _CURVE_LOCK_HANDLER_RUNNING:
        return

    _CURVE_LOCK_HANDLER_RUNNING = True
    try:
        for obj in bpy.data.objects:
            if obj.type == "CURVE":
                _apply_endpoint_locks_to_curve(obj)
                _apply_twist_lock_to_curve(obj)
    finally:
        _CURVE_LOCK_HANDLER_RUNNING = False


def _mirror_world_point_x(point, center_x=0.0):
    mirrored = point.copy()
    mirrored.x = center_x * 2.0 - mirrored.x
    return mirrored


def _mirror_point_to_object(source_obj, target_obj, point, spline):
    world_point = source_obj.matrix_world @ _point_local_co(point, spline)
    _set_point_local_co(point, spline, target_obj.matrix_world.inverted() @ _mirror_world_point_x(world_point))


def _mirror_bezier_handles_to_object(source_obj, target_obj, point):
    target_matrix_inverted = target_obj.matrix_world.inverted()
    left_world = source_obj.matrix_world @ point.handle_left
    right_world = source_obj.matrix_world @ point.handle_right
    point.handle_left = target_matrix_inverted @ _mirror_world_point_x(left_world)
    point.handle_right = target_matrix_inverted @ _mirror_world_point_x(right_world)


def _mirror_curve_data_x(source_obj, target_obj):
    for spline in target_obj.data.splines:
        if not _is_supported_spline(spline):
            continue

        for point in _spline_points(spline):
            _mirror_point_to_object(source_obj, target_obj, point, spline)
            _set_point_tilt(point, -_point_tilt(point))

            if spline.type == "BEZIER":
                _mirror_bezier_handles_to_object(source_obj, target_obj, point)


def _duplicate_mirror_curve(context, source_obj):
    mirrored_name = _unique_name(_mirror_side_name(source_obj.name), bpy.data.objects.keys())
    mirrored_data_name = _unique_name(_mirror_side_name(source_obj.data.name), bpy.data.curves.keys())

    target_data = source_obj.data.copy()
    target_data.name = mirrored_data_name

    target_obj = source_obj.copy()
    target_obj.data = target_data
    target_obj.animation_data_clear()
    target_obj.name = mirrored_name
    target_obj.matrix_world = source_obj.matrix_world.copy()
    target_obj.matrix_world.translation = _mirror_world_point_x(source_obj.matrix_world.translation)

    target_collection = _link_target_collection(context, source_obj)
    target_collection.objects.link(target_obj)

    _mirror_curve_data_x(source_obj, target_obj)
    return target_obj


def _origin_target_world(context, curve_obj, mode):
    splines = _editable_splines(curve_obj)
    if not splines:
        return None

    endpoint_data = _path_endpoint_data(context, curve_obj, splines[0])
    if endpoint_data is None:
        return None

    root_world, tip_world, total_length, path_points = endpoint_data
    if mode == "ROOT":
        return root_world
    if mode == "TIP":
        return tip_world
    return _point_at_distance(path_points, total_length * 0.5)


def _move_curve_origin_preserve_shape(curve_obj, target_world):
    old_matrix = curve_obj.matrix_world.copy()
    new_matrix = old_matrix.copy()
    new_matrix.translation = target_world
    new_matrix_inverted = new_matrix.inverted()

    for spline in curve_obj.data.splines:
        if not _is_supported_spline(spline):
            continue

        for point in _spline_points(spline):
            _transform_point_co(point, spline, old_matrix, new_matrix_inverted)
            if spline.type == "BEZIER":
                _transform_bezier_handles(point, old_matrix, new_matrix_inverted)

    curve_obj.matrix_world = new_matrix


def _collect_valid_chains(context, curve_obj):
    chains = []
    skipped_splines = 0

    for spline in curve_obj.data.splines:
        control_count = _control_point_count(spline)
        if control_count < 2:
            skipped_splines += 1
            continue

        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
        if len(path_points) < 2 or not _has_valid_segment(path_points):
            skipped_splines += 1
            continue

        joints = _resample_path_by_bone_count(path_points, control_count)
        if len(joints) < 2:
            skipped_splines += 1
            continue

        chains.append(joints)

    return chains, skipped_splines


def _clamp_node_index(value, control_count):
    return max(1, min(control_count, int(value)))


def _custom_node_range(control_count, bone_count, fill_mode, start_node, end_node):
    if control_count < 2:
        return None

    if fill_mode == "END_TO_END":
        return 1, control_count

    start_node = 1 if start_node == 0 else _clamp_node_index(start_node, control_count)
    end_node = control_count if end_node == 0 else _clamp_node_index(end_node, control_count)
    range_start = min(start_node, end_node)
    range_end = max(start_node, end_node)

    if range_end - range_start < 1:
        return None

    interval_count = range_end - range_start
    if bone_count <= 0:
        return range_start, range_end
    if fill_mode == "FROM_ROOT" and bone_count <= interval_count:
        return range_start, range_start + bone_count
    if fill_mode == "FROM_TIP" and bone_count <= interval_count:
        return range_end - bone_count, range_end

    return range_start, range_end


def _custom_bone_count(requested_bone_count, range_start, range_end):
    if requested_bone_count > 0:
        return requested_bone_count
    return max(1, range_end - range_start)


def _node_distance(total_length, control_count, node_index):
    return total_length * (node_index - 1) / (control_count - 1)


def _collect_custom_chains(context, curve_obj, bone_count, fill_mode, start_node, end_node):
    chains = []
    skipped_splines = 0

    for spline in curve_obj.data.splines:
        control_count = _control_point_count(spline)
        node_range = _custom_node_range(control_count, bone_count, fill_mode, start_node, end_node)
        if node_range is None:
            skipped_splines += 1
            continue

        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
        if len(path_points) < 2 or not _has_valid_segment(path_points):
            skipped_splines += 1
            continue

        total_length = _polyline_length(path_points)
        if total_length <= MIN_BONE_LENGTH:
            skipped_splines += 1
            continue

        range_start, range_end = node_range
        resolved_bone_count = _custom_bone_count(bone_count, range_start, range_end)
        start_distance = _node_distance(total_length, control_count, range_start)
        end_distance = _node_distance(total_length, control_count, range_end)
        joints = _resample_path_segment_by_bone_count(path_points, start_distance, end_distance, resolved_bone_count)
        if len(joints) < 2:
            skipped_splines += 1
            continue

        chains.append(joints)

    return chains, skipped_splines


def _chain_base_name(curve_name, chain_count, chain_index):
    if chain_count == 1:
        return curve_name
    return f"{curve_name}_spline.{chain_index + 1:03d}"


def _chain_bone_name(base_name, bone_index):
    if bone_index == 0:
        return base_name
    return f"{base_name}.{bone_index:03d}"


def _link_target_collection(context, source_obj):
    if source_obj.users_collection:
        return source_obj.users_collection[0]
    return context.collection


def _set_active_only(context, obj):
    for selected_obj in context.selected_objects:
        selected_obj.select_set(False)

    obj.select_set(True)
    context.view_layer.objects.active = obj


def _mode_set_object(context):
    active_obj = context.view_layer.objects.active
    if active_obj is not None and active_obj.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _create_armature_from_chains(context, curve_obj, chains):
    armature_name = _unique_name(f"{curve_obj.name}_bones", bpy.data.armatures.keys())
    object_name = _unique_name(f"{curve_obj.name}_bones", bpy.data.objects.keys())

    armature_data = bpy.data.armatures.new(armature_name)
    armature_obj = bpy.data.objects.new(object_name, armature_data)
    armature_obj.show_in_front = True

    target_collection = _link_target_collection(context, curve_obj)
    target_collection.objects.link(armature_obj)

    bone_count = 0
    skipped_segments = 0

    try:
        _set_active_only(context, armature_obj)
        bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = armature_data.edit_bones
        chain_count = len(chains)

        for chain_index, points in enumerate(chains):
            base_name = _chain_base_name(curve_obj.name, chain_count, chain_index)
            previous_bone = None
            chain_bone_index = 0

            for point_index in range(len(points) - 1):
                head = points[point_index]
                tail = points[point_index + 1]

                if (tail - head).length <= MIN_BONE_LENGTH:
                    skipped_segments += 1
                    continue

                bone = edit_bones.new(_chain_bone_name(base_name, chain_bone_index))
                bone.head = head
                bone.tail = tail

                if previous_bone is not None:
                    bone.parent = previous_bone
                    if (bone.head - previous_bone.tail).length <= MIN_BONE_LENGTH:
                        bone.head = previous_bone.tail
                        bone.use_connect = True

                previous_bone = bone
                chain_bone_index += 1
                bone_count += 1

        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        if context.view_layer.objects.active == armature_obj and armature_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.data.objects.remove(armature_obj, do_unlink=True)
        bpy.data.armatures.remove(armature_data, do_unlink=True)
        raise

    if bone_count == 0:
        bpy.data.objects.remove(armature_obj, do_unlink=True)
        bpy.data.armatures.remove(armature_data, do_unlink=True)
        raise RuntimeError("No valid bones could be created.")

    return armature_obj, bone_count, skipped_segments


def _selected_bone_names(armature_obj):
    if armature_obj.mode == "EDIT":
        return [bone.name for bone in armature_obj.data.edit_bones if bone.select]
    if armature_obj.mode == "POSE":
        return [pose_bone.name for pose_bone in armature_obj.pose.bones if pose_bone.bone.select]
    return [bone.name for bone in armature_obj.data.bones if bone.select]


def _selected_edit_bone_chains(edit_bones, selected_names):
    selected_set = set(selected_names)
    selected_children = {name: [] for name in selected_set}

    for name in selected_set:
        bone = edit_bones.get(name)
        if bone is None:
            continue
        if bone.parent is not None and bone.parent.name in selected_set:
            selected_children[bone.parent.name].append(name)

    roots = [
        name
        for name in selected_set
        if edit_bones[name].parent is None or edit_bones[name].parent.name not in selected_set
    ]
    if not roots:
        return None

    chains = []
    visited = set()
    for root in sorted(roots):
        chain = []
        current = root
        while current is not None:
            if current in visited:
                return None

            chain.append(current)
            visited.add(current)
            children = sorted(selected_children[current])
            if len(children) > 1:
                return None
            current = children[0] if children else None

        chains.append(chain)

    if visited != selected_set:
        return None

    return chains


def _invert_edit_bone_chains(edit_bones, chains):
    inverted_count = 0

    for chain in chains:
        for name in chain:
            edit_bones[name].use_connect = False

        for name in chain:
            edit_bones[name].parent = None

        for name in chain:
            bone = edit_bones[name]
            head = bone.head.copy()
            tail = bone.tail.copy()
            bone.head = tail
            bone.tail = head
            inverted_count += 1

        for index, name in enumerate(chain):
            bone = edit_bones[name]
            if index == len(chain) - 1:
                bone.parent = None
                bone.use_connect = False
                continue

            parent = edit_bones[chain[index + 1]]
            bone.parent = parent
            if (bone.head - parent.tail).length <= MIN_BONE_LENGTH:
                bone.head = parent.tail
                bone.use_connect = True

    return inverted_count


class CTK_PG_resolution_collection_item(bpy.types.PropertyGroup):
    collection: PointerProperty(
        name="Collection",
        description="Collection included in Resolution Batch",
        type=bpy.types.Collection,
    )


class CTK_PG_settings(bpy.types.PropertyGroup):
    show_curve_controls: BoolProperty(name="Curve Controls", default=True)
    show_resolution_batch: BoolProperty(name="Resolution Batch", default=True)
    show_smooth_reset: BoolProperty(name="Smooth / Reset", default=True)
    show_locks: BoolProperty(name="Locks", default=True)
    show_mirror: BoolProperty(name="Mirror", default=True)
    show_caps: BoolProperty(name="Caps", default=True)
    show_rigging: BoolProperty(name="Rigging", default=True)
    show_rig_from_points: BoolProperty(name="From Control Points", default=True)
    show_rig_custom_count: BoolProperty(name="Custom Count", default=True)
    show_rig_armature_tools: BoolProperty(name="Armature Tools", default=True)

    smooth_factor: FloatProperty(
        name="Factor",
        description="Strength for smoothing operations",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    smooth_steps: IntProperty(
        name="Steps",
        description="Number of smoothing passes",
        default=1,
        min=1,
        max=20,
    )

    resolution_collection: PointerProperty(
        name="Add Collection",
        description="Collection to add to Resolution Batch",
        type=bpy.types.Collection,
    )

    resolution_collections: CollectionProperty(
        name="Collections",
        description="Collections included in Resolution Batch",
        type=CTK_PG_resolution_collection_item,
    )

    path_resolution: IntProperty(
        name="Path Resolution",
        description="Uniform viewport and render resolution for path curves in the collection",
        default=2,
        min=0,
        max=64,
        update=_update_path_resolution,
    )

    bevel_reference_resolution: IntProperty(
        name="Bevel Reference Resolution",
        description="Uniform viewport and render resolution for bevel reference curves used by the path collection",
        default=2,
        min=0,
        max=64,
        update=_update_bevel_reference_resolution,
    )

    rig_bone_count: IntProperty(
        name="Bone Count",
        description="Number of bones to generate. 0 follows the target node count",
        default=0,
        min=0,
        max=256,
    )

    rig_fill_mode: EnumProperty(
        name="Fill Mode",
        description="How the custom rigging tool chooses the curve segment",
        items=(
            ("END_TO_END", "End To End", "Generate bones from curve root to tip"),
            ("FROM_ROOT", "From Root", "Generate bones from the root side of the selected node range"),
            ("FROM_TIP", "From Tip", "Generate bones from the tip side of the selected node range"),
        ),
        default="END_TO_END",
    )

    rig_start_node: IntProperty(
        name="Start Node",
        description="1-based start node. 0 uses the first node",
        default=0,
        min=0,
        max=10000,
    )

    rig_end_node: IntProperty(
        name="End Node",
        description="1-based end node. 0 uses the last node",
        default=0,
        min=0,
        max=10000,
    )


class CTK_OT_generate_bones_from_active_curve(bpy.types.Operator):
    bl_idname = "curve_toolkit.generate_bones_from_active_curve"
    bl_label = "Generate Bones From Active Curve"
    bl_description = "Generate a new armature from the active curve path"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            cls.poll_message_set("No active object.")
            return False
        return True

    def execute(self, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}

        try:
            _mode_set_object(context)
            chains, skipped_splines = _collect_valid_chains(context, curve_obj)
            if not chains:
                self.report({"ERROR"}, "Active curve has no spline with at least 2 usable points.")
                return {"CANCELLED"}

            armature_obj, bone_count, skipped_segments = _create_armature_from_chains(context, curve_obj, chains)
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to create bones: {exc}")
            return {"CANCELLED"}

        message = f"Created {bone_count} bones in {armature_obj.name}."
        if skipped_splines or skipped_segments:
            message += f" Skipped {skipped_splines} splines and {skipped_segments} segments."
        self.report({"INFO"}, message)
        return {"FINISHED"}


class CTK_OT_generate_custom_bones_from_active_curve(bpy.types.Operator):
    bl_idname = "curve_toolkit.generate_custom_bones_from_active_curve"
    bl_label = "Generate Custom Bones"
    bl_description = "Generate a new armature from the active curve with a custom bone count"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            cls.poll_message_set("Active object must be a Curve.")
            return False
        return True

    def execute(self, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        try:
            _mode_set_object(context)
            chains, skipped_splines = _collect_custom_chains(
                context,
                curve_obj,
                settings.rig_bone_count,
                settings.rig_fill_mode,
                settings.rig_start_node,
                settings.rig_end_node,
            )
            if not chains:
                self.report({"ERROR"}, "Active curve has no valid spline segment for custom bones.")
                return {"CANCELLED"}

            armature_obj, bone_count, skipped_segments = _create_armature_from_chains(context, curve_obj, chains)
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to create custom bones: {exc}")
            return {"CANCELLED"}

        message = f"Created {bone_count} custom bones in {armature_obj.name}."
        if skipped_splines or skipped_segments:
            message += f" Skipped {skipped_splines} splines and {skipped_segments} segments."
        self.report({"INFO"}, message)
        return {"FINISHED"}


class CTK_OT_invert_selected_bones(bpy.types.Operator):
    bl_idname = "curve_toolkit.invert_selected_bones"
    bl_label = "Invert Selected Bones"
    bl_description = "Reverse selected armature bone directions and connected parent order"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        armature_obj = _active_armature(context)
        if armature_obj is None:
            cls.poll_message_set("Active object must be an Armature.")
            return False
        return True

    def execute(self, context):
        armature_obj = _active_armature(context)
        if armature_obj is None:
            self.report({"ERROR"}, "Active object must be an Armature.")
            return {"CANCELLED"}

        previous_mode = armature_obj.mode
        selected_names = _selected_bone_names(armature_obj)
        if not selected_names:
            self.report({"ERROR"}, "Select at least one bone to invert.")
            return {"CANCELLED"}

        try:
            context.view_layer.objects.active = armature_obj
            armature_obj.select_set(True)
            if armature_obj.mode != "EDIT":
                bpy.ops.object.mode_set(mode="EDIT")

            edit_bones = armature_obj.data.edit_bones
            selected_names = [name for name in selected_names if edit_bones.get(name) is not None]
            if not selected_names:
                self.report({"ERROR"}, "Selected bones are no longer available in Edit Mode.")
                return {"CANCELLED"}

            chains = _selected_edit_bone_chains(edit_bones, selected_names)
            if chains is None:
                self.report({"ERROR"}, "Selected bones must be linear connected chains.")
                return {"CANCELLED"}

            inverted_count = _invert_edit_bone_chains(edit_bones, chains)
        finally:
            if previous_mode != armature_obj.mode:
                if previous_mode in {"OBJECT", "EDIT", "POSE"}:
                    bpy.ops.object.mode_set(mode=previous_mode)
                else:
                    bpy.ops.object.mode_set(mode="OBJECT")

        self.report({"INFO"}, f"Inverted {inverted_count} selected bones.")
        return {"FINISHED"}


class CTK_OT_reset_path(bpy.types.Operator):
    bl_idname = "curve_toolkit.reset_path"
    bl_label = "Reset Path"
    bl_description = "Straighten the active open curve while preserving root and current path length"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        changed_count = 0
        for spline in splines:
            if _reset_spline_path(context, curve_obj, spline):
                changed_count += 1

        if changed_count == 0:
            self.report({"ERROR"}, "No valid open spline path could be reset.")
            return {"CANCELLED"}

        context.view_layer.update()
        self.report({"INFO"}, f"Reset {changed_count} curve splines.")
        return {"FINISHED"}


class CTK_OT_reset_path_x_axis(bpy.types.Operator):
    bl_idname = "curve_toolkit.reset_path_x_axis"
    bl_label = "X Axis"
    bl_description = "Move all points to the curve X center while preserving Y and Z"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        changed_count = 0
        for spline in splines:
            if _flatten_spline_to_x_center(context, curve_obj, spline):
                changed_count += 1

        if changed_count == 0:
            self.report({"ERROR"}, "No valid open spline path could be centered on X axis.")
            return {"CANCELLED"}

        context.view_layer.update()
        self.report({"INFO"}, f"Centered X for {changed_count} curve splines.")
        return {"FINISHED"}


class CTK_OT_switch_direction(bpy.types.Operator):
    bl_idname = "curve_toolkit.switch_direction"
    bl_label = "Switch Direction"
    bl_description = "Reverse active open curve spline direction"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        for spline in splines:
            _reverse_spline_direction(spline)

        context.view_layer.update()
        self.report({"INFO"}, f"Switched direction for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_set_origin(bpy.types.Operator):
    bl_idname = "curve_toolkit.set_origin"
    bl_label = "Set Origin"
    bl_description = "Move curve origin without moving the visible curve"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        name="Origin",
        items=(
            ("ROOT", "Root", "Set origin to curve root"),
            ("TIP", "Tip", "Set origin to curve tip"),
            ("CENTER", "Center", "Set origin to midpoint by path length"),
        ),
        default="ROOT",
    )

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, _splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        target_world = _origin_target_world(context, curve_obj, self.mode)
        if target_world is None:
            self.report({"ERROR"}, "Could not find a valid origin target on the active curve.")
            return {"CANCELLED"}

        _move_curve_origin_preserve_shape(curve_obj, target_world)
        context.view_layer.update()
        self.report({"INFO"}, f"Set origin to {self.mode.lower()}.")
        return {"FINISHED"}


class CTK_OT_snap_cursor(bpy.types.Operator):
    bl_idname = "curve_toolkit.snap_cursor"
    bl_label = "Snap 3D Cursor"
    bl_description = "Snap the 3D cursor to the active curve root, tip, or center"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        name="Target",
        items=(
            ("ROOT", "Root", "Snap cursor to curve root"),
            ("TIP", "Tip", "Snap cursor to curve tip"),
            ("CENTER", "Center", "Snap cursor to midpoint by path length"),
        ),
        default="ROOT",
    )

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, _splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        target_world = _origin_target_world(context, curve_obj, self.mode)
        if target_world is None:
            self.report({"ERROR"}, "Could not find a valid cursor target on the active curve.")
            return {"CANCELLED"}

        context.scene.cursor.location = target_world
        self.report({"INFO"}, f"Snapped 3D cursor to {self.mode.lower()}.")
        return {"FINISHED"}


class CTK_OT_smooth_scale(bpy.types.Operator):
    bl_idname = "curve_toolkit.smooth_scale"
    bl_label = "Smooth Scale"
    bl_description = "Smooth curve point radius values"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        for spline in splines:
            _smooth_spline_radius(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        self.report({"INFO"}, f"Smoothed scale for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_smooth_curve(bpy.types.Operator):
    bl_idname = "curve_toolkit.smooth_curve"
    bl_label = "Smooth Curve"
    bl_description = "Smooth curve control point positions"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        selected_mode = _has_selected_points(splines)
        changed_count = 0

        for spline in splines:
            if selected_mode:
                changed_count += _smooth_selected_spline_positions(
                    spline,
                    settings.smooth_factor,
                    settings.smooth_steps,
                )
            else:
                _smooth_spline_positions(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        if selected_mode:
            if changed_count == 0:
                self.report({"ERROR"}, "Select at least 3 contiguous curve points to smooth.")
                return {"CANCELLED"}

            self.report({"INFO"}, f"Smoothed {changed_count} selected curve points.")
            return {"FINISHED"}

        self.report({"INFO"}, f"Smoothed curve for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_smooth_twist(bpy.types.Operator):
    bl_idname = "curve_toolkit.smooth_twist"
    bl_label = "Smooth Twist"
    bl_description = "Smooth curve point tilt values"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        for spline in splines:
            _smooth_spline_tilt(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        self.report({"INFO"}, f"Smoothed twist for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_reset_scale(bpy.types.Operator):
    bl_idname = "curve_toolkit.reset_scale"
    bl_label = "Reset Scale"
    bl_description = "Reset curve point radius values to 1"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        for spline in splines:
            _reset_spline_radius(spline)

        context.view_layer.update()
        self.report({"INFO"}, f"Reset scale for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_reset_twist(bpy.types.Operator):
    bl_idname = "curve_toolkit.reset_twist"
    bl_label = "Reset Twist"
    bl_description = "Reset curve point tilt values to 0"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        for spline in splines:
            _reset_spline_tilt(spline)

        context.view_layer.update()
        self.report({"INFO"}, f"Reset twist for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_flip_twist(bpy.types.Operator):
    bl_idname = "curve_toolkit.flip_twist"
    bl_label = "Flip Twist"
    bl_description = "Add 180 degrees to curve point twist values"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        for spline in splines:
            for point in _spline_points(spline):
                _set_point_tilt(point, _point_tilt(point) + pi)

        context.view_layer.update()
        self.report({"INFO"}, f"Flipped twist for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_lock_twist(bpy.types.Operator):
    bl_idname = "curve_toolkit.lock_twist"
    bl_label = "Lock Twist"
    bl_description = "Store the current twist state and preserve it when curve points move"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        _store_twist_lock_state(curve_obj, True)
        context.view_layer.update()
        self.report({"INFO"}, f"Locked twist for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_unlock_twist(bpy.types.Operator):
    bl_idname = "curve_toolkit.unlock_twist"
    bl_label = "Unlock Twist"
    bl_description = "Release the toolkit twist lock without changing the current curve shape"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        _store_twist_lock_state(curve_obj, False)
        context.view_layer.update()
        self.report({"INFO"}, f"Unlocked twist for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_set_endpoint_lock(bpy.types.Operator):
    bl_idname = "curve_toolkit.set_endpoint_lock"
    bl_label = "Set Endpoint Lock"
    bl_description = "Lock or unlock a curve endpoint at its current world position"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Endpoint",
        items=(
            ("ROOT", "Root", "Use the first control point in each spline"),
            ("TIP", "Tip", "Use the last control point in each spline"),
        ),
        default="ROOT",
    )
    enabled: BoolProperty(name="Enabled", default=True)

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        context.view_layer.update()
        if not self.enabled:
            _apply_endpoint_locks_to_curve(curve_obj)
            context.view_layer.update()

        _store_endpoint_lock_positions(curve_obj, self.mode, self.enabled)
        if self.enabled:
            _apply_endpoint_locks_to_curve(curve_obj)
        context.view_layer.update()

        endpoint_name = "root" if self.mode == "ROOT" else "tip"
        state = "Locked" if self.enabled else "Unlocked"
        self.report({"INFO"}, f"{state} {endpoint_name} endpoint for {len(splines)} splines.")
        return {"FINISHED"}


class CTK_OT_duplicate_mirror_selected_curves(bpy.types.Operator):
    bl_idname = "curve_toolkit.duplicate_mirror_selected_curves"
    bl_label = "Duplicate Mirror"
    bl_description = "Duplicate selected curves and mirror them across global X center"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "CURVE" for obj in context.selected_objects)

    def execute(self, context):
        source_curves = [obj for obj in context.selected_objects if obj.type == "CURVE"]
        if not source_curves:
            self.report({"ERROR"}, "Select at least one Curve object.")
            return {"CANCELLED"}

        context.view_layer.update()
        mirrored_objects = []
        for source_obj in source_curves:
            mirrored_objects.append(_duplicate_mirror_curve(context, source_obj))

        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in mirrored_objects:
            obj.select_set(True)

        context.view_layer.objects.active = mirrored_objects[-1]
        context.view_layer.update()
        self.report({"INFO"}, f"Created {len(mirrored_objects)} mirrored curve duplicates.")
        return {"FINISHED"}


class CTK_OT_set_fill_caps(bpy.types.Operator):
    bl_idname = "curve_toolkit.set_fill_caps"
    bl_label = "Close Ends"
    bl_description = "Close curve geometry caps without changing point radius scale"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ("ROOT", "Root", "Close caps for the root side"),
            ("TIP", "Tip", "Close caps for the tip side"),
            ("BOTH", "Ends", "Close caps for both ends"),
            ("OPEN", "Open", "Open curve caps"),
        ),
        default="BOTH",
    )

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}

        if self.mode == "OPEN":
            curve_obj["ctk_cap_root"] = False
            curve_obj["ctk_cap_tip"] = False
        elif self.mode == "ROOT":
            curve_obj["ctk_cap_root"] = True
        elif self.mode == "TIP":
            curve_obj["ctk_cap_tip"] = True
        else:
            curve_obj["ctk_cap_root"] = True
            curve_obj["ctk_cap_tip"] = True

        enabled = bool(curve_obj.get("ctk_cap_root", False) or curve_obj.get("ctk_cap_tip", False))
        if not _set_curve_fill_caps(curve_obj, enabled):
            self.report({"ERROR"}, "Active curve does not support fill caps.")
            return {"CANCELLED"}

        context.view_layer.update()
        if self.mode == "OPEN":
            self.report({"INFO"}, "Opened curve caps.")
        else:
            self.report({"INFO"}, "Closed curve caps without changing point scale.")
        return {"FINISHED"}


class CTK_OT_add_resolution_collection(bpy.types.Operator):
    bl_idname = "curve_toolkit.add_resolution_collection"
    bl_label = "Add Collection"
    bl_description = "Add the selected collection to Resolution Batch"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.curve_toolkit
        collection = settings.resolution_collection
        if collection is None:
            self.report({"WARNING"}, "Choose a collection to add.")
            return {"CANCELLED"}

        collection_key = collection.as_pointer()
        for item in settings.resolution_collections:
            if item.collection is not None and item.collection.as_pointer() == collection_key:
                self.report({"INFO"}, f"{collection.name} is already registered.")
                return {"FINISHED"}

        item = settings.resolution_collections.add()
        item.collection = collection
        _apply_resolution_batch(settings, "PATH")
        _apply_resolution_batch(settings, "BEVEL")
        self.report({"INFO"}, f"Added {collection.name} to Resolution Batch.")
        return {"FINISHED"}


class CTK_OT_remove_resolution_collection(bpy.types.Operator):
    bl_idname = "curve_toolkit.remove_resolution_collection"
    bl_label = "Remove Collection"
    bl_description = "Remove a collection from Resolution Batch"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        settings = context.scene.curve_toolkit
        if self.index < 0 or self.index >= len(settings.resolution_collections):
            self.report({"ERROR"}, "Invalid Resolution Batch collection index.")
            return {"CANCELLED"}

        item = settings.resolution_collections[self.index]
        collection_name = item.collection.name if item.collection is not None else "Missing Collection"
        settings.resolution_collections.remove(self.index)
        self.report({"INFO"}, f"Removed {collection_name} from Resolution Batch.")
        return {"FINISHED"}


class CTK_OT_refresh_resolution_batch(bpy.types.Operator):
    bl_idname = "curve_toolkit.refresh_resolution_batch"
    bl_label = "Refresh"
    bl_description = "Refresh Resolution Batch target counts"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.curve_toolkit
        collections = _resolution_batch_collections(settings)
        if not collections:
            self.report({"WARNING"}, "Add at least one collection for Resolution Batch.")
            return {"FINISHED"}

        path_curves, bevel_references = _resolution_batch_targets_from_collections(collections)
        self.report(
            {"INFO"},
            f"Resolution Batch found {len(path_curves)} path curves and {len(bevel_references)} bevel references from {len(collections)} collections.",
        )
        return {"FINISHED"}


class CTK_PT_tools(bpy.types.Panel):
    bl_label = "Curve Toolkit"
    bl_idname = "CTK_PT_tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Curve Toolkit"

    @staticmethod
    def _draw_foldout(box, settings, prop_name, text, icon=None):
        is_open = bool(getattr(settings, prop_name))
        row = box.row(align=True)
        row.prop(settings, prop_name, text="", icon="TRIA_DOWN" if is_open else "TRIA_RIGHT", emboss=False)
        if icon is None:
            row.label(text=text)
        else:
            row.label(text=text, icon=icon)
        return is_open

    def draw(self, context):
        layout = self.layout
        settings = context.scene.curve_toolkit
        curve_obj = _active_curve(context)
        armature_obj = _active_armature(context)
        twist_locked = curve_obj is not None and _custom_bool(curve_obj, "ctk_lock_twist", "hmt_lock_twist")
        root_locked = curve_obj is not None and _custom_bool(curve_obj, "ctk_lock_root", "hmt_lock_root")
        tip_locked = curve_obj is not None and _custom_bool(curve_obj, "ctk_lock_tip", "hmt_lock_tip")
        caps_filled = curve_obj is not None and bool(getattr(curve_obj.data, "use_fill_caps", False))
        cap_root = curve_obj is not None and _custom_bool(curve_obj, "ctk_cap_root", "hmt_cap_root")
        cap_tip = curve_obj is not None and _custom_bool(curve_obj, "ctk_cap_tip", "hmt_cap_tip")
        if caps_filled and not (cap_root or cap_tip):
            cap_root = True
            cap_tip = True
        twist_unlocked = curve_obj is not None and not twist_locked
        root_unlocked = curve_obj is not None and not root_locked
        tip_unlocked = curve_obj is not None and not tip_locked
        caps_open = curve_obj is not None and not caps_filled

        curve_box = layout.box()
        if self._draw_foldout(curve_box, settings, "show_curve_controls", "Curve Controls", "CURVE_DATA"):
            row = curve_box.row(align=True)
            row.operator(CTK_OT_reset_path.bl_idname)
            row.operator(CTK_OT_reset_path_x_axis.bl_idname)
            row.operator(CTK_OT_switch_direction.bl_idname)

            curve_box.label(text="Origin To")
            row = curve_box.row(align=True)
            op = row.operator(CTK_OT_set_origin.bl_idname, text="Root")
            op.mode = "ROOT"
            op = row.operator(CTK_OT_set_origin.bl_idname, text="Tip")
            op.mode = "TIP"
            op = row.operator(CTK_OT_set_origin.bl_idname, text="Center")
            op.mode = "CENTER"

            curve_box.label(text="3D Cursor To")
            row = curve_box.row(align=True)
            op = row.operator(CTK_OT_snap_cursor.bl_idname, text="Root")
            op.mode = "ROOT"
            op = row.operator(CTK_OT_snap_cursor.bl_idname, text="Tip")
            op.mode = "TIP"
            op = row.operator(CTK_OT_snap_cursor.bl_idname, text="Center")
            op.mode = "CENTER"

        resolution_box = layout.box()
        if self._draw_foldout(resolution_box, settings, "show_resolution_batch", "Resolution Batch", "OUTLINER_COLLECTION"):
            row = resolution_box.row(align=True)
            row.prop(settings, "resolution_collection")
            row.operator(CTK_OT_add_resolution_collection.bl_idname, text="", icon="ADD")

            collections = _resolution_batch_collections(settings)
            if settings.resolution_collections:
                resolution_box.label(text=f"Collections: {len(collections)}")
                for index, item in enumerate(settings.resolution_collections):
                    row = resolution_box.row(align=True)
                    row.label(text=item.collection.name if item.collection is not None else "Missing Collection")
                    op = row.operator(CTK_OT_remove_resolution_collection.bl_idname, text="", icon="X")
                    op.index = index
            else:
                resolution_box.label(text=f"Collections: {len(collections)}")

            path_curves, bevel_references = _resolution_batch_targets_from_collections(collections)
            resolution_box.label(text=f"Paths: {len(path_curves)}  Bevel Refs: {len(bevel_references)}")
            resolution_box.prop(settings, "path_resolution")
            resolution_box.prop(settings, "bevel_reference_resolution")
            resolution_box.operator(CTK_OT_refresh_resolution_batch.bl_idname)

        smooth_box = layout.box()
        if self._draw_foldout(smooth_box, settings, "show_smooth_reset", "Smooth / Reset", "MOD_SMOOTH"):
            smooth_box.prop(settings, "smooth_factor", slider=True)
            smooth_box.prop(settings, "smooth_steps")

            row = smooth_box.row(align=True)
            row.operator(CTK_OT_smooth_scale.bl_idname)
            row.operator(CTK_OT_smooth_curve.bl_idname)
            row.operator(CTK_OT_smooth_twist.bl_idname)

            row = smooth_box.row(align=True)
            row.operator(CTK_OT_reset_scale.bl_idname)
            row.operator(CTK_OT_reset_path.bl_idname, text="Reset Curve")
            row.operator(CTK_OT_reset_twist.bl_idname)

        lock_box = layout.box()
        if self._draw_foldout(lock_box, settings, "show_locks", "Locks", "LOCKED"):
            row = lock_box.row(align=True)
            row.operator(CTK_OT_lock_twist.bl_idname, depress=twist_locked)
            row.operator(CTK_OT_unlock_twist.bl_idname, depress=twist_unlocked)
            row.operator(CTK_OT_flip_twist.bl_idname)

            row = lock_box.row(align=True)
            op = row.operator(CTK_OT_set_endpoint_lock.bl_idname, text="Lock Root", depress=root_locked)
            op.mode = "ROOT"
            op.enabled = True
            op = row.operator(CTK_OT_set_endpoint_lock.bl_idname, text="Unlock Root", depress=root_unlocked)
            op.mode = "ROOT"
            op.enabled = False

            row = lock_box.row(align=True)
            op = row.operator(CTK_OT_set_endpoint_lock.bl_idname, text="Lock Tip", depress=tip_locked)
            op.mode = "TIP"
            op.enabled = True
            op = row.operator(CTK_OT_set_endpoint_lock.bl_idname, text="Unlock Tip", depress=tip_unlocked)
            op.mode = "TIP"
            op.enabled = False

        mirror_box = layout.box()
        if self._draw_foldout(mirror_box, settings, "show_mirror", "Mirror", "MOD_MIRROR"):
            mirror_box.operator(CTK_OT_duplicate_mirror_selected_curves.bl_idname)

        caps_box = layout.box()
        if self._draw_foldout(caps_box, settings, "show_caps", "Caps", "MESH_DATA"):
            row = caps_box.row(align=True)
            op = row.operator(CTK_OT_set_fill_caps.bl_idname, text="Root", depress=cap_root and not cap_tip)
            op.mode = "ROOT"
            op = row.operator(CTK_OT_set_fill_caps.bl_idname, text="Tip", depress=cap_tip and not cap_root)
            op.mode = "TIP"
            op = row.operator(CTK_OT_set_fill_caps.bl_idname, text="Ends", depress=cap_root and cap_tip)
            op.mode = "BOTH"
            caps_box.operator(CTK_OT_set_fill_caps.bl_idname, text="Open Caps", depress=caps_open).mode = "OPEN"

        rigging_box = layout.box()
        if self._draw_foldout(rigging_box, settings, "show_rigging", "Rigging", "ARMATURE_DATA"):
            if self._draw_foldout(rigging_box, settings, "show_rig_from_points", "From Control Points"):
                control_point_column = rigging_box.column(align=True)
                control_point_column.enabled = curve_obj is not None
                control_point_column.operator(
                    CTK_OT_generate_bones_from_active_curve.bl_idname,
                    text="Generate From Points",
                    icon="ARMATURE_DATA",
                )

            rigging_box.separator()
            if self._draw_foldout(rigging_box, settings, "show_rig_custom_count", "Custom Count"):
                rigging_box.prop(settings, "rig_bone_count")
                rigging_box.prop(settings, "rig_fill_mode")
                node_row = rigging_box.row(align=True)
                node_row.enabled = settings.rig_fill_mode != "END_TO_END"
                node_row.prop(settings, "rig_start_node")
                node_row.prop(settings, "rig_end_node")

                custom_column = rigging_box.column(align=True)
                custom_column.enabled = curve_obj is not None
                custom_column.operator(
                    CTK_OT_generate_custom_bones_from_active_curve.bl_idname,
                    text="Generate Custom Count",
                    icon="ARMATURE_DATA",
                )

            rigging_box.separator()
            if self._draw_foldout(rigging_box, settings, "show_rig_armature_tools", "Armature Tools"):
                invert_row = rigging_box.row(align=True)
                invert_row.enabled = armature_obj is not None
                invert_row.operator(CTK_OT_invert_selected_bones.bl_idname)


classes = (
    CTK_PG_resolution_collection_item,
    CTK_PG_settings,
    CTK_OT_generate_bones_from_active_curve,
    CTK_OT_generate_custom_bones_from_active_curve,
    CTK_OT_invert_selected_bones,
    CTK_OT_reset_path,
    CTK_OT_reset_path_x_axis,
    CTK_OT_switch_direction,
    CTK_OT_set_origin,
    CTK_OT_snap_cursor,
    CTK_OT_smooth_scale,
    CTK_OT_smooth_curve,
    CTK_OT_smooth_twist,
    CTK_OT_reset_scale,
    CTK_OT_reset_twist,
    CTK_OT_flip_twist,
    CTK_OT_lock_twist,
    CTK_OT_unlock_twist,
    CTK_OT_set_endpoint_lock,
    CTK_OT_duplicate_mirror_selected_curves,
    CTK_OT_set_fill_caps,
    CTK_OT_add_resolution_collection,
    CTK_OT_remove_resolution_collection,
    CTK_OT_refresh_resolution_batch,
    CTK_PT_tools,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.curve_toolkit = PointerProperty(type=CTK_PG_settings)
    if _ctk_curve_lock_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_ctk_curve_lock_handler)


def unregister():
    if _ctk_curve_lock_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_ctk_curve_lock_handler)
    del bpy.types.Scene.curve_toolkit
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
