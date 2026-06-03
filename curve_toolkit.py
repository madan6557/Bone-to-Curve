# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Curve Toolkit",
    "author": "madan6557",
    "version": (1, 8, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Curve Toolkit",
    "description": "Curve modeling tools and bone chain generation.",
    "category": "Curve",
}

import json
from math import pi

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from mathutils.bvhtree import BVHTree
from mathutils import Quaternion, Vector


MIN_BONE_LENGTH = 1.0e-6
CTK_ENDPOINT_LOCKS_KEY = "ctk_endpoint_locks"
CTK_TWIST_LOCKS_KEY = "ctk_twist_locks"
CTK_LENGTHS_KEY = "ctk_stored_lengths"
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


def _active_or_selected_armature(context):
    active = _active_armature(context)
    if active is not None:
        return active

    for obj in context.selected_objects:
        if obj.type == "ARMATURE":
            return obj

    return None


def _selected_curve_objects(context):
    return [obj for obj in context.selected_objects if obj.type == "CURVE"]


def _target_curve_objects(context):
    curves = _selected_curve_objects(context)
    active = _active_curve(context)
    if active is not None and active not in curves:
        curves.insert(0, active)
    return curves


def _target_or_scene_curve_objects(context):
    curves = _target_curve_objects(context)
    if curves:
        return curves
    return [obj for obj in context.scene.objects if obj.type == "CURVE"]


def _poll_mesh_object(_self, obj):
    return obj is not None and obj.type == "MESH"


def _poll_curve_object(_self, obj):
    return obj is not None and obj.type == "CURVE"


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


def _spline_world_positions(curve_obj, spline):
    return [_point_world_co(curve_obj, spline, point) for point in _spline_points(spline)]


def _set_spline_world_positions(curve_obj, spline, positions, reset_handles=False):
    for point, position in zip(_spline_points(spline), positions):
        _move_point_to_world(curve_obj, spline, point, position)

    if reset_handles and spline.type == "BEZIER":
        _reset_bezier_handles_to_path(spline)


def _translate_spline_world(curve_obj, spline, delta):
    for point in _spline_points(spline):
        _move_point_to_world(curve_obj, spline, point, _point_world_co(curve_obj, spline, point) + delta)


def _resample_world_path_by_point_count(path_points, point_count):
    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH or point_count < 2:
        return []

    return [
        _point_at_distance(path_points, total_length * index / (point_count - 1))
        for index in range(point_count)
    ]


def _resample_world_path_segment_by_point_count(path_points, start_distance, end_distance, point_count):
    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH or point_count < 2:
        return []

    start_distance = max(0.0, min(total_length, start_distance))
    end_distance = max(0.0, min(total_length, end_distance))
    if end_distance - start_distance <= MIN_BONE_LENGTH:
        return []

    return [
        _point_at_distance(path_points, start_distance + (end_distance - start_distance) * index / (point_count - 1))
        for index in range(point_count)
    ]


def _set_spline_length(curve_obj, spline, target_length):
    positions = _spline_world_positions(curve_obj, spline)
    current_length = _polyline_length(positions)
    if current_length <= MIN_BONE_LENGTH or target_length <= MIN_BONE_LENGTH:
        return False

    root = positions[0]
    scale = target_length / current_length
    new_positions = [root + (position - root) * scale for position in positions]
    _set_spline_world_positions(curve_obj, spline, new_positions, reset_handles=True)
    return True


def _trim_spline(curve_obj, spline, root_amount, tip_amount):
    positions = _spline_world_positions(curve_obj, spline)
    point_count = len(positions)
    total_length = _polyline_length(positions)
    if total_length <= MIN_BONE_LENGTH or point_count < 2:
        return False

    start_distance = max(0.0, root_amount)
    end_distance = total_length - max(0.0, tip_amount)
    new_positions = _resample_world_path_segment_by_point_count(positions, start_distance, end_distance, point_count)
    if not new_positions:
        return False

    _set_spline_world_positions(curve_obj, spline, new_positions, reset_handles=True)
    return True


def _stored_lengths(curve_obj):
    raw_data = curve_obj.get(CTK_LENGTHS_KEY, "[]")
    try:
        data = json.loads(raw_data)
    except (TypeError, ValueError):
        data = []
    return data if isinstance(data, list) else []


def _store_lengths(curve_obj):
    lengths = [_polyline_length(_spline_world_positions(curve_obj, spline)) for spline in _editable_splines(curve_obj)]
    curve_obj[CTK_LENGTHS_KEY] = json.dumps(lengths)
    return len(lengths)


def _surface_bvh_from_object(context, surface_obj):
    depsgraph = context.evaluated_depsgraph_get()
    eval_obj = surface_obj.evaluated_get(depsgraph)
    mesh = eval_obj.to_mesh()
    try:
        vertices = [vertex.co.copy() for vertex in mesh.vertices]
        polygons = [list(polygon.vertices) for polygon in mesh.polygons]
        bvh = BVHTree.FromPolygons(vertices, polygons)
    finally:
        eval_obj.to_mesh_clear()

    normal_matrix = surface_obj.matrix_world.to_3x3().inverted().transposed()
    return bvh, surface_obj.matrix_world.copy(), surface_obj.matrix_world.inverted(), normal_matrix


def _nearest_surface_point(surface_data, world_position):
    bvh, matrix_world, matrix_world_inverted, normal_matrix = surface_data
    local_position = matrix_world_inverted @ world_position
    result = bvh.find_nearest(local_position)
    if result is None:
        return None

    local_location, local_normal, _face_index, _distance = result
    world_location = matrix_world @ local_location
    world_normal = normal_matrix @ local_normal
    if world_normal.length <= MIN_BONE_LENGTH:
        world_normal = Vector((0.0, 0.0, 1.0))
    else:
        world_normal.normalize()

    return world_location, world_normal


def _surface_target_world(surface_data, world_position, offset):
    nearest = _nearest_surface_point(surface_data, world_position)
    if nearest is None:
        return None
    location, normal = nearest
    return location + normal * offset


def _move_spline_root_to_surface(curve_obj, spline, surface_data, offset):
    points = _spline_points(spline)
    target = _surface_target_world(surface_data, _point_world_co(curve_obj, spline, points[0]), offset)
    if target is None:
        return False

    _move_point_to_world(curve_obj, spline, points[0], target)
    return True


def _offset_spline_root_to_surface(curve_obj, spline, surface_data, offset):
    points = _spline_points(spline)
    root_world = _point_world_co(curve_obj, spline, points[0])
    target = _surface_target_world(surface_data, root_world, offset)
    if target is None:
        return False

    _translate_spline_world(curve_obj, spline, target - root_world)
    return True


def _snap_spline_to_surface(curve_obj, spline, surface_data, offset):
    changed = False
    for point in _spline_points(spline):
        world_position = _point_world_co(curve_obj, spline, point)
        target = _surface_target_world(surface_data, world_position, offset)
        if target is None:
            continue
        _move_point_to_world(curve_obj, spline, point, target)
        changed = True
    return changed


def _push_spline_from_surface(curve_obj, spline, surface_data, offset):
    changed = False
    for point in _spline_points(spline):
        world_position = _point_world_co(curve_obj, spline, point)
        nearest = _nearest_surface_point(surface_data, world_position)
        if nearest is None:
            continue

        location, normal = nearest
        distance = (world_position - location).dot(normal)
        if distance >= offset:
            continue

        _move_point_to_world(curve_obj, spline, point, location + normal * offset)
        changed = True

    return changed


def _profile_radius_value(preset, factor, root_radius, tip_radius, mid_radius):
    factor = max(0.0, min(1.0, factor))

    if preset == "FLAT":
        return root_radius
    if preset == "ROOT_THICK":
        return root_radius + (tip_radius - root_radius) * factor
    if preset == "TIP_THIN":
        return root_radius * (1.0 - factor) + tip_radius * factor
    if preset == "BOTH_THIN":
        middle = 1.0 - abs(2.0 * factor - 1.0)
        edge = tip_radius
        return edge + (mid_radius - edge) * middle
    if preset == "SHARP_TAPER":
        if factor < 0.35:
            local_factor = factor / 0.35
            return root_radius + (mid_radius - root_radius) * local_factor
        local_factor = (factor - 0.35) / 0.65
        return mid_radius + (tip_radius - mid_radius) * local_factor

    return root_radius


