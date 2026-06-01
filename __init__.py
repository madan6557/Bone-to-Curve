# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Bone to Curve",
    "author": "madan6557",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Bone to Curve",
    "description": "Generate connected bone chains from the active curve control points.",
    "category": "Rigging",
}

import bpy
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


def _point_to_vector(point):
    return Vector((point.co[0], point.co[1], point.co[2]))


def _spline_world_points(curve_obj, spline):
    matrix_world = curve_obj.matrix_world

    if spline.type == "BEZIER":
        return [matrix_world @ point.co for point in spline.bezier_points]

    if spline.type in {"POLY", "NURBS"}:
        return [matrix_world @ _point_to_vector(point) for point in spline.points]

    return []


def _has_valid_segment(points):
    return any((points[index + 1] - points[index]).length > MIN_BONE_LENGTH for index in range(len(points) - 1))


def _collect_valid_chains(curve_obj):
    chains = []
    skipped_splines = 0

    for spline in curve_obj.data.splines:
        points = _spline_world_points(curve_obj, spline)
        if len(points) < 2:
            skipped_splines += 1
            continue

        if not _has_valid_segment(points):
            skipped_splines += 1
            continue

        chains.append(points)

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
            chains, skipped_splines = _collect_valid_chains(curve_obj)
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
