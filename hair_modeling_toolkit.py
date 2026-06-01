# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Hair Modeling Toolkit",
    "author": "madan6557",
    "version": (1, 2, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Hair Toolkit",
    "description": "Curve hair modeling tools and bone chain generation.",
    "category": "Curve",
}

import bpy
from bpy.props import FloatProperty, IntProperty, PointerProperty
from mathutils import Vector


MIN_BONE_LENGTH = 1.0e-6


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


def _set_curve_twist_mode(curve_obj, twist_mode):
    if hasattr(curve_obj.data, "twist_mode"):
        curve_obj.data.twist_mode = twist_mode
    if twist_mode == "Z_UP" and hasattr(curve_obj.data, "twist_smooth"):
        curve_obj.data.twist_smooth = 0.0


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


class HMT_PG_settings(bpy.types.PropertyGroup):
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


class HMT_OT_generate_bones_from_active_curve(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.generate_bones_from_active_curve"
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


class HMT_OT_reset_path(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.reset_path"
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


class HMT_OT_reset_path_x_axis(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.reset_path_x_axis"
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


class HMT_OT_switch_direction(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.switch_direction"
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


class HMT_OT_set_origin(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.set_origin"
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


class HMT_OT_snap_cursor(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.snap_cursor"
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


class HMT_OT_smooth_scale(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.smooth_scale"
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

        settings = context.scene.hair_modeling_toolkit
        for spline in splines:
            _smooth_spline_radius(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        self.report({"INFO"}, f"Smoothed scale for {len(splines)} splines.")
        return {"FINISHED"}


class HMT_OT_smooth_curve(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.smooth_curve"
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

        settings = context.scene.hair_modeling_toolkit
        for spline in splines:
            _smooth_spline_positions(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        self.report({"INFO"}, f"Smoothed curve for {len(splines)} splines.")
        return {"FINISHED"}


class HMT_OT_smooth_twist(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.smooth_twist"
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

        settings = context.scene.hair_modeling_toolkit
        for spline in splines:
            _smooth_spline_tilt(spline, settings.smooth_factor, settings.smooth_steps)

        context.view_layer.update()
        self.report({"INFO"}, f"Smoothed twist for {len(splines)} splines.")
        return {"FINISHED"}


class HMT_OT_reset_scale(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.reset_scale"
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


class HMT_OT_reset_twist(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.reset_twist"
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


class HMT_OT_lock_twist(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.lock_twist"
    bl_label = "Lock Twist"
    bl_description = "Use Z-Up twist mode to prevent unwanted automatic curve twist while preserving manual tilt"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}

        _set_curve_twist_mode(curve_obj, "Z_UP")
        context.view_layer.update()
        self.report({"INFO"}, "Locked automatic twist with Z-Up mode.")
        return {"FINISHED"}


class HMT_OT_unlock_twist(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.unlock_twist"
    bl_label = "Unlock Twist"
    bl_description = "Restore Blender's default minimum twist mode while preserving manual tilt"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}

        _set_curve_twist_mode(curve_obj, "MINIMUM")
        context.view_layer.update()
        self.report({"INFO"}, "Unlocked automatic twist with Minimum mode.")
        return {"FINISHED"}


class HMT_OT_duplicate_mirror_selected_curves(bpy.types.Operator):
    bl_idname = "hair_modeling_toolkit.duplicate_mirror_selected_curves"
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


class HMT_PT_tools(bpy.types.Panel):
    bl_label = "Hair Modeling Toolkit"
    bl_idname = "HMT_PT_tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Hair Toolkit"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.hair_modeling_toolkit

        curve_box = layout.box()
        curve_box.label(text="Curve Controls", icon="CURVE_DATA")
        row = curve_box.row(align=True)
        row.operator(HMT_OT_reset_path.bl_idname)
        row.operator(HMT_OT_reset_path_x_axis.bl_idname)
        row.operator(HMT_OT_switch_direction.bl_idname)

        curve_box.label(text="Origin To")
        row = curve_box.row(align=True)
        op = row.operator(HMT_OT_set_origin.bl_idname, text="Root")
        op.mode = "ROOT"
        op = row.operator(HMT_OT_set_origin.bl_idname, text="Tip")
        op.mode = "TIP"
        op = row.operator(HMT_OT_set_origin.bl_idname, text="Center")
        op.mode = "CENTER"

        curve_box.label(text="3D Cursor To")
        row = curve_box.row(align=True)
        op = row.operator(HMT_OT_snap_cursor.bl_idname, text="Root")
        op.mode = "ROOT"
        op = row.operator(HMT_OT_snap_cursor.bl_idname, text="Tip")
        op.mode = "TIP"
        op = row.operator(HMT_OT_snap_cursor.bl_idname, text="Center")
        op.mode = "CENTER"

        smooth_box = layout.box()
        smooth_box.label(text="Smooth / Reset", icon="MOD_SMOOTH")
        smooth_box.prop(settings, "smooth_factor", slider=True)
        smooth_box.prop(settings, "smooth_steps")

        row = smooth_box.row(align=True)
        row.operator(HMT_OT_smooth_scale.bl_idname)
        row.operator(HMT_OT_smooth_curve.bl_idname)
        row.operator(HMT_OT_smooth_twist.bl_idname)

        row = smooth_box.row(align=True)
        row.operator(HMT_OT_reset_scale.bl_idname)
        row.operator(HMT_OT_reset_path.bl_idname, text="Reset Curve")
        row.operator(HMT_OT_reset_twist.bl_idname)

        row = smooth_box.row(align=True)
        row.operator(HMT_OT_lock_twist.bl_idname)
        row.operator(HMT_OT_unlock_twist.bl_idname)

        mirror_box = layout.box()
        mirror_box.label(text="Mirror", icon="MOD_MIRROR")
        mirror_box.operator(HMT_OT_duplicate_mirror_selected_curves.bl_idname)

        rigging_box = layout.box()
        rigging_box.label(text="Rigging", icon="ARMATURE_DATA")
        rigging_box.operator(HMT_OT_generate_bones_from_active_curve.bl_idname, icon="ARMATURE_DATA")


classes = (
    HMT_PG_settings,
    HMT_OT_generate_bones_from_active_curve,
    HMT_OT_reset_path,
    HMT_OT_reset_path_x_axis,
    HMT_OT_switch_direction,
    HMT_OT_set_origin,
    HMT_OT_snap_cursor,
    HMT_OT_smooth_scale,
    HMT_OT_smooth_curve,
    HMT_OT_smooth_twist,
    HMT_OT_reset_scale,
    HMT_OT_reset_twist,
    HMT_OT_lock_twist,
    HMT_OT_unlock_twist,
    HMT_OT_duplicate_mirror_selected_curves,
    HMT_PT_tools,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.hair_modeling_toolkit = PointerProperty(type=HMT_PG_settings)


def unregister():
    del bpy.types.Scene.hair_modeling_toolkit
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