def _apply_radius_profile_to_indices(points, indices, preset, root_radius, tip_radius, mid_radius):
    if len(indices) == 1:
        _set_point_radius(points[indices[0]], root_radius)
        return 1

    for offset, index in enumerate(indices):
        factor = offset / (len(indices) - 1)
        _set_point_radius(points[index], _profile_radius_value(preset, factor, root_radius, tip_radius, mid_radius))
    return len(indices)


def _apply_radius_profile_to_spline(spline, preset, root_radius, tip_radius, mid_radius, selected_mode):
    points = list(_spline_points(spline))
    changed = 0

    if selected_mode:
        for run in _selected_index_runs(spline):
            changed += _apply_radius_profile_to_indices(points, run, preset, root_radius, tip_radius, mid_radius)
        return changed

    return _apply_radius_profile_to_indices(points, list(range(len(points))), preset, root_radius, tip_radius, mid_radius)


def _sample_values(values, count):
    if not values or count < 1:
        return []
    if len(values) == 1:
        return [values[0]] * count
    if count == 1:
        return [values[0]]

    sampled = []
    for index in range(count):
        position = (len(values) - 1) * index / (count - 1)
        left_index = int(position)
        right_index = min(len(values) - 1, left_index + 1)
        factor = position - left_index
        sampled.append(values[left_index] + (values[right_index] - values[left_index]) * factor)
    return sampled


def _apply_sampled_radius_values(points, indices, values):
    sampled = _sample_values(values, len(indices))
    for index, value in zip(indices, sampled):
        _set_point_radius(points[index], value)
    return len(sampled)


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


def _distance_on_path_nearest(path_points, world_position):
    if len(path_points) < 2:
        return 0.0

    target = Vector(world_position)
    walked_distance = 0.0
    best_distance = 0.0
    best_squared = None

    for index in range(len(path_points) - 1):
        start = path_points[index]
        end = path_points[index + 1]
        segment = end - start
        segment_length = segment.length

        if segment_length <= MIN_BONE_LENGTH:
            continue

        factor = max(0.0, min(1.0, (target - start).dot(segment) / segment.length_squared))
        projected = start + segment * factor
        squared_distance = (target - projected).length_squared

        if best_squared is None or squared_distance < best_squared:
            best_squared = squared_distance
            best_distance = walked_distance + segment_length * factor

        walked_distance += segment_length

    return best_distance


def _fallback_node_distance(total_length, point_count, point_index):
    if point_count < 2:
        return 0.0
    return total_length * point_index / (point_count - 1)


def _monotonic_distances(distances, total_length, minimum_step=1.0e-5):
    if not distances:
        return []

    clamped = [max(0.0, min(total_length, float(distances[0])))]
    for distance in distances[1:]:
        next_distance = max(0.0, min(total_length, float(distance)))
        if next_distance <= clamped[-1]:
            next_distance = min(total_length, clamped[-1] + minimum_step)
        clamped.append(next_distance)

    if len(clamped) > 1 and clamped[-1] - clamped[0] <= minimum_step:
        start = clamped[0]
        end = min(total_length, start + minimum_step * (len(clamped) - 1))
        if end - start <= minimum_step:
            start = max(0.0, total_length - minimum_step * (len(clamped) - 1))
            end = total_length
        clamped = [
            start + (end - start) * index / (len(clamped) - 1)
            for index in range(len(clamped))
        ]

    return clamped


def _control_distances_on_path(curve_obj, spline, indices, path_points):
    total_length = _polyline_length(path_points)
    point_count = _control_point_count(spline)
    points = list(_spline_points(spline))
    distances = []

    for index in indices:
        if index == 0:
            distances.append(0.0)
            continue
        if index == point_count - 1:
            distances.append(total_length)
            continue

        world_position = _point_world_co(curve_obj, spline, points[index])
        distances.append(_distance_on_path_nearest(path_points, world_position))

    distances = _monotonic_distances(distances, total_length)
    if len(distances) >= 2 and distances[-1] - distances[0] > MIN_BONE_LENGTH:
        return distances

    return [
        _fallback_node_distance(total_length, point_count, index)
        for index in indices
    ]


def _sample_scalar_by_distance(source_distances, values, target_distance):
    if not source_distances or not values:
        return 0.0
    if len(source_distances) == 1:
        return values[0]

    if target_distance <= source_distances[0]:
        return values[0]
    if target_distance >= source_distances[-1]:
        return values[-1]

    for index in range(len(source_distances) - 1):
        start_distance = source_distances[index]
        end_distance = source_distances[index + 1]
        if end_distance - start_distance <= MIN_BONE_LENGTH:
            continue
        if target_distance <= end_distance:
            factor = (target_distance - start_distance) / (end_distance - start_distance)
            return values[index] + (values[index + 1] - values[index]) * factor

    return values[-1]


def _path_weighted_distances(path_points, start_distance, end_distance, point_count, curvature_bias):
    if point_count < 2:
        return []

    total_length = _polyline_length(path_points)
    start_distance = max(0.0, min(total_length, start_distance))
    end_distance = max(0.0, min(total_length, end_distance))
    if end_distance - start_distance <= MIN_BONE_LENGTH:
        return []

    bias = max(0.0, min(1.0, float(curvature_bias)))
    if bias <= 0.0 or len(path_points) < 3:
        return [
            start_distance + (end_distance - start_distance) * index / (point_count - 1)
            for index in range(point_count)
        ]

    cumulative = [0.0]
    for index in range(len(path_points) - 1):
        cumulative.append(cumulative[-1] + (path_points[index + 1] - path_points[index]).length)

    segment_entries = []
    max_turn = 0.0

    for index in range(len(path_points) - 1):
        segment_start = cumulative[index]
        segment_end = cumulative[index + 1]
        overlap_start = max(start_distance, segment_start)
        overlap_end = min(end_distance, segment_end)
        if overlap_end - overlap_start <= MIN_BONE_LENGTH:
            continue

        turn = 0.0
        for vertex_index in (index, index + 1):
            if vertex_index <= 0 or vertex_index >= len(path_points) - 1:
                continue
            first = path_points[vertex_index] - path_points[vertex_index - 1]
            second = path_points[vertex_index + 1] - path_points[vertex_index]
            if first.length <= MIN_BONE_LENGTH or second.length <= MIN_BONE_LENGTH:
                continue
            turn = max(turn, first.angle(second, 0.0))

        max_turn = max(max_turn, turn)
        segment_entries.append((overlap_start, overlap_end, turn))

    if not segment_entries:
        return []

    weighted_cumulative = [0.0]
    for overlap_start, overlap_end, turn in segment_entries:
        normalized_turn = turn / max_turn if max_turn > MIN_BONE_LENGTH else 0.0
        weight = 1.0 + normalized_turn * bias * 4.0
        weighted_cumulative.append(weighted_cumulative[-1] + (overlap_end - overlap_start) * weight)

    weighted_total = weighted_cumulative[-1]
    if weighted_total <= MIN_BONE_LENGTH:
        return []

    distances = []
    for index in range(point_count):
        target_weight = weighted_total * index / (point_count - 1)
        for entry_index, entry in enumerate(segment_entries):
            weight_start = weighted_cumulative[entry_index]
            weight_end = weighted_cumulative[entry_index + 1]
            if target_weight > weight_end and entry_index < len(segment_entries) - 1:
                continue

            overlap_start, overlap_end, _turn = entry
            factor = 0.0 if weight_end - weight_start <= MIN_BONE_LENGTH else (target_weight - weight_start) / (weight_end - weight_start)
            distances.append(overlap_start + (overlap_end - overlap_start) * max(0.0, min(1.0, factor)))
            break

    return distances


def _segment_distribution_distances(path_points, source_distances, point_count, mode, curvature_bias):
    if len(source_distances) < 2 or point_count < 2:
        return []

    start_distance = source_distances[0]
    end_distance = source_distances[-1]
    if end_distance - start_distance <= MIN_BONE_LENGTH:
        return []

    if mode == "FIT":
        return list(source_distances)
    if mode == "CURVE":
        return _path_weighted_distances(path_points, start_distance, end_distance, point_count, curvature_bias)

    return [
        start_distance + (end_distance - start_distance) * index / (point_count - 1)
        for index in range(point_count)
    ]


