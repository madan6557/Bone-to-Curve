# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Bone to Curve",
    "author": "madan6557",
    "version": (1, 0, 3),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Bone to Curve",
    "description": "Generate connected bone chains along the active curve path.",
    "category": "Rigging",
}

import bpy


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


def _control_point_count(spline):
    if spline.type == "BEZIER":
        return len(spline.bezier_points)
    if spline.type in {"POLY", "NURBS"}:
        return len(spline.points)
    return 0


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


class BONE_TO_CURVE_OT_generate_from_active_curve(bpy.types.Operator):
    bl_idname = "bone_to_curve.generate_from_active_curve"
    bl_label = "Generate Bones From Active Curve"
    bl_description = "Generate a new armature from the active curve control points"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active_obj = context.view_layer.objects.active
        if active_obj is None:
            cls.poll_message_set("No active object.")
            return False
        if active_obj.type != "CURVE":
            cls.poll_message_set("Active object must be a Curve.")
            return False
        return True

    def execute(self, context):
        curve_obj = context.view_layer.objects.active

        if curve_obj is None:
            self.report({"ERROR"}, "No active object.")
            return {"CANCELLED"}

        if curve_obj.type != "CURVE":
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


class BONE_TO_CURVE_PT_tools(bpy.types.Panel):
    bl_label = "Bone to Curve"
    bl_idname = "BONE_TO_CURVE_PT_tools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Bone to Curve"

    def draw(self, context):
        layout = self.layout
        layout.operator(BONE_TO_CURVE_OT_generate_from_active_curve.bl_idname, icon="ARMATURE_DATA")


classes = (
    BONE_TO_CURVE_OT_generate_from_active_curve,
    BONE_TO_CURVE_PT_tools,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