def _affected_bezier_indices(point_count, indices):
    affected = set()
    for index in indices:
        affected.add(index)
        if index > 0:
            affected.add(index - 1)
        if index < point_count - 1:
            affected.add(index + 1)
    return affected


def _apply_segment_distribution(context, curve_obj, spline, indices, mode, curvature_bias, path_points=None):
    if len(indices) < 2:
        return 0

    if path_points is None:
        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        return 0

    points = list(_spline_points(spline))
    source_distances = _control_distances_on_path(curve_obj, spline, indices, path_points)
    target_distances = _segment_distribution_distances(
        path_points,
        source_distances,
        len(indices),
        mode,
        curvature_bias,
    )
    if len(target_distances) != len(indices):
        return 0

    radii = [_point_radius(points[index]) for index in indices]
    tilts = [_point_tilt(points[index]) for index in indices]

    for index, target_distance in zip(indices, target_distances):
        point = points[index]
        _move_point_to_world(curve_obj, spline, point, _point_at_distance(path_points, target_distance))
        _set_point_radius(point, _sample_scalar_by_distance(source_distances, radii, target_distance))
        _set_point_tilt(point, _sample_scalar_by_distance(source_distances, tilts, target_distance))

    if spline.type == "BEZIER":
        _reset_bezier_handles_for_indices(spline, _affected_bezier_indices(len(points), indices))

    return len(indices)


def _segment_distribution_runs(spline, selected_mode):
    point_count = _control_point_count(spline)
    if selected_mode:
        return [run for run in _selected_index_runs(spline) if len(run) >= 2]
    return [list(range(point_count))]


def _set_point_selection(point, spline, selected):
    if spline.type == "BEZIER":
        point.select_control_point = selected
        point.select_left_handle = selected
        point.select_right_handle = selected
        return

    point.select = selected


def _spline_point_state(spline, point, selected=False, reset_left=False, reset_right=False):
    state = {
        "co": _point_local_co(point, spline),
        "radius": _point_radius(point),
        "tilt": _point_tilt(point),
        "selected": selected,
        "weight_softbody": getattr(point, "weight_softbody", 0.0),
        "reset_left": reset_left,
        "reset_right": reset_right,
    }

    if spline.type == "BEZIER":
        state.update(
            {
                "handle_left": point.handle_left.copy(),
                "handle_right": point.handle_right.copy(),
                "handle_left_type": point.handle_left_type,
                "handle_right_type": point.handle_right_type,
            }
        )
    else:
        state["weight"] = float(point.co[3])

    return state


def _lerp_value(first, second, factor):
    return first + (second - first) * factor


def _sampled_visual_subdivide_state(curve_obj, spline, path_points, source_distances, radii, tilts, weights, softbody_weights, distance):
    co = curve_obj.matrix_world.inverted() @ _point_at_distance(path_points, distance)
    state = {
        "co": co.copy(),
        "radius": _sample_scalar_by_distance(source_distances, radii, distance),
        "tilt": _sample_scalar_by_distance(source_distances, tilts, distance),
        "selected": True,
        "weight_softbody": _sample_scalar_by_distance(source_distances, softbody_weights, distance),
        "reset_left": True,
        "reset_right": True,
    }

    if spline.type == "BEZIER":
        state.update(
            {
                "handle_left": co.copy(),
                "handle_right": co.copy(),
                "handle_left_type": "FREE",
                "handle_right_type": "FREE",
            }
        )
    else:
        state["weight"] = _sample_scalar_by_distance(source_distances, weights, distance)

    return state


def _visual_subdivide_states_for_run(curve_obj, spline, points, run, cuts, path_points):
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        return []

    source_distances = _control_distances_on_path(curve_obj, spline, run, path_points)
    if len(source_distances) != len(run):
        return []

    run_points = [points[index] for index in run]
    radii = [_point_radius(point) for point in run_points]
    tilts = [_point_tilt(point) for point in run_points]
    softbody_weights = [getattr(point, "weight_softbody", 0.0) for point in run_points]
    weights = [float(point.co[3]) for point in run_points] if spline.type != "BEZIER" else []
    states = []

    for offset in range(len(run_points) - 1):
        start_distance = source_distances[offset]
        end_distance = source_distances[offset + 1]
        if end_distance - start_distance <= MIN_BONE_LENGTH:
            return []

        if offset == 0:
            states.append(_spline_point_state(spline, run_points[0], selected=True, reset_right=True))

        for cut_index in range(1, cuts + 1):
            factor = cut_index / (cuts + 1)
            target_distance = start_distance + (end_distance - start_distance) * factor
            states.append(
                _sampled_visual_subdivide_state(
                    curve_obj,
                    spline,
                    path_points,
                    source_distances,
                    radii,
                    tilts,
                    weights,
                    softbody_weights,
                    target_distance,
                )
            )

        states.append(
            _spline_point_state(
                spline,
                run_points[offset + 1],
                selected=True,
                reset_left=True,
                reset_right=offset < len(run_points) - 2,
            )
        )

    return states


def _reset_bezier_handles_from_subdivide_states(spline, states):
    points = list(spline.bezier_points)
    for index, (point, state) in enumerate(zip(points, states)):
        if state["reset_left"]:
            point.handle_left_type = "FREE"
            if index == 0:
                point.handle_left = point.co
            else:
                point.handle_left = point.co - (point.co - points[index - 1].co) / 3.0

        if state["reset_right"]:
            point.handle_right_type = "FREE"
            if index == len(points) - 1:
                point.handle_right = point.co
            else:
                point.handle_right = point.co + (points[index + 1].co - point.co) / 3.0


def _rebuild_bezier_spline_from_subdivide_states(spline, states):
    points = spline.bezier_points
    extra_count = len(states) - len(points)
    if extra_count > 0:
        points.add(extra_count)
        points = spline.bezier_points

    for point, state in zip(points, states):
        point.handle_left_type = state["handle_left_type"]
        point.handle_right_type = state["handle_right_type"]
        point.co = state["co"]
        point.handle_left = state["handle_left"]
        point.handle_right = state["handle_right"]
        _set_point_radius(point, state["radius"])
        _set_point_tilt(point, state["tilt"])
        _set_point_selection(point, spline, state["selected"])
        if hasattr(point, "weight_softbody"):
            point.weight_softbody = state["weight_softbody"]

    _reset_bezier_handles_from_subdivide_states(spline, states)


def _rebuild_points_spline_from_subdivide_states(spline, states):
    points = spline.points
    extra_count = len(states) - len(points)
    if extra_count > 0:
        points.add(extra_count)
        points = spline.points

    for point, state in zip(points, states):
        co = state["co"]
        point.co = (co.x, co.y, co.z, state["weight"])
        _set_point_radius(point, state["radius"])
        _set_point_tilt(point, state["tilt"])
        _set_point_selection(point, spline, state["selected"])
        if hasattr(point, "weight_softbody"):
            point.weight_softbody = state["weight_softbody"]


def _subdivide_selected_spline(context, curve_obj, spline, cuts):
    points = list(_spline_points(spline))
    runs = [run for run in _selected_index_runs(spline) if len(run) >= 3]
    if not runs:
        return 0

    path_points = _evaluated_spline_path_points(context, curve_obj, spline)
    replacements = {}
    for run in runs:
        states = _visual_subdivide_states_for_run(curve_obj, spline, points, run, cuts, path_points)
        if states:
            replacements[run[0]] = (run[-1], states)

    if not replacements:
        return 0

    rebuilt_states = []
    index = 0
    while index < len(points):
        replacement = replacements.get(index)
        if replacement is not None:
            end_index, states = replacement
            rebuilt_states.extend(states)
            index = end_index + 1
            continue

        rebuilt_states.append(_spline_point_state(spline, points[index], selected=False))
        index += 1

    added_count = len(rebuilt_states) - len(points)
    if added_count <= 0:
        return 0

    if spline.type == "BEZIER":
        _rebuild_bezier_spline_from_subdivide_states(spline, rebuilt_states)
    else:
        _rebuild_points_spline_from_subdivide_states(spline, rebuilt_states)

    return added_count


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


def _smooth_selected_spline_radius(spline, factor, steps):
    return _smooth_selected_spline_scalar(spline, _point_radius, _set_point_radius, factor, steps)


def _smooth_spline_tilt(spline, factor, steps):
    points = _spline_points(spline)
    values = [_point_tilt(point) for point in points]
    for point, value in zip(points, _smooth_values(values, factor, steps)):
        _set_point_tilt(point, value)


def _smooth_selected_spline_tilt(spline, factor, steps):
    return _smooth_selected_spline_scalar(spline, _point_tilt, _set_point_tilt, factor, steps)


def _smooth_selected_spline_scalar(spline, getter, setter, factor, steps):
    points = list(_spline_points(spline))
    values = [getter(point) for point in points]
    changed_indices = set()

    for run in _selected_index_runs(spline):
        if len(run) < 3:
            continue

        run_values = {index: values[index] for index in run}
        for _ in range(steps):
            next_values = dict(run_values)
            for offset, index in enumerate(run[1:-1], start=1):
                previous_index = run[offset - 1]
                next_index = run[offset + 1]
                target = (run_values[previous_index] + run_values[next_index]) * 0.5
                next_values[index] = run_values[index] + (target - run_values[index]) * factor
            run_values = next_values

        for index in run[1:-1]:
            setter(points[index], run_values[index])
            changed_indices.add(index)

    return len(changed_indices)


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
    show_segment_control: BoolProperty(name="Segment Control", default=True)
    show_resolution_batch: BoolProperty(name="Resolution Batch", default=True)
    show_smooth_reset: BoolProperty(name="Smooth / Reset", default=True)
    show_locks: BoolProperty(name="Locks", default=True)
    show_surface_tools: BoolProperty(name="Surface Tools", default=True)
    show_length_tools: BoolProperty(name="Length Tools", default=True)
    show_profile_tools: BoolProperty(name="Profile / Taper", default=True)
    show_bevel_manager: BoolProperty(name="Bevel Manager", default=True)
    show_validation_tools: BoolProperty(name="Validation", default=True)
    show_lod_tools: BoolProperty(name="LOD Tools", default=True)
    show_selection_tools: BoolProperty(name="Selection Tools", default=True)
    show_mirror: BoolProperty(name="Mirror", default=True)
    show_caps: BoolProperty(name="Caps", default=True)
    show_convert_tools: BoolProperty(name="Convert / Bridge", default=True)
    show_rigging: BoolProperty(name="Rigging", default=True)

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

    curvature_bias: FloatProperty(
        name="Curvature Bias",
        description="How strongly curve distribution concentrates points in curved areas",
        default=0.65,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
    )

    subdivide_cuts: IntProperty(
        name="Subdivide Cuts",
        description="Number of new points inserted between each selected segment",
        default=1,
        min=1,
        max=16,
    )

    surface_object: PointerProperty(
        name="Surface",
        description="Mesh surface used by snap and collision tools",
        type=bpy.types.Object,
        poll=_poll_mesh_object,
    )

    surface_offset: FloatProperty(
        name="Offset",
        description="Distance to keep curve points away from the surface",
        default=0.0,
        precision=4,
        subtype="DISTANCE",
    )

    length_value: FloatProperty(
        name="Length",
        description="Target length for Set Length and Match Length operations",
        default=1.0,
        min=0.0,
        precision=4,
        subtype="DISTANCE",
    )

    trim_amount: FloatProperty(
        name="Trim",
        description="Distance removed from root or tip by trim tools",
        default=0.05,
        min=0.0,
        precision=4,
        subtype="DISTANCE",
    )

    profile_preset: EnumProperty(
        name="Preset",
        description="Radius profile preset",
        items=(
            ("FLAT", "Flat", "Use one radius along the curve"),
            ("ROOT_THICK", "Root Thick", "Taper from root radius to tip radius"),
            ("TIP_THIN", "Tip Thin", "Alias taper profile for a thin tip"),
            ("BOTH_THIN", "Both Thin", "Thin root and tip with a thicker middle"),
            ("SHARP_TAPER", "Sharp Taper", "Build a sharp stylized curve profile"),
        ),
        default="ROOT_THICK",
    )

    profile_root_radius: FloatProperty(
        name="Root",
        description="Root radius used by profile presets",
        default=1.0,
        min=0.0,
        precision=4,
    )

    profile_mid_radius: FloatProperty(
        name="Middle",
        description="Middle radius used by profile presets",
        default=0.6,
        min=0.0,
        precision=4,
    )

    profile_tip_radius: FloatProperty(
        name="Tip",
        description="Tip radius used by profile presets",
        default=0.05,
        min=0.0,
        precision=4,
    )

    profile_clipboard: StringProperty(
        name="Profile Clipboard",
        description="Internal radius profile clipboard",
        default="",
        options={"HIDDEN"},
    )

    bevel_object: PointerProperty(
        name="Bevel Object",
        description="Curve object assigned as bevel object",
        type=bpy.types.Object,
        poll=_poll_curve_object,
    )

    validation_report: StringProperty(
        name="Report",
        description="Last validation report",
        default="No validation report.",
    )

    validation_problem_objects: StringProperty(
        name="Problem Objects",
        description="Internal validation object selection cache",
        default="[]",
        options={"HIDDEN"},
    )

    selection_length_threshold: FloatProperty(
        name="Length",
        description="Length threshold for object selection tools",
        default=0.25,
        min=0.0,
        precision=4,
        subtype="DISTANCE",
    )

    export_collection_name: StringProperty(
        name="Collection",
        description="Collection used by safe curve to mesh export",
        default="Curve Toolkit Mesh Export",
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


class CTK_OT_segment_distribute(bpy.types.Operator):
    bl_idname = "curve_toolkit.segment_distribute"
    bl_label = "Distribute Segments"
    bl_description = "Redistribute curve control points along the evaluated visual path"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ("EVEN", "Evenly", "Space points evenly along the visual path"),
            ("CURVE", "Curve", "Space points with extra density in curved areas"),
            ("FIT", "Fit To Visual Path", "Move control points onto the evaluated visual path"),
        ),
        default="EVEN",
    )

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
            path_points = _evaluated_spline_path_points(context, curve_obj, spline)
            for run in _segment_distribution_runs(spline, selected_mode):
                changed_count += _apply_segment_distribution(
                    context,
                    curve_obj,
                    spline,
                    run,
                    self.mode,
                    settings.curvature_bias,
                    path_points,
                )

        if changed_count == 0:
            self.report({"ERROR"}, "No valid open spline segment could be distributed.")
            return {"CANCELLED"}

        context.view_layer.update()
        label = "fit to visual path" if self.mode == "FIT" else self.mode.lower()
        self.report({"INFO"}, f"Updated {changed_count} curve points with {label}.")
        return {"FINISHED"}


class CTK_OT_segment_subdivide_selected(bpy.types.Operator):
    bl_idname = "curve_toolkit.segment_subdivide_selected"
    bl_label = "Subdivide Selected"
    bl_description = "Insert visual-path cuts between selected points without moving the selected points"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        added_count = 0
        has_selected_points = False

        for spline in splines:
            selected_runs = _selected_index_runs(spline)
            if selected_runs:
                has_selected_points = True
            added_count += _subdivide_selected_spline(context, curve_obj, spline, settings.subdivide_cuts)

        if added_count == 0:
            if not has_selected_points:
                self.report({"ERROR"}, "Select at least 3 contiguous curve points to subdivide.")
            else:
                self.report({"ERROR"}, "Selected ranges must contain at least 3 contiguous points.")
            return {"CANCELLED"}

        context.view_layer.update()
        self.report({"INFO"}, f"Added {added_count} curve points.")
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
        selected_mode = _has_selected_points(splines)
        changed_count = 0

        for spline in splines:
            if selected_mode:
                changed_count += _smooth_selected_spline_radius(
                    spline,
                    settings.smooth_factor,
                    settings.smooth_steps,
                )
            else:
                _smooth_spline_radius(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        if selected_mode:
            if changed_count == 0:
                self.report({"ERROR"}, "Select at least 3 contiguous curve points to smooth.")
                return {"CANCELLED"}

            self.report({"INFO"}, f"Smoothed scale for {changed_count} selected curve points.")
            return {"FINISHED"}

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
        selected_mode = _has_selected_points(splines)
        changed_count = 0

        for spline in splines:
            if selected_mode:
                changed_count += _smooth_selected_spline_tilt(
                    spline,
                    settings.smooth_factor,
                    settings.smooth_steps,
                )
            else:
                _smooth_spline_tilt(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        if selected_mode:
            if changed_count == 0:
                self.report({"ERROR"}, "Select at least 3 contiguous curve points to smooth.")
                return {"CANCELLED"}

            self.report({"INFO"}, f"Smoothed twist for {changed_count} selected curve points.")
            return {"FINISHED"}

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


class CTK_OT_set_surface_from_active(bpy.types.Operator):
    bl_idname = "curve_toolkit.set_surface_from_active"
    bl_label = "Use Active Mesh"
    bl_description = "Use the active mesh object as the Surface target"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.view_layer.objects.active
        return obj is not None and obj.type == "MESH"

    def execute(self, context):
        settings = context.scene.curve_toolkit
        settings.surface_object = context.view_layer.objects.active
        self.report({"INFO"}, f"Surface set to {settings.surface_object.name}.")
        return {"FINISHED"}


class CTK_OT_surface_snap(bpy.types.Operator):
    bl_idname = "curve_toolkit.surface_snap"
    bl_label = "Surface Snap"
    bl_description = "Snap selected curve points to the registered surface"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=(
            ("ROOT", "Root", "Snap only the root control point"),
            ("CURVE", "Curve", "Snap every control point"),
            ("OFFSET", "Offset", "Move each curve so its root reaches the surface"),
            ("PUSH_OUT", "Push Out", "Push points outside the surface offset distance"),
        ),
        default="ROOT",
    )

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        surface_obj = settings.surface_object
        if surface_obj is None or surface_obj.type != "MESH":
            self.report({"ERROR"}, "Set a mesh Surface first.")
            return {"CANCELLED"}

        curves = _target_curve_objects(context)
        if not curves:
            self.report({"ERROR"}, "Select at least one Curve object.")
            return {"CANCELLED"}

        _mode_set_object(context)
        surface_data = _surface_bvh_from_object(context, surface_obj)
        changed_count = 0
        for curve_obj in curves:
            for spline in _editable_splines(curve_obj):
                if self.mode == "ROOT":
                    changed_count += int(_move_spline_root_to_surface(curve_obj, spline, surface_data, settings.surface_offset))
                elif self.mode == "CURVE":
                    changed_count += int(_snap_spline_to_surface(curve_obj, spline, surface_data, settings.surface_offset))
                elif self.mode == "OFFSET":
                    changed_count += int(_offset_spline_root_to_surface(curve_obj, spline, surface_data, settings.surface_offset))
                elif self.mode == "PUSH_OUT":
                    changed_count += int(_push_spline_from_surface(curve_obj, spline, surface_data, settings.surface_offset))

        context.view_layer.update()
        if changed_count == 0:
            self.report({"ERROR"}, "No curve points could be moved to the surface.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Updated {changed_count} curve splines with Surface tools.")
        return {"FINISHED"}


class CTK_OT_length_store(bpy.types.Operator):
    bl_idname = "curve_toolkit.length_store"
    bl_label = "Store Length"
    bl_description = "Store current selected curve spline lengths"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        curves = _target_curve_objects(context)
        stored_count = sum(_store_lengths(curve_obj) for curve_obj in curves)
        self.report({"INFO"}, f"Stored {stored_count} spline lengths.")
        return {"FINISHED"}


class CTK_OT_length_restore(bpy.types.Operator):
    bl_idname = "curve_toolkit.length_restore"
    bl_label = "Restore Length"
    bl_description = "Restore previously stored selected curve spline lengths"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        _mode_set_object(context)
        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            lengths = _stored_lengths(curve_obj)
            for spline, target_length in zip(_editable_splines(curve_obj), lengths):
                changed_count += int(_set_spline_length(curve_obj, spline, float(target_length)))

        context.view_layer.update()
        if changed_count == 0:
            self.report({"ERROR"}, "No stored lengths were found.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Restored {changed_count} spline lengths.")
        return {"FINISHED"}


class CTK_OT_length_set(bpy.types.Operator):
    bl_idname = "curve_toolkit.length_set"
    bl_label = "Set Length"
    bl_description = "Set selected curve splines to the Length value"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        _mode_set_object(context)
        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            for spline in _editable_splines(curve_obj):
                changed_count += int(_set_spline_length(curve_obj, spline, settings.length_value))

        context.view_layer.update()
        if changed_count == 0:
            self.report({"ERROR"}, "No spline length could be changed.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Set {changed_count} spline lengths.")
        return {"FINISHED"}


class CTK_OT_length_match_active(bpy.types.Operator):
    bl_idname = "curve_toolkit.length_match_active"
    bl_label = "Match Active Length"
    bl_description = "Set selected curve splines to the active curve's first spline length"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        active_curve = _active_curve(context)
        active_splines = _editable_splines(active_curve)
        if not active_splines:
            self.report({"ERROR"}, "Active curve has no valid spline.")
            return {"CANCELLED"}

        target_length = _polyline_length(_spline_world_positions(active_curve, active_splines[0]))
        _mode_set_object(context)
        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            for spline in _editable_splines(curve_obj):
                changed_count += int(_set_spline_length(curve_obj, spline, target_length))

        context.view_layer.update()
        self.report({"INFO"}, f"Matched {changed_count} spline lengths to active curve.")
        return {"FINISHED"}


class CTK_OT_length_trim(bpy.types.Operator):
    bl_idname = "curve_toolkit.length_trim"
    bl_label = "Trim Length"
    bl_description = "Trim root or tip by the Trim distance"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=(
            ("ROOT", "Root", "Trim from the root"),
            ("TIP", "Tip", "Trim from the tip"),
        ),
        default="TIP",
    )

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        _mode_set_object(context)
        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            for spline in _editable_splines(curve_obj):
                root_amount = settings.trim_amount if self.mode == "ROOT" else 0.0
                tip_amount = settings.trim_amount if self.mode == "TIP" else 0.0
                changed_count += int(_trim_spline(curve_obj, spline, root_amount, tip_amount))

        context.view_layer.update()
        if changed_count == 0:
            self.report({"ERROR"}, "Trim distance is too large or curves are invalid.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Trimmed {changed_count} splines.")
        return {"FINISHED"}


class CTK_OT_profile_apply(bpy.types.Operator):
    bl_idname = "curve_toolkit.profile_apply"
    bl_label = "Apply Profile"
    bl_description = "Apply the selected radius profile to selected curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        splines = []
        for curve_obj in _target_curve_objects(context):
            splines.extend(_editable_splines(curve_obj))

        selected_mode = _has_selected_points(splines)
        changed_count = 0
        for spline in splines:
            changed_count += _apply_radius_profile_to_spline(
                spline,
                settings.profile_preset,
                settings.profile_root_radius,
                settings.profile_tip_radius,
                settings.profile_mid_radius,
                selected_mode,
            )

        context.view_layer.update()
        if changed_count == 0:
            self.report({"ERROR"}, "No curve points found for radius profile.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Applied radius profile to {changed_count} points.")
        return {"FINISHED"}


class CTK_OT_profile_copy(bpy.types.Operator):
    bl_idname = "curve_toolkit.profile_copy"
    bl_label = "Copy Profile"
    bl_description = "Copy the active curve's first radius profile"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        splines = _editable_splines(curve_obj)
        if not splines:
            self.report({"ERROR"}, "Active curve has no valid spline.")
            return {"CANCELLED"}

        values = [_point_radius(point) for point in _spline_points(splines[0])]
        context.scene.curve_toolkit.profile_clipboard = json.dumps(values)
        self.report({"INFO"}, f"Copied radius profile with {len(values)} points.")
        return {"FINISHED"}


class CTK_OT_profile_paste(bpy.types.Operator):
    bl_idname = "curve_toolkit.profile_paste"
    bl_label = "Paste Profile"
    bl_description = "Paste copied radius profile to selected curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        raw_values = context.scene.curve_toolkit.profile_clipboard
        try:
            values = json.loads(raw_values)
        except (TypeError, ValueError):
            values = []

        if not values:
            self.report({"ERROR"}, "Copy a radius profile first.")
            return {"CANCELLED"}

        curves = _target_curve_objects(context)
        splines = [spline for curve_obj in curves for spline in _editable_splines(curve_obj)]
        selected_mode = _has_selected_points(splines)
        changed_count = 0
        for spline in splines:
            points = list(_spline_points(spline))
            if selected_mode:
                for run in _selected_index_runs(spline):
                    changed_count += _apply_sampled_radius_values(points, run, values)
            else:
                changed_count += _apply_sampled_radius_values(points, list(range(len(points))), values)

        context.view_layer.update()
        self.report({"INFO"}, f"Pasted radius profile to {changed_count} points.")
        return {"FINISHED"}


class CTK_OT_bevel_assign(bpy.types.Operator):
    bl_idname = "curve_toolkit.bevel_assign"
    bl_label = "Assign Bevel"
    bl_description = "Assign the Bevel Object to selected curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        bevel_obj = settings.bevel_object
        if bevel_obj is None or bevel_obj.type != "CURVE":
            self.report({"ERROR"}, "Choose a Curve Bevel Object first.")
            return {"CANCELLED"}

        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            if curve_obj == bevel_obj:
                continue
            curve_obj.data.bevel_object = bevel_obj
            changed_count += 1

        self.report({"INFO"}, f"Assigned bevel object to {changed_count} curves.")
        return {"FINISHED"}


class CTK_OT_bevel_copy_active(bpy.types.Operator):
    bl_idname = "curve_toolkit.bevel_copy_active"
    bl_label = "Copy Active Bevel"
    bl_description = "Copy the active curve's bevel object to selected curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        active = _active_curve(context)
        bevel_obj = active.data.bevel_object
        if bevel_obj is None:
            self.report({"ERROR"}, "Active curve has no bevel object.")
            return {"CANCELLED"}

        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            if curve_obj != active:
                curve_obj.data.bevel_object = bevel_obj
                changed_count += 1

        self.report({"INFO"}, f"Copied active bevel object to {changed_count} curves.")
        return {"FINISHED"}


class CTK_OT_bevel_clear(bpy.types.Operator):
    bl_idname = "curve_toolkit.bevel_clear"
    bl_label = "Clear Bevel"
    bl_description = "Clear bevel objects from selected curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            curve_obj.data.bevel_object = None
            changed_count += 1

        self.report({"INFO"}, f"Cleared bevel objects from {changed_count} curves.")
        return {"FINISHED"}


class CTK_OT_bevel_select_same(bpy.types.Operator):
    bl_idname = "curve_toolkit.bevel_select_same"
    bl_label = "Select Same Bevel"
    bl_description = "Select scene curves using the same bevel object as the active curve"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        active = _active_curve(context)
        bevel_obj = active.data.bevel_object
        if bevel_obj is None:
            self.report({"ERROR"}, "Active curve has no bevel object.")
            return {"CANCELLED"}

        count = 0
        for obj in context.scene.objects:
            selected = obj.type == "CURVE" and obj.data.bevel_object == bevel_obj
            obj.select_set(selected)
            count += int(selected)

        context.view_layer.objects.active = active
        self.report({"INFO"}, f"Selected {count} curves with the same bevel object.")
        return {"FINISHED"}


class CTK_OT_validate_curves(bpy.types.Operator):
    bl_idname = "curve_toolkit.validate_curves"
    bl_label = "Check Curves"
    bl_description = "Validate selected curves and summarize common workflow issues"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.curve_toolkit
        curves = _target_or_scene_curve_objects(context)
        problem_objects = []
        issue_counts = {
            "missing bevel": 0,
            "zero length": 0,
            "close points": 0,
            "non-applied scale": 0,
            "closed spline": 0,
            "twist lock": 0,
        }

        for curve_obj in curves:
            has_problem = False
            if curve_obj.data.bevel_object is None:
                issue_counts["missing bevel"] += 1
                has_problem = True
            if any(abs(value - 1.0) > 1.0e-4 for value in curve_obj.scale):
                issue_counts["non-applied scale"] += 1
                has_problem = True
            if _custom_bool(curve_obj, "ctk_lock_twist", "hmt_lock_twist"):
                issue_counts["twist lock"] += 1
                has_problem = True

            for spline in curve_obj.data.splines:
                if not _is_supported_spline(spline):
                    continue
                if _is_closed_spline(spline):
                    issue_counts["closed spline"] += 1
                    has_problem = True
                positions = _spline_world_positions(curve_obj, spline)
                if len(positions) < 2 or _polyline_length(positions) <= MIN_BONE_LENGTH:
                    issue_counts["zero length"] += 1
                    has_problem = True
                if any((positions[index + 1] - positions[index]).length <= MIN_BONE_LENGTH for index in range(len(positions) - 1)):
                    issue_counts["close points"] += 1
                    has_problem = True

            if has_problem:
                problem_objects.append(curve_obj.name)

        active_counts = [f"{name}: {count}" for name, count in issue_counts.items() if count]
        report = f"Checked {len(curves)} curves. Problems: {len(problem_objects)}."
        if active_counts:
            report += " " + "; ".join(active_counts)
        settings.validation_report = report
        settings.validation_problem_objects = json.dumps(problem_objects)
        self.report({"INFO"}, report)
        return {"FINISHED"}


class CTK_OT_select_validation_problems(bpy.types.Operator):
    bl_idname = "curve_toolkit.select_validation_problems"
    bl_label = "Select Problems"
    bl_description = "Select curves found by the last validation"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            names = json.loads(context.scene.curve_toolkit.validation_problem_objects)
        except (TypeError, ValueError):
            names = []

        if not names:
            self.report({"ERROR"}, "Run Check Curves first.")
            return {"CANCELLED"}

        count = 0
        for obj in context.scene.objects:
            selected = obj.name in names
            obj.select_set(selected)
            count += int(selected)

        self.report({"INFO"}, f"Selected {count} problem curves.")
        return {"FINISHED"}


class CTK_OT_apply_lod_preset(bpy.types.Operator):
    bl_idname = "curve_toolkit.apply_lod_preset"
    bl_label = "Apply LOD"
    bl_description = "Apply viewport and render resolution presets"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset",
        items=(
            ("DRAFT", "Draft", "Low resolution for fast editing"),
            ("WORK", "Work", "Balanced working resolution"),
            ("FINAL", "Final", "Higher resolution and filled caps"),
        ),
        default="WORK",
    )

    def execute(self, context):
        global _RESOLUTION_BATCH_UPDATE_RUNNING

        settings = context.scene.curve_toolkit
        preset_values = {
            "DRAFT": (1, 0, False),
            "WORK": (2, 2, False),
            "FINAL": (8, 4, True),
        }
        path_resolution, bevel_resolution, fill_caps = preset_values[self.preset]

        curves = _target_curve_objects(context)
        if not curves:
            collections = _resolution_batch_collections(settings)
            curves, bevel_references = _resolution_batch_targets_from_collections(collections)
        else:
            bevel_references = sorted(
                {obj.data.bevel_object for obj in curves if obj.data.bevel_object is not None and obj.data.bevel_object.type == "CURVE"},
                key=lambda obj: obj.name,
            )

        for curve_obj in curves:
            _set_curve_data_resolution(curve_obj, path_resolution)
            curve_obj.data.use_fill_caps = fill_caps
        for bevel_obj in bevel_references:
            _set_curve_data_resolution(bevel_obj, bevel_resolution)

        _RESOLUTION_BATCH_UPDATE_RUNNING = True
        try:
            settings.path_resolution = path_resolution
            settings.bevel_reference_resolution = bevel_resolution
        finally:
            _RESOLUTION_BATCH_UPDATE_RUNNING = False

        self.report({"INFO"}, f"Applied {self.preset.title()} LOD to {len(curves)} curves and {len(bevel_references)} bevel references.")
        return {"FINISHED"}


class CTK_OT_select_curve_points(bpy.types.Operator):
    bl_idname = "curve_toolkit.select_curve_points"
    bl_label = "Select Points"
    bl_description = "Select root, tip, all, or no control points on selected curves"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=(
            ("ROOT", "Root", "Select root points"),
            ("TIP", "Tip", "Select tip points"),
            ("ALL", "All", "Select all points"),
            ("NONE", "None", "Clear point selection"),
        ),
        default="ROOT",
    )

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        changed_count = 0
        for curve_obj in _target_curve_objects(context):
            for spline in _editable_splines(curve_obj):
                points = list(_spline_points(spline))
                for index, point in enumerate(points):
                    selected = (
                        self.mode == "ALL"
                        or (self.mode == "ROOT" and index == 0)
                        or (self.mode == "TIP" and index == len(points) - 1)
                    )
                    if self.mode == "NONE":
                        selected = False

                    if spline.type == "BEZIER":
                        point.select_control_point = selected
                        point.select_left_handle = False
                        point.select_right_handle = False
                    else:
                        point.select = selected
                    changed_count += int(selected)

        self.report({"INFO"}, f"Selected {changed_count} curve points.")
        return {"FINISHED"}


class CTK_OT_select_curves_by_length(bpy.types.Operator):
    bl_idname = "curve_toolkit.select_curves_by_length"
    bl_label = "Select By Length"
    bl_description = "Select scene curves shorter or longer than the Length threshold"
    bl_options = {"REGISTER", "UNDO"}

    mode: EnumProperty(
        name="Mode",
        items=(
            ("SHORTER", "Shorter", "Select curves shorter than Length"),
            ("LONGER", "Longer", "Select curves longer than Length"),
        ),
        default="SHORTER",
    )

    def execute(self, context):
        threshold = context.scene.curve_toolkit.selection_length_threshold
        count = 0
        for obj in context.scene.objects:
            if obj.type != "CURVE":
                obj.select_set(False)
                continue

            max_length = max((_polyline_length(_spline_world_positions(obj, spline)) for spline in _editable_splines(obj)), default=0.0)
            selected = max_length < threshold if self.mode == "SHORTER" else max_length > threshold
            obj.select_set(selected)
            count += int(selected)

        self.report({"INFO"}, f"Selected {count} curves by length.")
        return {"FINISHED"}


class CTK_OT_convert_curves_to_mesh(bpy.types.Operator):
    bl_idname = "curve_toolkit.convert_curves_to_mesh"
    bl_label = "Export Mesh Copy"
    bl_description = "Create mesh copies of selected curves in a separate collection"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        collection = bpy.data.collections.get(settings.export_collection_name)
        if collection is None:
            collection = bpy.data.collections.new(settings.export_collection_name)
            context.scene.collection.children.link(collection)

        depsgraph = context.evaluated_depsgraph_get()
        created = []
        for curve_obj in _target_curve_objects(context):
            eval_obj = curve_obj.evaluated_get(depsgraph)
            mesh = bpy.data.meshes.new_from_object(eval_obj, depsgraph=depsgraph)
            mesh.name = _unique_name(f"{curve_obj.name}_mesh", bpy.data.meshes.keys())
            mesh_obj = bpy.data.objects.new(_unique_name(f"{curve_obj.name}_mesh", bpy.data.objects.keys()), mesh)
            mesh_obj.matrix_world = curve_obj.matrix_world.copy()
            for slot in curve_obj.material_slots:
                if slot.material is not None:
                    mesh.materials.append(slot.material)
            collection.objects.link(mesh_obj)
            created.append(mesh_obj)

        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in created:
            obj.select_set(True)
        if created:
            context.view_layer.objects.active = created[0]

        self.report({"INFO"}, f"Created {len(created)} mesh copies in {collection.name}.")
        return {"FINISHED"}


class CTK_OT_convert_curves_object_to_curve(bpy.types.Operator):
    bl_idname = "curve_toolkit.convert_curves_object_to_curve"
    bl_label = "Curves Object to Curve"
    bl_description = "Convert active Curves object to a legacy Curve object for Curve Toolkit tools"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        obj = context.view_layer.objects.active
        return obj is not None and obj.type == "CURVES"

    def execute(self, context):
        source_obj = context.view_layer.objects.active
        source_data = source_obj.data
        if len(source_data.curves) == 0:
            self.report({"ERROR"}, "Active Curves object has no curves.")
            return {"CANCELLED"}

        curve_data = bpy.data.curves.new(_unique_name(f"{source_obj.name}_curve", bpy.data.curves.keys()), "CURVE")
        curve_data.dimensions = "3D"
        curve_data.resolution_u = 2
        curve_data.render_resolution_u = 2

        for source_curve in source_data.curves:
            if source_curve.points_length < 2:
                continue
            spline = curve_data.splines.new("POLY")
            spline.points.add(source_curve.points_length - 1)
            for target_point, source_point in zip(spline.points, source_curve.points):
                position = source_point.position
                target_point.co = (position.x, position.y, position.z, 1.0)
                target_point.radius = source_point.radius

        if not curve_data.splines:
            bpy.data.curves.remove(curve_data)
            self.report({"ERROR"}, "Curves object has no curve with at least 2 points.")
            return {"CANCELLED"}

        curve_obj = bpy.data.objects.new(_unique_name(f"{source_obj.name}_curve", bpy.data.objects.keys()), curve_data)
        curve_obj.matrix_world = source_obj.matrix_world.copy()
        _link_target_collection(context, source_obj).objects.link(curve_obj)
        _set_active_only(context, curve_obj)
        self.report({"INFO"}, f"Converted Curves object to {curve_obj.name}.")
        return {"FINISHED"}


def _flat_curve_point_indices(curve_obj):
    indices = []
    flat_index = 0
    for spline in curve_obj.data.splines:
        if not _is_supported_spline(spline):
            continue
        for point in _spline_points(spline):
            indices.append((flat_index, spline, point))
            flat_index += 1
    return indices


def _point_segment_distance(point, start, end):
    segment = end - start
    if segment.length <= MIN_BONE_LENGTH:
        return (point - start).length
    factor = max(0.0, min(1.0, (point - start).dot(segment) / segment.length_squared))
    return (point - (start + segment * factor)).length


def _nearest_bone_name(armature_obj, world_position):
    best_name = ""
    best_distance = None
    for bone in armature_obj.data.bones:
        head = armature_obj.matrix_world @ bone.head_local
        tail = armature_obj.matrix_world @ bone.tail_local
        distance = _point_segment_distance(world_position, head, tail)
        if best_distance is None or distance < best_distance:
            best_name = bone.name
            best_distance = distance
    return best_name


class CTK_OT_bind_hooks_to_armature(bpy.types.Operator):
    bl_idname = "curve_toolkit.bind_hooks_to_armature"
    bl_label = "Bind Hooks"
    bl_description = "Add Hook modifiers from selected curve points to the nearest armature bones"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context)) and _active_or_selected_armature(context) is not None

    def execute(self, context):
        armature_obj = _active_or_selected_armature(context)
        if armature_obj is None or not armature_obj.data.bones:
            self.report({"ERROR"}, "Select an armature with bones.")
            return {"CANCELLED"}

        _mode_set_object(context)
        hook_count = 0
        for curve_obj in _target_curve_objects(context):
            for flat_index, spline, point in _flat_curve_point_indices(curve_obj):
                bone_name = _nearest_bone_name(armature_obj, _point_world_co(curve_obj, spline, point))
                if not bone_name:
                    continue
                modifier = curve_obj.modifiers.new(_unique_name(f"CTK Hook {bone_name}", [item.name for item in curve_obj.modifiers]), "HOOK")
                modifier.object = armature_obj
                modifier.subtarget = bone_name
                try:
                    modifier.vertex_indices_set([flat_index])
                except RuntimeError:
                    curve_obj.modifiers.remove(modifier)
                    continue
                hook_count += 1

        if hook_count == 0:
            self.report({"ERROR"}, "No hook modifiers could be created.")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Created {hook_count} hook modifiers.")
        return {"FINISHED"}


class CTK_OT_clear_hook_modifiers(bpy.types.Operator):
    bl_idname = "curve_toolkit.clear_hook_modifiers"
    bl_label = "Clear Hooks"
    bl_description = "Remove Hook modifiers from selected curves"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        removed_count = 0
        for curve_obj in _target_curve_objects(context):
            for modifier in list(curve_obj.modifiers):
                if modifier.type == "HOOK":
                    curve_obj.modifiers.remove(modifier)
                    removed_count += 1

        self.report({"INFO"}, f"Removed {removed_count} hook modifiers.")
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
            curve_box.operator(CTK_OT_flip_twist.bl_idname)

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

        segment_box = layout.box()
        if self._draw_foldout(segment_box, settings, "show_segment_control", "Segment Control", "IPO_EASE_IN_OUT"):
            segment_column = segment_box.column(align=True)
            segment_column.enabled = curve_obj is not None
            segment_column.label(text="Distribution")
            segment_column.prop(settings, "curvature_bias", slider=True)
            row = segment_column.row(align=True)
            op = row.operator(CTK_OT_segment_distribute.bl_idname, text="Distribute Evenly")
            op.mode = "EVEN"
            op = row.operator(CTK_OT_segment_distribute.bl_idname, text="Distribute Curve")
            op.mode = "CURVE"
            op = segment_column.operator(CTK_OT_segment_distribute.bl_idname, text="Fit To Visual Path")
            op.mode = "FIT"

            segment_column.separator()
            segment_column.label(text="Subdivide")
            segment_column.prop(settings, "subdivide_cuts")
            segment_column.operator(CTK_OT_segment_subdivide_selected.bl_idname)

        surface_box = layout.box()
        if self._draw_foldout(surface_box, settings, "show_surface_tools", "Surface Tools", "MOD_SHRINKWRAP"):
            row = surface_box.row(align=True)
            row.prop(settings, "surface_object")
            row.operator(CTK_OT_set_surface_from_active.bl_idname, text="", icon="EYEDROPPER")
            surface_box.prop(settings, "surface_offset")

            row = surface_box.row(align=True)
            op = row.operator(CTK_OT_surface_snap.bl_idname, text="Snap Root")
            op.mode = "ROOT"
            op = row.operator(CTK_OT_surface_snap.bl_idname, text="Snap Curve")
            op.mode = "CURVE"
            row = surface_box.row(align=True)
            op = row.operator(CTK_OT_surface_snap.bl_idname, text="Offset Curve")
            op.mode = "OFFSET"
            op = row.operator(CTK_OT_surface_snap.bl_idname, text="Push Out")
            op.mode = "PUSH_OUT"

        length_box = layout.box()
        if self._draw_foldout(length_box, settings, "show_length_tools", "Length Tools", "DRIVER_DISTANCE"):
            row = length_box.row(align=True)
            row.prop(settings, "length_value")
            row.operator(CTK_OT_length_set.bl_idname)
            length_box.operator(CTK_OT_length_match_active.bl_idname)

            row = length_box.row(align=True)
            row.prop(settings, "trim_amount")
            op = row.operator(CTK_OT_length_trim.bl_idname, text="Root")
            op.mode = "ROOT"
            op = row.operator(CTK_OT_length_trim.bl_idname, text="Tip")
            op.mode = "TIP"

            row = length_box.row(align=True)
            row.operator(CTK_OT_length_store.bl_idname)
            row.operator(CTK_OT_length_restore.bl_idname)

        profile_box = layout.box()
        if self._draw_foldout(profile_box, settings, "show_profile_tools", "Profile / Taper", "IPO_EASE_IN_OUT"):
            profile_box.prop(settings, "profile_preset")
            row = profile_box.row(align=True)
            row.prop(settings, "profile_root_radius")
            row.prop(settings, "profile_mid_radius")
            row.prop(settings, "profile_tip_radius")
            profile_box.operator(CTK_OT_profile_apply.bl_idname)

            row = profile_box.row(align=True)
            row.operator(CTK_OT_profile_copy.bl_idname)
            row.operator(CTK_OT_profile_paste.bl_idname)

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

        bevel_box = layout.box()
        if self._draw_foldout(bevel_box, settings, "show_bevel_manager", "Bevel Manager", "CURVE_BEZCURVE"):
            bevel_box.prop(settings, "bevel_object")
            row = bevel_box.row(align=True)
            row.operator(CTK_OT_bevel_assign.bl_idname)
            row.operator(CTK_OT_bevel_copy_active.bl_idname)
            row = bevel_box.row(align=True)
            row.operator(CTK_OT_bevel_clear.bl_idname)
            row.operator(CTK_OT_bevel_select_same.bl_idname)

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

        validation_box = layout.box()
        if self._draw_foldout(validation_box, settings, "show_validation_tools", "Validation", "CHECKMARK"):
            validation_box.operator(CTK_OT_validate_curves.bl_idname)
            validation_box.label(text=settings.validation_report)
            validation_box.operator(CTK_OT_select_validation_problems.bl_idname)

        lod_box = layout.box()
        if self._draw_foldout(lod_box, settings, "show_lod_tools", "LOD Tools", "SETTINGS"):
            row = lod_box.row(align=True)
            op = row.operator(CTK_OT_apply_lod_preset.bl_idname, text="Draft")
            op.preset = "DRAFT"
            op = row.operator(CTK_OT_apply_lod_preset.bl_idname, text="Work")
            op.preset = "WORK"
            op = row.operator(CTK_OT_apply_lod_preset.bl_idname, text="Final")
            op.preset = "FINAL"

        selection_box = layout.box()
        if self._draw_foldout(selection_box, settings, "show_selection_tools", "Selection Tools", "RESTRICT_SELECT_OFF"):
            row = selection_box.row(align=True)
            op = row.operator(CTK_OT_select_curve_points.bl_idname, text="Roots")
            op.mode = "ROOT"
            op = row.operator(CTK_OT_select_curve_points.bl_idname, text="Tips")
            op.mode = "TIP"
            row = selection_box.row(align=True)
            op = row.operator(CTK_OT_select_curve_points.bl_idname, text="All Points")
            op.mode = "ALL"
            op = row.operator(CTK_OT_select_curve_points.bl_idname, text="Clear Points")
            op.mode = "NONE"

            selection_box.prop(settings, "selection_length_threshold")
            row = selection_box.row(align=True)
            op = row.operator(CTK_OT_select_curves_by_length.bl_idname, text="Shorter")
            op.mode = "SHORTER"
            op = row.operator(CTK_OT_select_curves_by_length.bl_idname, text="Longer")
            op.mode = "LONGER"

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

        convert_box = layout.box()
        if self._draw_foldout(convert_box, settings, "show_convert_tools", "Convert / Bridge", "MESH_DATA"):
            convert_box.prop(settings, "export_collection_name")
            convert_box.operator(CTK_OT_convert_curves_to_mesh.bl_idname)
            convert_box.operator(CTK_OT_convert_curves_object_to_curve.bl_idname)

        rigging_box = layout.box()
        if self._draw_foldout(rigging_box, settings, "show_rigging", "Rigging", "ARMATURE_DATA"):
            rigging_box.label(text="From Control Points")
            control_point_column = rigging_box.column(align=True)
            control_point_column.enabled = curve_obj is not None
            control_point_column.operator(
                CTK_OT_generate_bones_from_active_curve.bl_idname,
                text="Generate From Points",
                icon="ARMATURE_DATA",
            )

            rigging_box.separator()
            rigging_box.label(text="Custom Count")
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
            rigging_box.label(text="Armature Tools")
            invert_row = rigging_box.row(align=True)
            invert_row.enabled = armature_obj is not None
            invert_row.operator(CTK_OT_invert_selected_bones.bl_idname)
            row = rigging_box.row(align=True)
            row.operator(CTK_OT_bind_hooks_to_armature.bl_idname)
            row.operator(CTK_OT_clear_hook_modifiers.bl_idname)


classes = (
    CTK_PG_resolution_collection_item,
    CTK_PG_settings,
    CTK_OT_set_surface_from_active,
    CTK_OT_surface_snap,
    CTK_OT_length_store,
    CTK_OT_length_restore,
    CTK_OT_length_set,
    CTK_OT_length_match_active,
    CTK_OT_length_trim,
    CTK_OT_profile_apply,
    CTK_OT_profile_copy,
    CTK_OT_profile_paste,
    CTK_OT_bevel_assign,
    CTK_OT_bevel_copy_active,
    CTK_OT_bevel_clear,
    CTK_OT_bevel_select_same,
    CTK_OT_validate_curves,
    CTK_OT_select_validation_problems,
    CTK_OT_apply_lod_preset,
    CTK_OT_select_curve_points,
    CTK_OT_select_curves_by_length,
    CTK_OT_convert_curves_to_mesh,
    CTK_OT_convert_curves_object_to_curve,
    CTK_OT_bind_hooks_to_armature,
    CTK_OT_clear_hook_modifiers,
    CTK_OT_generate_bones_from_active_curve,
    CTK_OT_generate_custom_bones_from_active_curve,
    CTK_OT_invert_selected_bones,
    CTK_OT_reset_path,
    CTK_OT_reset_path_x_axis,
    CTK_OT_switch_direction,
    CTK_OT_set_origin,
    CTK_OT_snap_cursor,
    CTK_OT_segment_distribute,
    CTK_OT_segment_subdivide_selected,
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
