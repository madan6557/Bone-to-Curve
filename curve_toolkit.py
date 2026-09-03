# SPDX-FileCopyrightText: 2026 madan6557
# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Curve Toolkit",
    "author": "madan6557",
    "version": (1, 9, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Curve Toolkit",
    "description": "Curve modeling tools and bone chain generation.",
    "category": "Curve",
}

import json
from math import ceil, floor, pi

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty, StringProperty
from mathutils.bvhtree import BVHTree
from mathutils import Quaternion, Vector


MIN_BONE_LENGTH = 1.0e-6
CTK_SEGMENT_PREVIEW_POINT_LIMIT = 512
CTK_PREVIEW_REFRESH_DELAY = 0.15
CTK_ENDPOINT_LOCKS_KEY = "ctk_endpoint_locks"
CTK_TWIST_LOCKS_KEY = "ctk_twist_locks"
CTK_LENGTHS_KEY = "ctk_stored_lengths"
CTK_PREVIEW_KEY = "ctk_preview"
CTK_PREVIEW_KIND_KEY = "ctk_preview_kind"
LEGACY_ENDPOINT_LOCKS_KEY = "hmt_endpoint_locks"
_CURVE_LOCK_HANDLER_RUNNING = False
_RESOLUTION_BATCH_UPDATE_RUNNING = False
_PROFILE_UPDATE_RUNNING = False
_PREVIEW_UPDATE_RUNNING = False
_PREVIEW_HANDLER_RUNNING = False
_PREVIEW_REFRESH_PENDING = False
_PREVIEW_FORCE_PENDING = False


def _remove_handler_by_name(handler_list, handler_name):
    for handler in list(handler_list):
        if getattr(handler, "__name__", "") == handler_name:
            handler_list.remove(handler)


def _remove_ctk_handlers():
    _remove_handler_by_name(bpy.app.handlers.depsgraph_update_post, "_ctk_curve_lock_handler")
    _remove_handler_by_name(bpy.app.handlers.depsgraph_update_post, "_ctk_preview_refresh_handler")
    _remove_handler_by_name(bpy.app.handlers.save_pre, "_ctk_clear_preview_save_handler")
    timer = globals().get("_ctk_preview_refresh_timer")
    if timer is not None and bpy.app.timers.is_registered(timer):
        bpy.app.timers.unregister(timer)


_remove_ctk_handlers()


def _unique_name(base_name, existing_names):
    if base_name not in existing_names:
        return base_name

    index = 1
    while True:
        candidate = f"{base_name}.{index:03d}"
        if candidate not in existing_names:
            return candidate
        index += 1


MIRROR_SIDE_REPLACEMENTS = (
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

MIRROR_LEFT_TOKENS = (".L", "_L", "-L", "Left", "left")
MIRROR_RIGHT_TOKENS = (".R", "_R", "-R", "Right", "right")


def _has_mirror_side_token(name):
    return any(source in name for source, _target in MIRROR_SIDE_REPLACEMENTS)


def _mirror_side(name):
    if any(token in name for token in MIRROR_LEFT_TOKENS):
        return "L"
    if any(token in name for token in MIRROR_RIGHT_TOKENS):
        return "R"
    return None


def _mirror_side_name(name):
    for source, target in MIRROR_SIDE_REPLACEMENTS:
        if source in name:
            return name.replace(source, target)

    return f"{name}.R"


def _ensure_left_side_name(name, existing_names):
    if _has_mirror_side_token(name):
        return name
    return _unique_name(f"{name}.L", existing_names)


def _mirror_selected_curve_collections(context):
    selected_curves = [
        obj
        for obj in context.selected_objects
        if obj.type == "CURVE" and not _is_ctk_preview_object(obj)
    ]
    collections = []
    seen = set()

    for obj in selected_curves:
        for collection in obj.users_collection:
            collection_key = collection.as_pointer()
            if collection_key in seen:
                continue
            collections.append(collection)
            seen.add(collection_key)

    return collections


def _append_unique_collection(collections, seen, collection):
    if collection is None:
        return

    collection_key = collection.as_pointer()
    if collection_key in seen:
        return

    collections.append(collection)
    seen.add(collection_key)


def _mirror_selected_id_collections(selected_ids, collections, seen):
    for selected_id in selected_ids or ():
        if not isinstance(selected_id, bpy.types.Collection):
            continue
        _append_unique_collection(collections, seen, selected_id)


def _mirror_outliner_selected_collections(context, collections, seen):
    screen = getattr(context, "screen", None)
    if screen is None:
        return

    for area in screen.areas:
        if area.type != "OUTLINER":
            continue

        window_region = next((region for region in area.regions if region.type == "WINDOW"), None)
        if window_region is None:
            continue

        try:
            with context.temp_override(area=area, region=window_region):
                selected_ids = getattr(bpy.context, "selected_ids", ()) or ()
        except (AttributeError, TypeError, RuntimeError):
            continue

        _mirror_selected_id_collections(selected_ids, collections, seen)


def _mirror_context_collections(context):
    collections = []
    seen = set()

    _mirror_selected_context_collections(context, collections, seen)

    if collections:
        return collections

    fallback_collection = context.collection
    if fallback_collection is None and context.view_layer.active_layer_collection is not None:
        fallback_collection = context.view_layer.active_layer_collection.collection

    if fallback_collection is not None:
        _append_unique_collection(collections, seen, fallback_collection)

    return collections


def _mirror_selected_context_collections(context, collections=None, seen=None):
    if collections is None:
        collections = []
    if seen is None:
        seen = set()

    try:
        selected_ids = getattr(context, "selected_ids", ()) or ()
    except AttributeError:
        selected_ids = ()

    _mirror_selected_id_collections(selected_ids, collections, seen)
    _mirror_outliner_selected_collections(context, collections, seen)

    return collections


def _mirror_collection_scope(context):
    selected_context_collections = _mirror_selected_context_collections(context)
    if selected_context_collections:
        return [(collection, True) for collection in selected_context_collections]

    selected_curve_collections = _mirror_selected_curve_collections(context)
    if selected_curve_collections:
        return [(collection, False) for collection in selected_curve_collections]

    return [(collection, True) for collection in _mirror_context_collections(context)]


def _mirror_collection_objects(collection, recursive):
    if collection is None:
        return []
    if not recursive:
        return list(collection.objects)

    objects = []
    seen_collections = set()

    def visit(current_collection):
        collection_key = current_collection.as_pointer()
        if collection_key in seen_collections:
            return

        seen_collections.add(collection_key)
        objects.extend(current_collection.objects)
        for child_collection in current_collection.children:
            visit(child_collection)

    visit(collection)
    return objects


def _mirror_collection_curve_objects(context):
    objects = []
    seen = set()
    for collection, recursive in _mirror_collection_scope(context):
        for obj in _mirror_collection_objects(collection, recursive):
            object_key = obj.as_pointer()
            if object_key in seen:
                continue
            if obj.type != "CURVE" or _is_ctk_preview_object(obj):
                continue
            objects.append(obj)
            seen.add(object_key)
    return objects


def _control_point_count(spline):
    if spline.type == "BEZIER":
        return len(spline.bezier_points)
    if spline.type in {"POLY", "NURBS"}:
        return len(spline.points)
    return 0


def _is_ctk_preview_object(obj):
    return obj is not None and bool(obj.get(CTK_PREVIEW_KEY, False))


def _active_curve(context):
    obj = context.view_layer.objects.active
    if obj is None:
        return None
    if _is_ctk_preview_object(obj):
        return None
    if obj.type != "CURVE":
        return None
    return obj


def _active_armature(context):
    obj = context.view_layer.objects.active
    if obj is None:
        return None
    if _is_ctk_preview_object(obj):
        return None
    if obj.type != "ARMATURE":
        return None
    return obj


def _active_or_selected_armature(context):
    active = _active_armature(context)
    if active is not None:
        return active

    for obj in context.selected_objects:
        if _is_ctk_preview_object(obj):
            continue
        if obj.type == "ARMATURE":
            return obj

    return None


def _selected_curve_objects(context):
    return [obj for obj in context.selected_objects if obj.type == "CURVE" and not _is_ctk_preview_object(obj)]


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
    return [obj for obj in context.scene.objects if obj.type == "CURVE" and not _is_ctk_preview_object(obj)]


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

    if preset == "CUSTOM":
        if factor <= 0.5:
            local_factor = factor / 0.5
            return root_radius + (mid_radius - root_radius) * local_factor
        local_factor = (factor - 0.5) / 0.5
        return mid_radius + (tip_radius - mid_radius) * local_factor
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


def _apply_profile_settings_to_curves(context, settings, preset=None):
    curves = _target_curve_objects(context)
    splines = [spline for curve_obj in curves for spline in _editable_splines(curve_obj)]
    selected_mode = _has_selected_points(splines)
    changed_count = 0
    profile_preset = preset if preset is not None else settings.profile_preset

    for spline in splines:
        changed_count += _apply_radius_profile_to_spline(
            spline,
            profile_preset,
            settings.profile_root_radius,
            settings.profile_tip_radius,
            settings.profile_mid_radius,
            selected_mode,
        )

    if changed_count:
        context.view_layer.update()

    return changed_count


def _profile_preset_defaults(preset):
    if preset == "FLAT":
        return 1.0, 1.0, 1.0
    if preset == "ROOT_THICK":
        return 1.0, 0.6, 0.05
    if preset == "TIP_THIN":
        return 0.05, 0.6, 1.0
    if preset == "BOTH_THIN":
        return 0.05, 1.0, 0.05
    if preset == "SHARP_TAPER":
        return 1.0, 0.35, 0.0
    return 1.0, 0.6, 0.05


def _set_profile_numeric_values(settings, root_radius, mid_radius, tip_radius):
    settings.profile_root_radius = root_radius
    settings.profile_mid_radius = mid_radius
    settings.profile_tip_radius = tip_radius


def _update_profile_numeric(settings, context):
    global _PROFILE_UPDATE_RUNNING

    if _PROFILE_UPDATE_RUNNING:
        return
    if context is None or not bool(_target_curve_objects(context)):
        return

    _PROFILE_UPDATE_RUNNING = True
    try:
        _apply_profile_settings_to_curves(context, settings, preset="CUSTOM")
    finally:
        _PROFILE_UPDATE_RUNNING = False


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


def _shortest_arc_angle_delta(start_angle, end_angle):
    diff = (end_angle - start_angle) % (2.0 * pi)
    if diff > pi:
        diff -= 2.0 * pi
    return diff


def _interpolate_tilt(tilt0, tilt1, factor):
    delta = _shortest_arc_angle_delta(tilt0, tilt1)
    return tilt0 + delta * factor


def _eval_cubic_bezier(p0, h0, h1, p1, t):
    om_t = 1.0 - t
    return (
        p0 * (om_t ** 3)
        + h0 * (3.0 * (om_t ** 2) * t)
        + h1 * (3.0 * om_t * (t ** 2))
        + p1 * (t ** 3)
    )


def _eval_cubic_bezier_deriv(p0, h0, h1, p1, t):
    om_t = 1.0 - t
    d0 = (h0 - p0) * 3.0
    d1 = (h1 - h0) * 3.0
    d2 = (p1 - h1) * 3.0
    return d0 * (om_t ** 2) + d1 * (2.0 * om_t * t) + d2 * (t ** 2)


def _split_bezier_segment_de_casteljau(p0, hr0, hl1, p1, cuts, r0, r1, tilt0, tilt1, w0, w1):
    if cuts < 1:
        return [], hr0.copy(), hl1.copy()

    t_vals = [i / (cuts + 1) for i in range(cuts + 2)]
    intermediate_points = []

    d0 = _eval_cubic_bezier_deriv(p0, hr0, hl1, p1, 0.0)
    updated_first_hr = p0 + d0 * (t_vals[1] / 3.0)

    d_end = _eval_cubic_bezier_deriv(p0, hr0, hl1, p1, 1.0)
    updated_last_hl = p1 - d_end * ((1.0 - t_vals[cuts]) / 3.0)

    for i in range(1, cuts + 1):
        t = t_vals[i]
        dt_left = t - t_vals[i - 1]
        dt_right = t_vals[i + 1] - t

        pos = _eval_cubic_bezier(p0, hr0, hl1, p1, t)
        deriv = _eval_cubic_bezier_deriv(p0, hr0, hl1, p1, t)

        h_left = pos - deriv * (dt_left / 3.0)
        h_right = pos + deriv * (dt_right / 3.0)

        radius = r0 + (r1 - r0) * t
        tilt = _interpolate_tilt(tilt0, tilt1, t)
        softbody = w0 + (w1 - w0) * t

        intermediate_points.append({
            "co": pos,
            "handle_left": h_left,
            "handle_right": h_right,
            "handle_left_type": "FREE",
            "handle_right_type": "FREE",
            "tilt": tilt,
            "radius": radius,
            "weight_softbody": softbody,
            "select_control_point": True,
            "select_left_handle": True,
            "select_right_handle": True,
        })

    return intermediate_points, updated_first_hr, updated_last_hl


def _evaluated_spline_path_points(_context, curve_obj, spline, dense_samples_per_segment=24):
    if spline is None:
        return []
    matrix_world = curve_obj.matrix_world if curve_obj is not None else None

    if spline.type == "BEZIER":
        points = spline.bezier_points
        if len(points) < 2:
            return [matrix_world @ p.co if matrix_world else p.co.copy() for p in points]

        resolution = max(4, dense_samples_per_segment)
        sampled_path = []
        seg_count = len(points) - 1

        for i in range(seg_count):
            p0 = points[i].co
            h0 = points[i].handle_right
            h1 = points[i + 1].handle_left
            p1 = points[i + 1].co

            for step in range(resolution):
                t = step / resolution
                pos = _eval_cubic_bezier(p0, h0, h1, p1, t)
                if matrix_world:
                    sampled_path.append(matrix_world @ pos)
                else:
                    sampled_path.append(pos)

        last_p = points[-1].co
        if matrix_world:
            sampled_path.append(matrix_world @ last_p)
        else:
            sampled_path.append(last_p.copy())

        return sampled_path

    elif spline.type == "POLY":
        points = spline.points
        if matrix_world:
            return [matrix_world @ Vector((p.co[0], p.co[1], p.co[2])) for p in points]
        return [Vector((p.co[0], p.co[1], p.co[2])) for p in points]

    elif spline.type == "NURBS":
        point_count = len(spline.points)
        if point_count < 2:
            if matrix_world:
                return [matrix_world @ Vector((p.co[0], p.co[1], p.co[2])) for p in spline.points]
            return [Vector((p.co[0], p.co[1], p.co[2])) for p in spline.points]

        order = max(2, min(int(getattr(spline, "order_u", 2)), point_count))
        degree = order - 1
        use_endpoint = bool(getattr(spline, "use_endpoint_u", False))
        knots = _nurbs_knot_vector(point_count, degree, use_endpoint)
        if knots is None:
            if matrix_world:
                return [matrix_world @ Vector((p.co[0], p.co[1], p.co[2])) for p in spline.points]
            return [Vector((p.co[0], p.co[1], p.co[2])) for p in spline.points]

        local_pts = [Vector((p.co[0], p.co[1], p.co[2])) for p in spline.points]
        weights = [max(MIN_BONE_LENGTH, float(p.co[3])) for p in spline.points]
        sample_count = max(point_count * dense_samples_per_segment, 64)
        sampled_path = []

        for step in range(sample_count):
            u = step / (sample_count - 1)
            basis = _rational_nurbs_basis_values(point_count, degree, knots, weights, u)
            pos = Vector((0.0, 0.0, 0.0))
            for pt, w_basis in zip(local_pts, basis):
                pos += pt * w_basis
            if matrix_world:
                sampled_path.append(matrix_world @ pos)
            else:
                sampled_path.append(pos)

        return sampled_path

    return []


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


def _path_tangent_at_distance(points, distance):
    if len(points) < 2:
        return Vector((1.0, 0.0, 0.0))

    total_length = _polyline_length(points)
    distance = max(0.0, min(total_length, distance))
    walked_distance = 0.0
    last_tangent = None

    for index in range(len(points) - 1):
        segment = points[index + 1] - points[index]
        segment_length = segment.length
        if segment_length <= MIN_BONE_LENGTH:
            continue

        tangent = segment.normalized()
        last_tangent = tangent
        next_distance = walked_distance + segment_length
        if distance <= next_distance:
            return tangent
        walked_distance = next_distance

    return last_tangent if last_tangent is not None else Vector((1.0, 0.0, 0.0))


def _path_cumulative_distances(points):
    cumulative = [0.0]
    for index in range(len(points) - 1):
        cumulative.append(cumulative[-1] + (points[index + 1] - points[index]).length)
    return cumulative


def _path_vertex_curvatures(path_points):
    curvatures = [0.0] * len(path_points)

    for index in range(1, len(path_points) - 1):
        first = path_points[index] - path_points[index - 1]
        second = path_points[index + 1] - path_points[index]
        if first.length <= MIN_BONE_LENGTH or second.length <= MIN_BONE_LENGTH:
            continue

        local_length = max(MIN_BONE_LENGTH, (first.length + second.length) * 0.5)
        curvatures[index] = first.angle(second, 0.0) / local_length

    if len(path_points) > 2:
        curvatures[0] = curvatures[1]
        curvatures[-1] = curvatures[-2]

    return curvatures


def _path_smoothed_curvatures(path_points):
    curvatures = _path_vertex_curvatures(path_points)
    smoothed = list(curvatures)
    for index in range(1, len(curvatures) - 1):
        smoothed[index] = (
            curvatures[index - 1] * 0.25
            + curvatures[index] * 0.5
            + curvatures[index + 1] * 0.25
        )
    return smoothed


def _resample_path_by_bone_count(path_points, bone_count):
    total_length = _polyline_length(path_points)
    if total_length <= MIN_BONE_LENGTH:
        return []

    return [
        _point_at_distance(path_points, total_length * index / bone_count)
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
    full_selection = (len(indices) == point_count and indices == list(range(point_count)))

    for index in indices:
        if full_selection and index == 0:
            distances.append(0.0)
            continue
        if full_selection and index == point_count - 1:
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


def _sample_angle_by_distance(source_distances, angles, target_distance):
    if not source_distances or not angles:
        return 0.0
    if len(source_distances) == 1:
        return angles[0]

    if target_distance <= source_distances[0]:
        return angles[0]
    if target_distance >= source_distances[-1]:
        return angles[-1]

    for index in range(len(source_distances) - 1):
        start_distance = source_distances[index]
        end_distance = source_distances[index + 1]
        if end_distance - start_distance <= MIN_BONE_LENGTH:
            continue
        if target_distance <= end_distance:
            factor = (target_distance - start_distance) / (end_distance - start_distance)
            delta = _shortest_arc_angle_delta(angles[index], angles[index + 1])
            return angles[index] + delta * factor

    return angles[-1]


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

    cumulative = _path_cumulative_distances(path_points)
    smoothed_curvatures = _path_smoothed_curvatures(path_points)
    max_curvature = max(smoothed_curvatures)
    if max_curvature <= MIN_BONE_LENGTH:
        return [
            start_distance + (end_distance - start_distance) * index / (point_count - 1)
            for index in range(point_count)
        ]

    segment_entries = []
    for index in range(len(path_points) - 1):
        segment_start = cumulative[index]
        segment_end = cumulative[index + 1]
        overlap_start = max(start_distance, segment_start)
        overlap_end = min(end_distance, segment_end)
        if overlap_end - overlap_start <= MIN_BONE_LENGTH:
            continue

        curvature = max(smoothed_curvatures[index], smoothed_curvatures[index + 1])
        segment_entries.append((overlap_start, overlap_end, curvature))

    if not segment_entries:
        return []

    weighted_cumulative = [0.0]
    for overlap_start, overlap_end, curvature in segment_entries:
        normalized_curvature = max(0.0, min(1.0, curvature / max_curvature))
        density = pow(normalized_curvature, 1.35)
        weight = 1.0 + density * bias * 3.0
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


def _nurbs_knot_vector(point_count, degree, use_endpoint):
    if point_count < 2 or degree < 1 or degree >= point_count or not use_endpoint:
        return None

    span_count = point_count - degree
    if span_count < 1:
        return None

    knots = [0.0] * (degree + 1)
    for index in range(1, point_count - degree):
        knots.append(index / span_count)
    knots.extend([1.0] * (degree + 1))
    return knots


def _bspline_basis_values(point_count, degree, knots, parameter):
    parameter = max(0.0, min(1.0, float(parameter)))
    if parameter >= 1.0:
        values = [0.0] * point_count
        values[-1] = 1.0
        return values

    values = [0.0] * point_count
    for index in range(point_count):
        if knots[index] <= parameter < knots[index + 1]:
            values[index] = 1.0

    for current_degree in range(1, degree + 1):
        next_values = [0.0] * point_count
        for index in range(point_count):
            left = 0.0
            left_denominator = knots[index + current_degree] - knots[index]
            if left_denominator > MIN_BONE_LENGTH:
                left = (parameter - knots[index]) / left_denominator * values[index]

            right = 0.0
            if index + 1 < point_count:
                right_denominator = knots[index + current_degree + 1] - knots[index + 1]
                if right_denominator > MIN_BONE_LENGTH:
                    right = (knots[index + current_degree + 1] - parameter) / right_denominator * values[index + 1]

            next_values[index] = left + right
        values = next_values

    return values


def _rational_nurbs_basis_values(point_count, degree, knots, weights, parameter):
    values = _bspline_basis_values(point_count, degree, knots, parameter)
    weighted = [value * weight for value, weight in zip(values, weights)]
    total = sum(weighted)
    if total <= MIN_BONE_LENGTH:
        return values
    return [value / total for value in weighted]


def _nurbs_knot_span(knots, point_count, degree, parameter):
    n = point_count - 1
    if parameter >= knots[n + 1]:
        return n
    if parameter <= knots[degree]:
        return degree

    low = degree
    high = n + 1
    middle = (low + high) // 2
    while parameter < knots[middle] or parameter >= knots[middle + 1]:
        if parameter < knots[middle]:
            high = middle
        else:
            low = middle
        middle = (low + high) // 2
    return middle


def _knot_multiplicity(knots, parameter, tolerance=1.0e-7):
    return sum(1 for knot in knots if abs(knot - parameter) <= tolerance)


def _nurbs_state_from_point(point):
    weight = max(MIN_BONE_LENGTH, float(point.co[3]))
    return {
        "homogeneous": Vector((point.co[0] * weight, point.co[1] * weight, point.co[2] * weight, weight)),
        "radius": _point_radius(point),
        "tilt": _point_tilt(point),
        "weight_softbody": getattr(point, "weight_softbody", 0.0),
        "select": bool(point.select),
    }


def _blend_nurbs_states(first, second, alpha):
    return {
        "homogeneous": first["homogeneous"] * (1.0 - alpha) + second["homogeneous"] * alpha,
        "radius": first["radius"] * (1.0 - alpha) + second["radius"] * alpha,
        "tilt": first["tilt"] * (1.0 - alpha) + second["tilt"] * alpha,
        "weight_softbody": first["weight_softbody"] * (1.0 - alpha) + second["weight_softbody"] * alpha,
        "select": first["select"] or second["select"],
    }


def _insert_nurbs_knot_once(states, knots, degree, parameter):
    point_count = len(states)
    span_index = _nurbs_knot_span(knots, point_count, degree, parameter)
    multiplicity = _knot_multiplicity(knots, parameter)
    if multiplicity >= degree:
        return states, knots, False

    new_states = [None] * (point_count + 1)
    for index in range(0, span_index - degree + 1):
        new_states[index] = states[index]
    for index in range(span_index - multiplicity, point_count):
        new_states[index + 1] = states[index]
    for index in range(span_index - degree + 1, span_index - multiplicity + 1):
        denominator = knots[index + degree] - knots[index]
        alpha = 0.0 if abs(denominator) <= MIN_BONE_LENGTH else (parameter - knots[index]) / denominator
        new_states[index] = _blend_nurbs_states(states[index - 1], states[index], max(0.0, min(1.0, alpha)))

    new_knots = knots[:span_index + 1] + [parameter] + knots[span_index + 1:]
    return new_states, new_knots, True


def _uniform_refinement_knots(current_span_count, target_span_count):
    missing_knots = []
    for knot_index in range(1, target_span_count):
        if (knot_index * current_span_count) % target_span_count == 0:
            continue
        missing_knots.append(knot_index / target_span_count)
    return missing_knots


def _set_nurbs_spline_states(spline, states):
    points = spline.points
    if len(states) > len(points):
        points.add(len(states) - len(points))

    for point, state in zip(points, states):
        homogeneous = state["homogeneous"]
        weight = max(MIN_BONE_LENGTH, homogeneous[3])
        point.co = (homogeneous[0] / weight, homogeneous[1] / weight, homogeneous[2] / weight, weight)
        _set_point_radius(point, state["radius"])
        _set_point_tilt(point, state["tilt"])
        if hasattr(point, "weight_softbody"):
            point.weight_softbody = state["weight_softbody"]
        point.select = state["select"]


def _refine_nurbs_spline_uniform(spline, target_span_count, selected=True):
    if spline.type != "NURBS" or _is_closed_spline(spline) or not bool(getattr(spline, "use_endpoint_u", False)):
        return 0

    point_count = _control_point_count(spline)
    order = max(2, min(int(getattr(spline, "order_u", 2)), point_count))
    degree = order - 1
    current_span_count = point_count - degree
    if current_span_count < 1 or target_span_count <= current_span_count:
        return 0
    if target_span_count % current_span_count != 0:
        return 0

    knots = _nurbs_knot_vector(point_count, degree, True)
    if knots is None:
        return 0

    states = [_nurbs_state_from_point(point) for point in spline.points]
    for state in states:
        state["select"] = bool(selected)

    inserted_count = 0
    for parameter in _uniform_refinement_knots(current_span_count, target_span_count):
        states, knots, inserted = _insert_nurbs_knot_once(states, knots, degree, parameter)
        if inserted:
            inserted_count += 1

    if inserted_count == 0:
        return 0

    _set_nurbs_spline_states(spline, states)
    spline.order_u = order
    spline.use_endpoint_u = True
    return inserted_count


def _solve_vector_linear_system(matrix_rows, targets):
    size = len(matrix_rows)
    if size == 0 or len(targets) != size:
        return None

    matrix = [list(row) for row in matrix_rows]
    values = [[target.x, target.y, target.z] for target in targets]

    for pivot_index in range(size):
        pivot_row = max(range(pivot_index, size), key=lambda row_index: abs(matrix[row_index][pivot_index]))
        if abs(matrix[pivot_row][pivot_index]) <= 1.0e-10:
            return None

        if pivot_row != pivot_index:
            matrix[pivot_index], matrix[pivot_row] = matrix[pivot_row], matrix[pivot_index]
            values[pivot_index], values[pivot_row] = values[pivot_row], values[pivot_index]

        pivot = matrix[pivot_index][pivot_index]
        for column in range(pivot_index, size):
            matrix[pivot_index][column] /= pivot
        for axis in range(3):
            values[pivot_index][axis] /= pivot

        for row_index in range(size):
            if row_index == pivot_index:
                continue

            factor = matrix[row_index][pivot_index]
            if abs(factor) <= 1.0e-12:
                continue

            for column in range(pivot_index, size):
                matrix[row_index][column] -= factor * matrix[pivot_index][column]
            for axis in range(3):
                values[row_index][axis] -= factor * values[pivot_index][axis]

    return [Vector(value) for value in values]


def _normalized_parameters_from_distances(distances, total_length):
    if total_length <= MIN_BONE_LENGTH or len(distances) < 2:
        return None

    parameters = [max(0.0, min(1.0, distance / total_length)) for distance in distances]
    parameters[0] = 0.0
    parameters[-1] = 1.0
    return _monotonic_distances(parameters, 1.0)


def _apply_nurbs_interpolated_positions(curve_obj, spline, indices, target_positions, target_parameters=None):
    point_count = _control_point_count(spline)
    if spline.type != "NURBS" or len(indices) != point_count or indices != list(range(point_count)):
        return False
    if point_count > 32:
        return False
    if len(target_positions) != point_count:
        return False

    order = max(2, min(int(getattr(spline, "order_u", 2)), point_count))
    degree = order - 1
    knots = _nurbs_knot_vector(point_count, degree, bool(getattr(spline, "use_endpoint_u", False)))
    if knots is None:
        return False

    points = list(spline.points)
    weights = [max(MIN_BONE_LENGTH, float(point.co[3])) for point in points]
    if target_parameters is None or len(target_parameters) != point_count:
        target_parameters = [index / (point_count - 1) for index in range(point_count)]

    matrix_rows = []
    for parameter in target_parameters:
        matrix_rows.append(_rational_nurbs_basis_values(point_count, degree, knots, weights, parameter))

    solved_positions = _solve_vector_linear_system(matrix_rows, target_positions)
    if solved_positions is None:
        return False

    for point, world_position in zip(points, solved_positions):
        _move_point_to_world(curve_obj, spline, point, world_position)

    return True


def _fit_bezier_handles_to_path(curve_obj, spline, indices, target_distances, path_points):
    if spline.type != "BEZIER" or len(indices) < 2 or len(indices) != len(target_distances):
        return

    points = list(spline.bezier_points)
    selected = set(indices)
    distance_by_index = dict(zip(indices, target_distances))
    matrix_inverted = curve_obj.matrix_world.inverted()

    for index in indices:
        if index < 0 or index >= len(points):
            continue

        point = points[index]
        current_world = curve_obj.matrix_world @ point.co
        tangent = _path_tangent_at_distance(path_points, distance_by_index[index])

        if index - 1 in selected:
            point.handle_left_type = "FREE"
            left_length = max(0.0, distance_by_index[index] - distance_by_index[index - 1]) / 3.0
            point.handle_left = matrix_inverted @ (current_world - tangent * left_length)
        elif index == 0:
            point.handle_left_type = "FREE"
            point.handle_left = point.co

        if index + 1 in selected:
            point.handle_right_type = "FREE"
            right_length = max(0.0, distance_by_index[index + 1] - distance_by_index[index]) / 3.0
            point.handle_right = matrix_inverted @ (current_world + tangent * right_length)
        elif index == len(points) - 1:
            point.handle_right_type = "FREE"
            point.handle_right = point.co


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


def _distribution_path_for_indices(context, curve_obj, spline, indices, mode, path_points=None):
    if path_points is None:
        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        points = list(_spline_points(spline))
        path_points = [_point_world_co(curve_obj, spline, points[index]) for index in indices]

    source_distances = _control_distances_on_path(curve_obj, spline, indices, path_points)
    return path_points, source_distances


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

    path_points, source_distances = _distribution_path_for_indices(context, curve_obj, spline, indices, mode, path_points)
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        return 0

    points = list(_spline_points(spline))
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
    target_positions = [_point_at_distance(path_points, target_distance) for target_distance in target_distances]

    use_nurbs_interpolation = (
        spline.type == "NURBS"
        and mode != "FIT"
        and _runs_cover_full_spline(spline, [indices])
        and _control_point_count(spline) <= 12
    )
    target_parameters = _normalized_parameters_from_distances(target_distances, _polyline_length(path_points))
    if use_nurbs_interpolation and _apply_nurbs_interpolated_positions(curve_obj, spline, indices, target_positions, target_parameters):
        pass
    else:
        for index, target_position in zip(indices, target_positions):
            point = points[index]
            _move_point_to_world(curve_obj, spline, point, target_position)

    _fit_bezier_handles_to_path(curve_obj, spline, indices, target_distances, path_points)

    for index, target_distance in zip(indices, target_distances):
        point = points[index]
        _set_point_radius(point, _sample_scalar_by_distance(source_distances, radii, target_distance))
        _set_point_tilt(point, _sample_angle_by_distance(source_distances, tilts, target_distance))

    if mode == "FIT" and spline.type == "BEZIER":
        _reset_bezier_handles_for_indices(spline, _affected_bezier_indices(len(points), indices))

    return len(indices)


def _segment_distribution_runs(spline, selected_mode):
    point_count = _control_point_count(spline)
    if selected_mode:
        return [run for run in _selected_index_runs(spline) if len(run) >= 2]
    return [list(range(point_count))]


def _apply_subdivide_distribution(context, curve_obj, spline, mode, curvature_bias, path_points):
    if mode == "NONE":
        return 0
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        return 0

    changed_count = 0
    for run in _selected_index_runs(spline):
        if len(run) < 2:
            continue
        changed_count += _apply_segment_distribution(
            context,
            curve_obj,
            spline,
            run,
            mode,
            curvature_bias,
            path_points,
        )

    return changed_count


def _bezier_state_from_point(point):
    return {
        "co": point.co.copy(),
        "handle_left": point.handle_left.copy(),
        "handle_right": point.handle_right.copy(),
        "handle_left_type": point.handle_left_type,
        "handle_right_type": point.handle_right_type,
        "tilt": _point_tilt(point),
        "radius": _point_radius(point),
        "weight_softbody": getattr(point, "weight_softbody", 0.0),
        "select_control_point": bool(point.select_control_point),
        "select_left_handle": bool(point.select_left_handle),
        "select_right_handle": bool(point.select_right_handle),
    }


def _curve_state_from_point(point):
    return {
        "co": point.co.copy(),
        "tilt": _point_tilt(point),
        "radius": _point_radius(point),
        "weight_softbody": getattr(point, "weight_softbody", 0.0),
        "select": bool(point.select),
    }


def _set_bezier_state(point, state):
    point.co = state["co"]
    point.handle_left = state["handle_left"]
    point.handle_right = state["handle_right"]
    point.handle_left_type = state["handle_left_type"]
    point.handle_right_type = state["handle_right_type"]
    _set_point_tilt(point, state["tilt"])
    _set_point_radius(point, state["radius"])
    if hasattr(point, "weight_softbody"):
        point.weight_softbody = state["weight_softbody"]
    point.select_control_point = state["select_control_point"]
    point.select_left_handle = state["select_left_handle"]
    point.select_right_handle = state["select_right_handle"]


def _set_curve_state(point, state):
    point.co = state["co"]
    _set_point_tilt(point, state["tilt"])
    _set_point_radius(point, state["radius"])
    if hasattr(point, "weight_softbody"):
        point.weight_softbody = state["weight_softbody"]
    point.select = state["select"]


def _interpolate_state_value(first, second, factor, key):
    return first[key] + (second[key] - first[key]) * factor


def _subdivide_insert_state(curve_obj, spline, first_state, second_state, factor, target_world):
    local_position = curve_obj.matrix_world.inverted() @ target_world
    radius = _interpolate_state_value(first_state, second_state, factor, "radius")
    tilt = _interpolate_state_value(first_state, second_state, factor, "tilt")
    weight_softbody = _interpolate_state_value(first_state, second_state, factor, "weight_softbody")

    if spline.type == "BEZIER":
        return {
            "co": local_position,
            "handle_left": local_position.copy(),
            "handle_right": local_position.copy(),
            "handle_left_type": "FREE",
            "handle_right_type": "FREE",
            "tilt": tilt,
            "radius": radius,
            "weight_softbody": weight_softbody,
            "select_control_point": True,
            "select_left_handle": True,
            "select_right_handle": True,
        }

    weight = _interpolate_state_value(first_state, second_state, factor, "co")[3]
    return {
        "co": (local_position.x, local_position.y, local_position.z, weight),
        "tilt": tilt,
        "radius": radius,
        "weight_softbody": weight_softbody,
        "select": True,
    }


def _assign_spline_states(spline, states):
    if spline.type == "BEZIER":
        points = spline.bezier_points
        if len(states) > len(points):
            points.add(len(states) - len(points))
        for point, state in zip(points, states):
            _set_bezier_state(point, state)
        return

    points = spline.points
    if len(states) > len(points):
        points.add(len(states) - len(points))
    for point, state in zip(points, states):
        _set_curve_state(point, state)


def _decimate_target_count(point_count, factor, minimum=2):
    if point_count <= minimum:
        return point_count

    target_count = floor(point_count * (1.0 - max(0.0, min(0.95, float(factor)))))
    return max(minimum, min(point_count - 1, target_count))


def _spline_attrs_from_spline(spline):
    attrs = {}
    for attr_name in (
        "resolution_u",
        "order_u",
        "use_endpoint_u",
        "use_bezier_u",
        "use_cyclic_u",
        "use_smooth",
    ):
        if hasattr(spline, attr_name):
            attrs[attr_name] = getattr(spline, attr_name)
    return attrs


def _spline_state_from_spline(spline):
    points = list(_spline_points(spline))
    states = [_bezier_state_from_point(point) for point in points] if spline.type == "BEZIER" else [_curve_state_from_point(point) for point in points]
    return {
        "type": spline.type,
        "attrs": _spline_attrs_from_spline(spline),
        "states": states,
    }


def _create_spline_from_state(curve_data, spline_state):
    states = spline_state["states"]
    if not states:
        return None

    spline = curve_data.splines.new(spline_state["type"])
    if spline.type == "BEZIER":
        spline.bezier_points.add(len(states) - 1)
        for point, state in zip(spline.bezier_points, states):
            _set_bezier_state(point, state)
    else:
        spline.points.add(len(states) - 1)
        for point, state in zip(spline.points, states):
            _set_curve_state(point, state)

    attrs = spline_state["attrs"]
    for attr_name, value in attrs.items():
        if not hasattr(spline, attr_name):
            continue
        if attr_name == "order_u":
            value = max(2, min(int(value), len(states)))
        try:
            setattr(spline, attr_name, value)
        except (TypeError, ValueError):
            pass

    return spline


def _replace_curve_spline_states(curve_data, spline_states):
    for spline in list(curve_data.splines):
        curve_data.splines.remove(spline)

    for spline_state in spline_states:
        _create_spline_from_state(curve_data, spline_state)


def _source_state_interval(source_distances, target_distance):
    if len(source_distances) < 2:
        return 0, 0, 0.0

    if target_distance <= source_distances[0]:
        return 0, 0, 0.0
    if target_distance >= source_distances[-1]:
        last_index = len(source_distances) - 1
        return last_index, last_index, 0.0

    for index in range(len(source_distances) - 1):
        start_distance = source_distances[index]
        end_distance = source_distances[index + 1]
        if target_distance > end_distance:
            continue

        span = end_distance - start_distance
        factor = 0.0 if span <= MIN_BONE_LENGTH else (target_distance - start_distance) / span
        return index, index + 1, max(0.0, min(1.0, factor))

    last_index = len(source_distances) - 1
    return last_index, last_index, 0.0


def _decimate_state_at_distance(curve_obj, spline, states, source_distances, path_points, target_distance):
    first_index, second_index, factor = _source_state_interval(source_distances, target_distance)
    first = states[first_index]
    second = states[second_index]
    target_world = _point_at_distance(path_points, target_distance)
    target_local = curve_obj.matrix_world.inverted() @ target_world
    radius = first["radius"] + (second["radius"] - first["radius"]) * factor
    tilt = _interpolate_tilt(first["tilt"], second["tilt"], factor)
    weight_softbody = first["weight_softbody"] + (second["weight_softbody"] - first["weight_softbody"]) * factor

    if spline.type == "BEZIER":
        return {
            "co": target_local,
            "handle_left": target_local.copy(),
            "handle_right": target_local.copy(),
            "handle_left_type": "FREE",
            "handle_right_type": "FREE",
            "tilt": tilt,
            "radius": radius,
            "weight_softbody": weight_softbody,
            "select_control_point": True,
            "select_left_handle": True,
            "select_right_handle": True,
        }

    first_weight = float(first["co"][3])
    second_weight = float(second["co"][3])
    weight = first_weight + (second_weight - first_weight) * factor
    return {
        "co": (target_local.x, target_local.y, target_local.z, weight),
        "tilt": tilt,
        "radius": radius,
        "weight_softbody": weight_softbody,
        "select": True,
    }


def _fit_bezier_states_to_path(curve_obj, states, target_distances, path_points):
    if len(states) < 2 or len(states) != len(target_distances):
        return

    matrix_inverted = curve_obj.matrix_world.inverted()
    for index, state in enumerate(states):
        current_world = curve_obj.matrix_world @ state["co"]
        tangent = _path_tangent_at_distance(path_points, target_distances[index])

        if index > 0:
            left_length = max(0.0, target_distances[index] - target_distances[index - 1]) / 3.0
            state["handle_left"] = matrix_inverted @ (current_world - tangent * left_length)
        else:
            state["handle_left"] = state["co"].copy()

        if index + 1 < len(states):
            right_length = max(0.0, target_distances[index + 1] - target_distances[index]) / 3.0
            state["handle_right"] = matrix_inverted @ (current_world + tangent * right_length)
        else:
            state["handle_right"] = state["co"].copy()


def _fit_nurbs_states_to_path(curve_obj, states, target_distances, path_points, order, use_endpoint):
    point_count = len(states)
    if point_count < 2 or point_count != len(target_distances):
        return False

    degree = max(1, min(int(order) - 1, point_count - 1))
    knots = _nurbs_knot_vector(point_count, degree, use_endpoint)
    if knots is None:
        return False

    total_length = _polyline_length(path_points)
    target_parameters = _normalized_parameters_from_distances(target_distances, total_length)
    weights = [max(MIN_BONE_LENGTH, float(state["co"][3])) for state in states]
    matrix_rows = [
        _rational_nurbs_basis_values(point_count, degree, knots, weights, parameter)
        for parameter in target_parameters
    ]
    target_positions = [_point_at_distance(path_points, distance) for distance in target_distances]
    solved_positions = _solve_vector_linear_system(matrix_rows, target_positions)
    if solved_positions is None:
        return False

    matrix_inverted = curve_obj.matrix_world.inverted()
    for state, world_position, weight in zip(states, solved_positions, weights):
        local_position = matrix_inverted @ world_position
        state["co"] = (local_position.x, local_position.y, local_position.z, weight)

    return True


def _decimated_run_states(context, curve_obj, spline, run, states, factor, distribution_mode, curvature_bias, path_points):
    minimum_count = 2
    if spline.type == "NURBS":
        minimum_count = min(len(run) - 1, max(2, int(getattr(spline, "order_u", 2))))

    target_count = _decimate_target_count(len(run), factor, minimum=minimum_count)
    if target_count >= len(run):
        return [states[index] for index in run], 0

    path_points, source_distances = _distribution_path_for_indices(context, curve_obj, spline, run, distribution_mode, path_points)
    if len(path_points) < 2 or len(source_distances) < 2 or not _has_valid_segment(path_points):
        return [states[index] for index in run], 0
    full_run = _runs_cover_full_spline(spline, [run])
    if full_run:
        total_length = _polyline_length(path_points)
        source_distances = [
            total_length * index / (len(run) - 1)
            for index in range(len(run))
        ]

    target_distances = _segment_distribution_distances(
        path_points,
        source_distances,
        target_count,
        distribution_mode,
        curvature_bias,
    )
    if len(target_distances) != target_count:
        return [states[index] for index in run], 0

    run_states = [
        _decimate_state_at_distance(
            curve_obj,
            spline,
            [states[index] for index in run],
            source_distances,
            path_points,
            target_distance,
        )
        for target_distance in target_distances
    ]
    if spline.type == "BEZIER":
        _fit_bezier_states_to_path(curve_obj, run_states, target_distances, path_points)
    elif spline.type == "NURBS" and full_run:
        _fit_nurbs_states_to_path(
            curve_obj,
            run_states,
            target_distances,
            path_points,
            getattr(spline, "order_u", 2),
            getattr(spline, "use_endpoint_u", True),
        )

    return run_states, len(run) - target_count


def _decimate_spline_state(context, curve_obj, spline, factor, distribution_mode, curvature_bias, path_points=None):
    spline_state = _spline_state_from_spline(spline)
    valid_runs = [run for run in _selected_index_runs(spline) if len(run) >= 3]
    if not valid_runs:
        return spline_state, 0

    if path_points is None:
        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        path_points = _spline_world_positions(curve_obj, spline)

    states = spline_state["states"]
    run_by_start = {run[0]: run for run in valid_runs}
    new_states = []
    removed_count = 0
    index = 0

    while index < len(states):
        run = run_by_start.get(index)
        if run is None:
            new_states.append(states[index])
            index += 1
            continue

        run_states, run_removed = _decimated_run_states(
            context,
            curve_obj,
            spline,
            run,
            states,
            factor,
            distribution_mode,
            curvature_bias,
            path_points,
        )
        new_states.extend(run_states)
        removed_count += run_removed
        index = run[-1] + 1

    if removed_count:
        spline_state["states"] = new_states
        if spline_state["type"] == "NURBS" and "order_u" in spline_state["attrs"]:
            spline_state["attrs"]["order_u"] = max(2, min(int(spline_state["attrs"]["order_u"]), len(new_states)))

    return spline_state, removed_count


def _decimate_selected_curve_data(context, curve_obj, factor, distribution_mode, curvature_bias):
    spline_states = []
    removed_count = 0

    for spline in list(curve_obj.data.splines):
        if not _is_supported_spline(spline):
            spline_states.append(_spline_state_from_spline(spline))
            continue

        spline_state, spline_removed = _decimate_spline_state(
            context,
            curve_obj,
            spline,
            factor,
            distribution_mode,
            curvature_bias,
        )
        spline_states.append(spline_state)
        removed_count += spline_removed

    if removed_count:
        _replace_curve_spline_states(curve_obj.data, spline_states)

    return removed_count


def _segment_preview_point_count(curve_obj, settings):
    total_count = 0
    for spline in _editable_splines(curve_obj):
        point_count = _control_point_count(spline)
        total_count += point_count
        if settings.segment_preview_operation != "SUBDIVIDE":
            continue

        selected_segments = sum(max(0, len(run) - 1) for run in _selected_index_runs(spline))
        total_count += selected_segments * settings.subdivide_cuts

    return total_count


def _path_distance_for_segment(distances, segment_index, fallback_total, fallback_count):
    if segment_index < len(distances):
        return distances[segment_index]
    return _fallback_node_distance(fallback_total, fallback_count, segment_index)


def _subdivide_selected_spline_data(context, curve_obj, spline, cuts, distribution_mode, curvature_bias, path_points=None):
    cuts = max(1, int(cuts))
    selected_runs = [run for run in _selected_index_runs(spline) if len(run) >= 2]
    if not selected_runs:
        return 0

    if spline.type == "NURBS" and _runs_cover_full_spline(spline, selected_runs):
        point_count = _control_point_count(spline)
        order = max(2, min(int(getattr(spline, "order_u", 2)), point_count))
        current_span_count = point_count - (order - 1)
        inserted_count = _refine_nurbs_spline_uniform(spline, current_span_count * (cuts + 1), selected=True)
        if inserted_count == 0:
            return 0

        if distribution_mode != "NONE":
            run = list(range(_control_point_count(spline)))
            path_points = _evaluated_spline_path_points(context, curve_obj, spline) if path_points is None else path_points
            return inserted_count + _apply_segment_distribution(
                context,
                curve_obj,
                spline,
                run,
                distribution_mode,
                curvature_bias,
                path_points,
            )

        return inserted_count

    if path_points is None:
        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
    if len(path_points) < 2 or not _has_valid_segment(path_points):
        path_points = _spline_world_positions(curve_obj, spline)

    points = list(_spline_points(spline))
    point_count = len(points)
    if point_count < 2:
        return 0

    selected_segments = {index for run in selected_runs for index in range(run[0], run[-1])}
    total_length = _polyline_length(path_points)

    if spline.type == "BEZIER":
        states = [_bezier_state_from_point(point) for point in points]
        old_to_new = {}
        new_states = []

        for index in range(point_count):
            old_to_new[index] = len(new_states)
            new_states.append(states[index])

            if index in selected_segments and index + 1 < point_count:
                p0 = states[index]["co"]
                hr0 = states[index]["handle_right"]
                hl1 = states[index + 1]["handle_left"]
                p1 = states[index + 1]["co"]
                r0 = states[index]["radius"]
                r1 = states[index + 1]["radius"]
                t0 = states[index]["tilt"]
                t1 = states[index + 1]["tilt"]
                w0 = states[index]["weight_softbody"]
                w1 = states[index + 1]["weight_softbody"]

                sub_pts, new_hr0, new_hl1 = _split_bezier_segment_de_casteljau(
                    p0, hr0, hl1, p1, cuts, r0, r1, t0, t1, w0, w1
                )
                new_states[old_to_new[index]]["handle_right"] = new_hr0
                states[index + 1]["handle_left"] = new_hl1
                new_states.extend(sub_pts)

        if len(new_states) == len(states):
            return 0

        _assign_spline_states(spline, new_states)
        expanded_runs = [list(range(old_to_new[run[0]], old_to_new[run[-1]] + 1)) for run in selected_runs]
        changed_count = len(new_states) - len(states)

        if distribution_mode != "NONE":
            for run in expanded_runs:
                changed_count += _apply_segment_distribution(
                    context,
                    curve_obj,
                    spline,
                    run,
                    distribution_mode,
                    curvature_bias,
                    path_points,
                )

        return changed_count

    else:
        states = [_curve_state_from_point(point) for point in points]
        control_distances = _control_distances_on_path(curve_obj, spline, list(range(point_count)), path_points)
        old_to_new = {}
        new_states = []

        for index in range(point_count):
            old_to_new[index] = len(new_states)
            new_states.append(states[index])

            if index not in selected_segments or index + 1 >= point_count:
                continue

            start_distance = _path_distance_for_segment(control_distances, index, total_length, point_count)
            end_distance = _path_distance_for_segment(control_distances, index + 1, total_length, point_count)
            if end_distance - start_distance <= MIN_BONE_LENGTH:
                start_distance = _fallback_node_distance(total_length, point_count, index)
                end_distance = _fallback_node_distance(total_length, point_count, index + 1)

            start_world = curve_obj.matrix_world @ _point_local_co(points[index], spline)
            end_world = curve_obj.matrix_world @ _point_local_co(points[index + 1], spline)

            for cut_index in range(1, cuts + 1):
                factor = cut_index / (cuts + 1)
                target_distance = start_distance + (end_distance - start_distance) * factor
                if len(path_points) >= 2 and _has_valid_segment(path_points):
                    target_world = _point_at_distance(path_points, target_distance)
                else:
                    target_world = start_world.lerp(end_world, factor)
                new_states.append(_subdivide_insert_state(curve_obj, spline, states[index], states[index + 1], factor, target_world))

        if len(new_states) == len(states):
            return 0

        _assign_spline_states(spline, new_states)
        expanded_runs = [list(range(old_to_new[run[0]], old_to_new[run[-1]] + 1)) for run in selected_runs]
        changed_count = len(new_states) - len(states)

        if distribution_mode != "NONE":
            for run in expanded_runs:
                changed_count += _apply_segment_distribution(
                    context,
                    curve_obj,
                    spline,
                    run,
                    distribution_mode,
                    curvature_bias,
                    path_points,
                )

        return changed_count


def _integrated_curvature_scores(path_points, distances):
    if len(path_points) < 3 or len(distances) < 2:
        return []

    cumulative = _path_cumulative_distances(path_points)
    curvatures = _path_smoothed_curvatures(path_points)
    scores = []

    for segment_index in range(len(distances) - 1):
        start_distance = distances[segment_index]
        end_distance = distances[segment_index + 1]
        score = 0.0

        for path_index in range(len(path_points) - 1):
            overlap_start = max(start_distance, cumulative[path_index])
            overlap_end = min(end_distance, cumulative[path_index + 1])
            if overlap_end - overlap_start <= MIN_BONE_LENGTH:
                continue

            curvature = max(curvatures[path_index], curvatures[path_index + 1])
            score += curvature * (overlap_end - overlap_start)

        scores.append(score)

    return scores


def _path_chord_deviation(path_points, start_distance, end_distance):
    if len(path_points) < 3 or end_distance - start_distance <= MIN_BONE_LENGTH:
        return 0.0

    cumulative = _path_cumulative_distances(path_points)
    chord_start = _point_at_distance(path_points, start_distance)
    chord_end = _point_at_distance(path_points, end_distance)
    max_deviation = 0.0

    for index, distance in enumerate(cumulative):
        if distance < start_distance or distance > end_distance:
            continue
        max_deviation = max(max_deviation, _point_segment_distance(path_points[index], chord_start, chord_end))

    return max_deviation


def _auto_subdivide_cut_counts(curve_obj, spline, candidate_segments, path_points, detail_factor):
    detail = max(0.0, min(1.0, float(detail_factor)))
    if detail <= 0.0:
        return {}

    point_count = _control_point_count(spline)
    if point_count < 2 or not candidate_segments:
        return {}

    indices = list(range(point_count))
    distances = _control_distances_on_path(curve_obj, spline, indices, path_points)
    scores = _integrated_curvature_scores(path_points, distances)
    if not scores:
        return {}

    max_score = max(scores)
    deviations = [
        _path_chord_deviation(path_points, distances[index], distances[index + 1])
        for index in range(len(distances) - 1)
    ]
    max_deviation = max(deviations) if deviations else 0.0
    if max_score <= MIN_BONE_LENGTH and max_deviation <= MIN_BONE_LENGTH:
        return {}

    threshold = 0.22
    strength = 5.5
    cut_counts = {}

    for segment_index in sorted(set(candidate_segments)):
        if segment_index < 0 or segment_index >= len(scores):
            continue

        curvature_normalized = scores[segment_index] / max_score if max_score > MIN_BONE_LENGTH else 0.0
        deviation_normalized = deviations[segment_index] / max_deviation if max_deviation > MIN_BONE_LENGTH else 0.0
        normalized = max(curvature_normalized, deviation_normalized)
        if normalized <= threshold:
            continue

        scaled = (normalized - threshold) / (1.0 - threshold) * detail * strength
        cuts = int(scaled + 0.5)
        if cuts > 0:
            cut_counts[segment_index] = min(8, cuts)

    return cut_counts


def _curve_selection_runs_or_full(splines):
    selected_mode = _has_selected_points(splines)
    runs_by_spline = []

    for spline in splines:
        if selected_mode:
            runs = [run for run in _selected_index_runs(spline) if len(run) >= 2]
        else:
            point_count = _control_point_count(spline)
            runs = [list(range(point_count))] if point_count >= 2 else []
        runs_by_spline.append(runs)

    return selected_mode, runs_by_spline


def _runs_cover_full_spline(spline, runs):
    point_count = _control_point_count(spline)
    return len(runs) == 1 and runs[0] == list(range(point_count))


def _auto_nurbs_target_span_count(spline, cut_counts):
    point_count = _control_point_count(spline)
    order = max(2, min(int(getattr(spline, "order_u", 2)), point_count))
    degree = order - 1
    current_span_count = point_count - degree
    if current_span_count < 1:
        return 0

    planned_point_count = point_count + sum(max(0, cuts) for cuts in cut_counts.values())
    planned_span_count = max(current_span_count + 1, planned_point_count - degree)
    refine_factor = max(2, ceil(planned_span_count / current_span_count))
    return current_span_count * refine_factor


def _segments_from_runs(runs):
    segments = []
    for run in runs:
        for index in range(run[0], run[-1]):
            segments.append(index)
    return segments


def _contiguous_segment_runs(segment_indices):
    runs = []
    current = []

    for segment_index in sorted(set(segment_indices)):
        if current and segment_index != current[-1] + 1:
            runs.append(current)
            current = []
        current.append(segment_index)

    if current:
        runs.append(current)

    return runs


def _set_curve_point_selection(curve_obj, selected=False):
    for spline in curve_obj.data.splines:
        if not _is_supported_spline(spline):
            continue

        for point in _spline_points(spline):
            if spline.type == "BEZIER":
                point.select_control_point = selected
                point.select_left_handle = selected
                point.select_right_handle = selected
            else:
                point.select = selected


def _select_spline_index_range(spline, start_index, end_index, selected=True):
    points = list(_spline_points(spline))
    start_index = max(0, min(len(points) - 1, start_index))
    end_index = max(0, min(len(points) - 1, end_index))

    for index in range(min(start_index, end_index), max(start_index, end_index) + 1):
        point = points[index]
        if spline.type == "BEZIER":
            point.select_control_point = selected
            point.select_left_handle = selected
            point.select_right_handle = selected
        else:
            point.select = selected


def _mapped_original_index(original_index, cut_counts):
    return original_index + sum(cuts for segment_index, cuts in cut_counts.items() if segment_index < original_index)


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


def _collection_manager_targets(settings, object_type="ALL"):
    collections = sorted(_resolution_batch_collections(settings), key=lambda collection: collection.name.lower())
    if not collections:
        return []

    target_entries = []
    seen_objects = set()
    for collection in collections:
        for obj in _collection_objects_recursive(collection):
            if object_type != "ALL" and obj.type != object_type:
                continue

            object_key = obj.as_pointer()
            if object_key in seen_objects:
                continue

            seen_objects.add(object_key)
            target_entries.append((collection.name.lower(), obj.name.lower(), obj.name, obj))

    target_entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    return [entry[3] for entry in target_entries]


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

    if _CURVE_LOCK_HANDLER_RUNNING or not _bpy_data_objects_available():
        return

    _CURVE_LOCK_HANDLER_RUNNING = True
    try:
        for obj in bpy.data.objects:
            if _is_ctk_preview_object(obj):
                continue
            if obj.type == "CURVE":
                _apply_endpoint_locks_to_curve(obj)
                _apply_twist_lock_to_curve(obj)
    finally:
        _CURVE_LOCK_HANDLER_RUNNING = False


def _mirror_axis_index(axis):
    return {"X": 0, "Y": 1, "Z": 2}.get(axis, 0)


def _mirror_matrix_world_axis(matrix, axis="X", center=0.0):
    axis_index = _mirror_axis_index(axis)
    mirrored = matrix.copy()
    for column in range(4):
        mirrored[axis_index][column] *= -1.0
    mirrored[axis_index][3] += center * 2.0
    return mirrored


def _duplicate_mirror_curve(context, source_obj, axis="X"):
    source_obj.name = _ensure_left_side_name(source_obj.name, bpy.data.objects.keys())
    source_obj.data.name = _ensure_left_side_name(source_obj.data.name, bpy.data.curves.keys())

    mirrored_name = _unique_name(_mirror_side_name(source_obj.name), bpy.data.objects.keys())
    mirrored_data_name = _unique_name(_mirror_side_name(source_obj.data.name), bpy.data.curves.keys())

    target_data = source_obj.data.copy()
    target_data.name = mirrored_data_name

    target_obj = source_obj.copy()
    target_obj.data = target_data
    target_obj.animation_data_clear()
    target_obj.name = mirrored_name
    target_obj.matrix_world = _mirror_matrix_world_axis(source_obj.matrix_world, axis)

    target_collection = _link_target_collection(context, source_obj)
    target_collection.objects.link(target_obj)

    if _custom_bool(target_obj, "ctk_lock_twist", "hmt_lock_twist"):
        _store_twist_lock_state(target_obj, True)
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


def _clamp_segment_offset(value, segment_count):
    return max(0, min(segment_count, int(value)))


def _custom_segment_range(control_count, fill_mode, start_segment, end_segment):
    if control_count < 2:
        return None

    segment_count = control_count - 1
    if fill_mode == "END_TO_END":
        return 0, segment_count

    start_offset = 0 if start_segment == 0 else _clamp_segment_offset(start_segment, segment_count)
    end_offset = segment_count if end_segment == 0 else _clamp_segment_offset(end_segment, segment_count)
    range_start = min(start_offset, end_offset)
    range_end = max(start_offset, end_offset)

    if range_end - range_start <= 0:
        return None

    if fill_mode == "FROM_TIP":
        return segment_count - range_start, segment_count - range_end

    return range_start, range_end


def _custom_bone_count(requested_bone_count, start_segment, end_segment):
    if requested_bone_count > 0:
        return requested_bone_count
    return max(1, abs(end_segment - start_segment))


def _segment_distance(total_length, segment_count, segment_index):
    if segment_count < 1:
        return 0.0
    return total_length * segment_index / segment_count


def _bone_joints_on_path(path_points, start_distance, end_distance, bone_count, distribution_mode, curvature_bias):
    if bone_count < 1:
        return []

    reverse = start_distance > end_distance
    range_start = min(start_distance, end_distance)
    range_end = max(start_distance, end_distance)

    if distribution_mode == "CURVE":
        distances = _path_weighted_distances(path_points, range_start, range_end, bone_count + 1, curvature_bias)
    else:
        distances = []

    if len(distances) != bone_count + 1:
        distances = [
            range_start + (range_end - range_start) * index / bone_count
            for index in range(bone_count + 1)
        ]

    joints = [_point_at_distance(path_points, distance) for distance in distances]
    if reverse:
        joints.reverse()
    return joints


def _collect_custom_chains(
    context,
    curve_obj,
    bone_count,
    fill_mode,
    start_segment,
    end_segment,
    distribution_mode,
    curvature_bias,
):
    chains = []
    skipped_splines = 0

    for spline in curve_obj.data.splines:
        control_count = _control_point_count(spline)
        segment_range = _custom_segment_range(control_count, fill_mode, start_segment, end_segment)
        if segment_range is None:
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

        segment_count = control_count - 1
        range_start, range_end = segment_range
        resolved_bone_count = _custom_bone_count(bone_count, range_start, range_end)
        start_distance = _segment_distance(total_length, segment_count, range_start)
        end_distance = _segment_distance(total_length, segment_count, range_end)
        joints = _bone_joints_on_path(
            path_points,
            start_distance,
            end_distance,
            resolved_bone_count,
            distribution_mode,
            curvature_bias,
        )
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


def _curve_path_candidates(context, curve_obj):
    candidates = []
    for spline in curve_obj.data.splines:
        if _control_point_count(spline) < 2:
            continue

        path_points = _evaluated_spline_path_points(context, curve_obj, spline)
        if len(path_points) < 2 or not _has_valid_segment(path_points):
            continue

        candidates.append(path_points)

    return candidates


def _path_projection(path_points, world_position):
    distance = _distance_on_path_nearest(path_points, world_position)
    projected = _point_at_distance(path_points, distance)
    return distance, (world_position - projected).length_squared


def _best_path_for_edit_bone_chain(path_candidates, armature_obj, edit_bones, chain):
    if not path_candidates or not chain:
        return None

    first_bone = edit_bones.get(chain[0])
    last_bone = edit_bones.get(chain[-1])
    if first_bone is None or last_bone is None:
        return None

    start_world = armature_obj.matrix_world @ first_bone.head
    end_world = armature_obj.matrix_world @ last_bone.tail
    best = None

    for path_points in path_candidates:
        start_distance, start_score = _path_projection(path_points, start_world)
        end_distance, end_score = _path_projection(path_points, end_world)
        score = start_score + end_score
        if best is None or score < best[0]:
            best = (score, path_points, start_distance, end_distance)

    if best is None:
        return None
    return best[1], best[2], best[3]


def _set_edit_bone_chain_positions(armature_obj, edit_bones, chain, joints):
    if len(joints) != len(chain) + 1:
        return 0

    matrix_world_inverted = armature_obj.matrix_world.inverted()
    local_joints = [matrix_world_inverted @ joint for joint in joints]
    changed_count = 0

    for index, name in enumerate(chain):
        bone = edit_bones.get(name)
        if bone is None:
            continue

        bone.head = local_joints[index]
        bone.tail = local_joints[index + 1]

        if index == 0:
            bone.use_connect = False
        else:
            parent = edit_bones.get(chain[index - 1])
            if parent is not None:
                bone.parent = parent
                bone.head = parent.tail
                bone.use_connect = True

        changed_count += 1

    return changed_count


def _resample_edit_bone_chains_to_curve(
    path_candidates,
    armature_obj,
    edit_bones,
    chains,
    distribution_mode,
    curvature_bias,
):
    changed_count = 0
    skipped_count = 0

    for chain in chains:
        path_data = _best_path_for_edit_bone_chain(path_candidates, armature_obj, edit_bones, chain)
        if path_data is None:
            skipped_count += 1
            continue

        path_points, start_distance, end_distance = path_data
        joints = _bone_joints_on_path(
            path_points,
            start_distance,
            end_distance,
            len(chain),
            distribution_mode,
            curvature_bias,
        )
        if len(joints) != len(chain) + 1:
            skipped_count += 1
            continue

        changed_count += _set_edit_bone_chain_positions(armature_obj, edit_bones, chain, joints)

    return changed_count, skipped_count


def _edit_bone_chains_are_connected(edit_bones, chains):
    for chain in chains:
        for index in range(1, len(chain)):
            parent = edit_bones.get(chain[index - 1])
            bone = edit_bones.get(chain[index])
            if parent is None or bone is None:
                return False
            if bone.parent != parent:
                return False
            if (bone.head - parent.tail).length > MIN_BONE_LENGTH:
                return False

    return True


def _subdivide_edit_bone_chain(edit_bones, chain, cuts):
    if cuts < 1:
        return chain, 0

    expanded_chain = []
    added_count = 0

    for name in chain:
        bone = edit_bones.get(name)
        if bone is None:
            continue

        expanded_chain.append(name)
        for cut_index in range(cuts):
            new_name = _unique_name(f"{name}_sub.{cut_index + 1:03d}", edit_bones.keys())
            new_bone = edit_bones.new(new_name)
            new_bone.head = bone.head.copy()
            new_bone.tail = bone.tail.copy()
            new_bone.roll = bone.roll
            expanded_chain.append(new_bone.name)
            added_count += 1

    for index, name in enumerate(expanded_chain):
        bone = edit_bones.get(name)
        if bone is None:
            continue

        if index == 0:
            bone.use_connect = False
            continue

        parent = edit_bones.get(expanded_chain[index - 1])
        if parent is not None:
            bone.parent = parent
            bone.head = parent.tail
            bone.use_connect = True

    return expanded_chain, added_count


def _spaced_chain_keep_indices(chain_length, target_count):
    if target_count <= 1:
        return [0]

    keep_indices = []
    for index in range(target_count):
        keep_index = round(index * (chain_length - 1) / (target_count - 1))
        if keep_index not in keep_indices:
            keep_indices.append(keep_index)

    candidate = 0
    while len(keep_indices) < target_count and candidate < chain_length:
        if candidate not in keep_indices:
            keep_indices.append(candidate)
        candidate += 1

    return sorted(keep_indices[:target_count])


def _decimate_edit_bone_chain(edit_bones, chain, factor):
    if len(chain) < 2:
        return chain, 0

    target_count = _decimate_target_count(len(chain), factor, minimum=1)
    if target_count >= len(chain):
        return chain, 0

    keep_indices = set(_spaced_chain_keep_indices(len(chain), target_count))
    kept_chain = [name for index, name in enumerate(chain) if index in keep_indices and edit_bones.get(name) is not None]
    remove_names = [name for index, name in enumerate(chain) if index not in keep_indices and edit_bones.get(name) is not None]

    for name in remove_names:
        bone = edit_bones.get(name)
        if bone is not None:
            bone.use_connect = False

    for name in remove_names:
        bone = edit_bones.get(name)
        if bone is not None:
            edit_bones.remove(bone)

    for index, name in enumerate(kept_chain):
        bone = edit_bones.get(name)
        if bone is None:
            continue

        if index == 0:
            bone.parent = None
            bone.use_connect = False
            continue

        parent = edit_bones.get(kept_chain[index - 1])
        if parent is not None:
            bone.parent = parent
            bone.head = parent.tail
            bone.use_connect = True

    return kept_chain, len(remove_names)


def _select_edit_bone_chains(edit_bones, chains):
    selected_set = {name for chain in chains for name in chain}
    for bone in edit_bones:
        selected = bone.name in selected_set
        bone.select = selected
        bone.select_head = selected
        bone.select_tail = selected


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


def _capture_view_state(context):
    active_obj = context.view_layer.objects.active
    return active_obj, list(context.selected_objects), active_obj.mode if active_obj is not None else "OBJECT"


def _restore_view_state(context, state):
    active_obj, selected_objects, mode = state
    current_active = context.view_layer.objects.active
    if current_active is not None and current_active.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")

    selected_names = {obj.name for obj in selected_objects if obj.name in bpy.data.objects}
    for obj in context.view_layer.objects:
        try:
            obj.select_set(obj.name in selected_names)
        except RuntimeError:
            pass

    if active_obj is not None and active_obj.name in bpy.data.objects:
        context.view_layer.objects.active = active_obj
        if mode != "OBJECT" and active_obj.mode != mode and mode in {"EDIT", "POSE"}:
            try:
                bpy.ops.object.mode_set(mode=mode)
            except RuntimeError:
                pass


def _bpy_data_objects_available():
    return hasattr(bpy.data, "objects")


def _clear_ctk_previews(kind=None):
    if not _bpy_data_objects_available():
        return

    for obj in list(bpy.data.objects):
        if not bool(obj.get(CTK_PREVIEW_KEY, False)):
            continue
        if kind is not None and obj.get(CTK_PREVIEW_KIND_KEY, "") != kind:
            continue

        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is None or data.users > 0:
            continue
        if isinstance(data, bpy.types.Curve):
            bpy.data.curves.remove(data, do_unlink=True)
        elif isinstance(data, bpy.types.Armature):
            bpy.data.armatures.remove(data, do_unlink=True)


def _has_ctk_preview(kind=None):
    if not _bpy_data_objects_available():
        return False

    return any(
        bool(obj.get(CTK_PREVIEW_KEY, False))
        and (kind is None or obj.get(CTK_PREVIEW_KIND_KEY, "") == kind)
        for obj in bpy.data.objects
    )


def _preview_collection(context, source_obj):
    if source_obj is not None and source_obj.users_collection:
        return source_obj.users_collection[0]
    return context.collection


def _create_preview_object(context, source_obj, data_copy, kind):
    preview_name = _unique_name(f"CTK Preview {kind.title()} {source_obj.name}", bpy.data.objects.keys())
    preview_obj = bpy.data.objects.new(preview_name, data_copy)
    preview_obj.matrix_world = source_obj.matrix_world.copy()
    preview_obj[CTK_PREVIEW_KEY] = True
    preview_obj[CTK_PREVIEW_KIND_KEY] = kind
    preview_obj.hide_render = True
    preview_obj.hide_select = False
    preview_obj.show_in_front = True
    preview_obj.display_type = "WIRE"
    preview_obj.color = (0.2, 0.7, 1.0, 0.45)
    _preview_collection(context, source_obj).objects.link(preview_obj)
    return preview_obj


def _finish_preview_object(preview_obj):
    preview_obj.hide_select = True
    preview_obj.select_set(False)


def _selected_point_signature(curve_obj):
    data = []
    for spline in curve_obj.data.splines:
        if not _is_supported_spline(spline):
            continue

        points = list(_spline_points(spline))
        selected_indices = [index for index, point in enumerate(points) if _point_is_selected(point, spline)]
        selected_positions = [
            tuple(round(value, 6) for value in _point_local_co(points[index], spline))
            for index in selected_indices
        ]
        data.append((spline.type, len(points), selected_indices, selected_positions))

    return data


def _curve_shape_signature(curve_obj):
    data = []
    for spline in curve_obj.data.splines:
        if not _is_supported_spline(spline):
            continue

        points = list(_spline_points(spline))
        positions = [tuple(round(value, 6) for value in _point_local_co(point, spline)) for point in points]
        data.append((spline.type, len(points), positions))

    matrix = tuple(round(value, 6) for row in curve_obj.matrix_world for value in row)
    return curve_obj.name, matrix, data


def _selected_bone_signature(armature_obj):
    selected_names = _selected_bone_names(armature_obj)
    data = []

    for name in selected_names:
        bone = armature_obj.data.bones.get(name)
        if bone is None:
            continue

        head = armature_obj.matrix_world @ bone.head_local
        tail = armature_obj.matrix_world @ bone.tail_local
        data.append(
            (
                name,
                tuple(round(value, 6) for value in head),
                tuple(round(value, 6) for value in tail),
            )
        )

    return data


def _preview_signature(payload):
    return json.dumps(payload, sort_keys=True)


def _segment_preview_signature(context, settings):
    curve_obj = _active_curve(context)
    if curve_obj is None:
        return _preview_signature({"kind": "SEGMENT", "missing": True})

    return _preview_signature(
        {
            "kind": "SEGMENT",
            "object": curve_obj.name,
            "operation": settings.segment_preview_operation,
            "distribution": settings.distribution_mode,
            "sub_distribution": settings.subdivide_distribution,
            "decimate_distribution": settings.decimate_distribution_mode,
            "bias": round(settings.curvature_bias, 6),
            "cuts": settings.subdivide_cuts,
            "decimate_factor": round(settings.decimate_factor, 6),
            "selection": _selected_point_signature(curve_obj),
        }
    )


def _bone_preview_signature(context, settings):
    curve_obj = _active_curve(context)
    armature_obj = _active_or_selected_armature(context)
    if curve_obj is None or armature_obj is None:
        return _preview_signature({"kind": "BONE", "missing": True})

    return _preview_signature(
        {
            "kind": "BONE",
            "curve": _curve_shape_signature(curve_obj),
            "armature": armature_obj.name,
            "operation": settings.bone_preview_operation,
            "distribution": settings.rig_distribution_mode,
            "bias": round(settings.rig_curvature_bias, 6),
            "cuts": settings.rig_subdivide_cuts,
            "decimate_factor": round(settings.rig_decimate_factor, 6),
            "selection": _selected_bone_signature(armature_obj),
        }
    )


def _segment_has_valid_preview_selection(curve_obj):
    splines = _editable_splines(curve_obj)
    return any(len(run) >= 2 for spline in splines for run in _selected_index_runs(spline))


def _preview_segment_distribution(context, preview_obj, settings):
    splines = _editable_splines(preview_obj)
    if not _segment_has_valid_preview_selection(preview_obj):
        return 0

    changed_count = 0
    for spline in splines:
        path_points = _evaluated_spline_path_points(context, preview_obj, spline)
        for run in _segment_distribution_runs(spline, True):
            changed_count += _apply_segment_distribution(
                context,
                preview_obj,
                spline,
                run,
                settings.distribution_mode,
                settings.curvature_bias,
                path_points,
            )

    return changed_count


def _preview_segment_subdivide(context, preview_obj, settings):
    splines = _editable_splines(preview_obj)
    if not _segment_has_valid_preview_selection(preview_obj):
        return 0

    changed_count = 0
    for spline in splines:
        if not any(len(run) >= 2 for run in _selected_index_runs(spline)):
            continue
        path_points = _evaluated_spline_path_points(context, preview_obj, spline)
        changed_count += _subdivide_selected_spline_data(
            context,
            preview_obj,
            spline,
            settings.subdivide_cuts,
            settings.subdivide_distribution,
            settings.curvature_bias,
            path_points,
        )

    return changed_count


def _preview_segment_decimate(context, preview_obj, settings):
    if not any(len(run) >= 3 for spline in _editable_splines(preview_obj) for run in _selected_index_runs(spline)):
        return 0

    return _decimate_selected_curve_data(
        context,
        preview_obj,
        settings.decimate_factor,
        settings.decimate_distribution_mode,
        settings.curvature_bias,
    )


def _build_segment_preview(context, settings):
    curve_obj = _active_curve(context)
    if curve_obj is None or not _segment_has_valid_preview_selection(curve_obj):
        settings.segment_preview_status = ""
        return False

    preview_point_count = _segment_preview_point_count(curve_obj, settings)
    if preview_point_count > CTK_SEGMENT_PREVIEW_POINT_LIMIT:
        settings.segment_preview_status = f"Preview skipped: {preview_point_count} points exceeds {CTK_SEGMENT_PREVIEW_POINT_LIMIT}."
        return False

    settings.segment_preview_status = ""
    preview_obj = _create_preview_object(context, curve_obj, curve_obj.data.copy(), "SEGMENT")
    changed_count = 0

    if settings.segment_preview_operation == "SUBDIVIDE":
        changed_count = _preview_segment_subdivide(context, preview_obj, settings)
    elif settings.segment_preview_operation == "DECIMATE":
        changed_count = _preview_segment_decimate(context, preview_obj, settings)
    else:
        changed_count = _preview_segment_distribution(context, preview_obj, settings)

    if changed_count == 0:
        settings.segment_preview_status = ""
        _clear_ctk_previews("SEGMENT")
        return False

    _finish_preview_object(preview_obj)
    return True


def _preview_bone_distribution(context, preview_obj, selected_names, settings):
    curve_obj = _active_curve(context)
    path_candidates = _curve_path_candidates(context, curve_obj)
    if not path_candidates:
        return 0, 0, []

    state = _capture_view_state(context)
    changed_count = 0
    skipped_count = 0
    chains = []

    try:
        context.view_layer.objects.active = preview_obj
        preview_obj.select_set(True)
        if preview_obj.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = preview_obj.data.edit_bones
        selected_names = [name for name in selected_names if edit_bones.get(name) is not None]
        if not selected_names:
            return 0, 0, []

        chains = _selected_edit_bone_chains(edit_bones, selected_names)
        if chains is None or not _edit_bone_chains_are_connected(edit_bones, chains):
            return 0, 0, []

        changed_count, skipped_count = _resample_edit_bone_chains_to_curve(
            path_candidates,
            preview_obj,
            edit_bones,
            chains,
            settings.rig_distribution_mode,
            settings.rig_curvature_bias,
        )
        _select_edit_bone_chains(edit_bones, chains)
    finally:
        _restore_view_state(context, state)

    return changed_count, skipped_count, chains


def _preview_bone_subdivide(context, preview_obj, selected_names, settings):
    curve_obj = _active_curve(context)
    path_candidates = _curve_path_candidates(context, curve_obj)
    if not path_candidates:
        return 0, 0, []

    state = _capture_view_state(context)
    added_count = 0
    changed_count = 0
    expanded_chains = []

    try:
        context.view_layer.objects.active = preview_obj
        preview_obj.select_set(True)
        if preview_obj.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = preview_obj.data.edit_bones
        selected_names = [name for name in selected_names if edit_bones.get(name) is not None]
        if not selected_names:
            return 0, 0, []

        chains = _selected_edit_bone_chains(edit_bones, selected_names)
        if chains is None or not _edit_bone_chains_are_connected(edit_bones, chains):
            return 0, 0, []

        for chain in chains:
            expanded_chain, chain_added_count = _subdivide_edit_bone_chain(
                edit_bones,
                chain,
                settings.rig_subdivide_cuts,
            )
            if expanded_chain:
                expanded_chains.append(expanded_chain)
                added_count += chain_added_count

        if added_count == 0:
            return 0, 0, []

        changed_count, _skipped_count = _resample_edit_bone_chains_to_curve(
            path_candidates,
            preview_obj,
            edit_bones,
            expanded_chains,
            settings.rig_distribution_mode,
            settings.rig_curvature_bias,
        )
        _select_edit_bone_chains(edit_bones, expanded_chains)
    finally:
        _restore_view_state(context, state)

    return added_count, changed_count, expanded_chains


def _preview_bone_decimate(context, preview_obj, selected_names, settings):
    curve_obj = _active_curve(context)
    path_candidates = _curve_path_candidates(context, curve_obj)
    if not path_candidates:
        return 0, 0, []

    state = _capture_view_state(context)
    removed_count = 0
    changed_count = 0
    decimated_chains = []

    try:
        context.view_layer.objects.active = preview_obj
        preview_obj.select_set(True)
        if preview_obj.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")

        edit_bones = preview_obj.data.edit_bones
        selected_names = [name for name in selected_names if edit_bones.get(name) is not None]
        if not selected_names:
            return 0, 0, []

        chains = _selected_edit_bone_chains(edit_bones, selected_names)
        if chains is None or not _edit_bone_chains_are_connected(edit_bones, chains):
            return 0, 0, []

        for chain in chains:
            decimated_chain, chain_removed_count = _decimate_edit_bone_chain(
                edit_bones,
                chain,
                settings.rig_decimate_factor,
            )
            if chain_removed_count:
                decimated_chains.append(decimated_chain)
                removed_count += chain_removed_count

        if removed_count == 0:
            return 0, 0, []

        changed_count, _skipped_count = _resample_edit_bone_chains_to_curve(
            path_candidates,
            preview_obj,
            edit_bones,
            decimated_chains,
            settings.rig_distribution_mode,
            settings.rig_curvature_bias,
        )
        _select_edit_bone_chains(edit_bones, decimated_chains)
    finally:
        _restore_view_state(context, state)

    return removed_count, changed_count, decimated_chains


def _build_bone_preview(context, settings):
    curve_obj = _active_curve(context)
    armature_obj = _active_or_selected_armature(context)
    if curve_obj is None or armature_obj is None:
        return False

    selected_names = _selected_bone_names(armature_obj)
    if not selected_names:
        return False

    preview_obj = _create_preview_object(context, armature_obj, armature_obj.data.copy(), "BONE")
    success = False

    if settings.bone_preview_operation == "SUBDIVIDE":
        added_count, changed_count, _chains = _preview_bone_subdivide(context, preview_obj, selected_names, settings)
        success = added_count > 0 and changed_count > 0
    elif settings.bone_preview_operation == "DECIMATE":
        removed_count, changed_count, _chains = _preview_bone_decimate(context, preview_obj, selected_names, settings)
        success = removed_count > 0 and changed_count > 0
    else:
        changed_count, _skipped_count, _chains = _preview_bone_distribution(context, preview_obj, selected_names, settings)
        success = changed_count > 0

    if not success:
        _clear_ctk_previews("BONE")
        return False

    _finish_preview_object(preview_obj)
    return True


def _store_current_preview_signature(context, kind):
    settings = getattr(context.scene, "curve_toolkit", None)
    if settings is None:
        return
    if kind == "SEGMENT":
        settings.segment_preview_signature = _segment_preview_signature(context, settings)
    elif kind == "BONE":
        settings.bone_preview_signature = _bone_preview_signature(context, settings)


def _refresh_ctk_previews(context, force=False):
    global _PREVIEW_UPDATE_RUNNING

    if (
        _PREVIEW_UPDATE_RUNNING
        or not _bpy_data_objects_available()
        or context is None
        or getattr(context.scene, "curve_toolkit", None) is None
    ):
        return

    settings = context.scene.curve_toolkit
    _PREVIEW_UPDATE_RUNNING = True
    try:
        if settings.segment_preview_enabled:
            signature = _segment_preview_signature(context, settings)
            if force or signature != settings.segment_preview_signature:
                _clear_ctk_previews("SEGMENT")
                _build_segment_preview(context, settings)
                settings.segment_preview_signature = signature
        else:
            if settings.segment_preview_signature or _has_ctk_preview("SEGMENT"):
                _clear_ctk_previews("SEGMENT")
            settings.segment_preview_signature = ""
            settings.segment_preview_status = ""

        if settings.bone_preview_enabled:
            signature = _bone_preview_signature(context, settings)
            if force or signature != settings.bone_preview_signature:
                _clear_ctk_previews("BONE")
                _build_bone_preview(context, settings)
                settings.bone_preview_signature = signature
        else:
            if settings.bone_preview_signature or _has_ctk_preview("BONE"):
                _clear_ctk_previews("BONE")
            settings.bone_preview_signature = ""
    finally:
        _PREVIEW_UPDATE_RUNNING = False


def _ctk_preview_refresh_timer():
    global _PREVIEW_REFRESH_PENDING
    global _PREVIEW_FORCE_PENDING

    if not _PREVIEW_REFRESH_PENDING:
        return None

    force = _PREVIEW_FORCE_PENDING
    _PREVIEW_REFRESH_PENDING = False
    _PREVIEW_FORCE_PENDING = False
    _refresh_ctk_previews(bpy.context, force=force)
    return None


def _schedule_ctk_preview_refresh(_context=None, force=False):
    global _PREVIEW_REFRESH_PENDING
    global _PREVIEW_FORCE_PENDING

    if not _bpy_data_objects_available():
        return

    _PREVIEW_REFRESH_PENDING = True
    _PREVIEW_FORCE_PENDING = _PREVIEW_FORCE_PENDING or bool(force)
    if not bpy.app.timers.is_registered(_ctk_preview_refresh_timer):
        bpy.app.timers.register(_ctk_preview_refresh_timer, first_interval=CTK_PREVIEW_REFRESH_DELAY)


def _set_segment_preview_operation(settings, operation):
    if settings.segment_preview_operation != operation:
        settings.segment_preview_operation = operation


def _set_bone_preview_operation(settings, operation):
    if settings.bone_preview_operation != operation:
        settings.bone_preview_operation = operation


def _update_segment_preview_enabled(settings, context):
    if not settings.segment_preview_enabled:
        _clear_ctk_previews("SEGMENT")
        settings.segment_preview_signature = ""
        settings.segment_preview_status = ""
        return
    _schedule_ctk_preview_refresh(context, force=True)


def _update_bone_preview_enabled(settings, context):
    if not settings.bone_preview_enabled:
        _clear_ctk_previews("BONE")
        settings.bone_preview_signature = ""
        return
    _schedule_ctk_preview_refresh(context, force=True)


def _update_segment_distribution_preview(settings, context):
    _set_segment_preview_operation(settings, "DISTRIBUTE")
    _schedule_ctk_preview_refresh(context)


def _update_segment_subdivide_preview(settings, context):
    _set_segment_preview_operation(settings, "SUBDIVIDE")
    _schedule_ctk_preview_refresh(context)


def _update_segment_decimate_preview(settings, context):
    _set_segment_preview_operation(settings, "DECIMATE")
    _schedule_ctk_preview_refresh(context)


def _update_preview_settings(settings, context):
    _schedule_ctk_preview_refresh(context)


def _update_bone_distribution_preview(settings, context):
    _set_bone_preview_operation(settings, "DISTRIBUTE")
    _schedule_ctk_preview_refresh(context)


def _update_bone_subdivide_preview(settings, context):
    _set_bone_preview_operation(settings, "SUBDIVIDE")
    _schedule_ctk_preview_refresh(context)


def _update_bone_decimate_preview(settings, context):
    _set_bone_preview_operation(settings, "DECIMATE")
    _schedule_ctk_preview_refresh(context)


@persistent
def _ctk_preview_refresh_handler(_scene, _depsgraph):
    global _PREVIEW_HANDLER_RUNNING

    if _PREVIEW_HANDLER_RUNNING or not _bpy_data_objects_available():
        return

    _PREVIEW_HANDLER_RUNNING = True
    try:
        _schedule_ctk_preview_refresh(bpy.context)
    finally:
        _PREVIEW_HANDLER_RUNNING = False


@persistent
def _ctk_clear_preview_save_handler(_dummy):
    if not _bpy_data_objects_available():
        return

    _clear_ctk_previews()


class CTK_PG_resolution_collection_item(bpy.types.PropertyGroup):
    collection: PointerProperty(
        name="Collection",
        description="Collection included in Collection Manager",
        type=bpy.types.Collection,
    )


class CTK_PG_settings(bpy.types.PropertyGroup):
    show_curve_controls: BoolProperty(name="Curve Controls", default=True)
    show_segment_control: BoolProperty(name="Segment Control", default=True)
    show_smooth_reset: BoolProperty(name="Smooth / Reset", default=True)
    show_length_tools: BoolProperty(name="Length Tools", default=True)
    show_surface_tools: BoolProperty(name="Surface Tools", default=True)
    show_locks: BoolProperty(name="Locks", default=True)
    show_profile_tools: BoolProperty(name="Profile / Taper", default=True)
    show_bevel_manager: BoolProperty(name="Bevel Manager", default=True)
    show_caps: BoolProperty(name="Caps", default=True)
    show_resolution_batch: BoolProperty(name="Collection Manager", default=True)
    show_lod_tools: BoolProperty(name="LOD Tools", default=True)
    show_selection_tools: BoolProperty(name="Selection Tools", default=True)
    show_validation_tools: BoolProperty(name="Validation", default=True)
    show_mirror: BoolProperty(name="Mirror", default=True)
    show_convert_tools: BoolProperty(name="Convert / Bridge", default=True)
    show_bone_control: BoolProperty(name="Bone Control", default=True)
    show_rigging: BoolProperty(name="Rigging", default=True)

    mirror_axis: EnumProperty(
        name="Mirror Axis",
        description="Global axis used as the mirror plane center",
        items=(
            ("X", "X", "Mirror across global X center"),
            ("Y", "Y", "Mirror across global Y center"),
            ("Z", "Z", "Mirror across global Z center"),
        ),
        default="X",
    )

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
        update=_update_preview_settings,
    )

    distribution_mode: EnumProperty(
        name="Distribution",
        description="Distribution mode for Apply Distribution",
        items=(
            ("EVEN", "Evenly", "Space points evenly along the visual path"),
            ("CURVE", "Curve", "Space points with extra density in curved areas"),
        ),
        default="EVEN",
        update=_update_segment_distribution_preview,
    )

    subdivide_distribution: EnumProperty(
        name="Sub Distribution",
        description="Optional distribution applied after conventional subdivision",
        items=(
            ("NONE", "None", "Insert cuts without redistributing points"),
            ("EVEN", "Evenly", "Redistribute subdivided points evenly along the visual path"),
            ("CURVE", "Curve", "Redistribute subdivided points with extra density in curved areas"),
        ),
        default="NONE",
        update=_update_segment_subdivide_preview,
    )

    subdivide_cuts: IntProperty(
        name="Subdivide Cuts",
        description="Number of new points inserted between each selected segment",
        default=1,
        min=1,
        max=16,
        update=_update_segment_subdivide_preview,
    )

    decimate_factor: FloatProperty(
        name="Reduction Factor",
        description="Fraction of selected curve points removed by Decimate Selected",
        default=0.5,
        min=0.05,
        max=0.95,
        subtype="FACTOR",
        update=_update_segment_decimate_preview,
    )

    decimate_distribution_mode: EnumProperty(
        name="Distribution",
        description="Distribution mode used after curve point decimation",
        items=(
            ("EVEN", "Evenly", "Space remaining points evenly along the visual path"),
            ("CURVE", "Curve", "Keep extra point density in curved areas"),
        ),
        default="EVEN",
        update=_update_segment_decimate_preview,
    )

    segment_preview_enabled: BoolProperty(
        name="Preview",
        description="Show a non-destructive Segment Control ghost preview for selected points",
        default=False,
        update=_update_segment_preview_enabled,
    )

    segment_preview_operation: StringProperty(
        name="Segment Preview Operation",
        description="Internal Segment Control preview operation",
        default="DISTRIBUTE",
        options={"HIDDEN"},
    )

    segment_preview_signature: StringProperty(
        name="Segment Preview Signature",
        description="Internal Segment Control preview cache key",
        default="",
        options={"HIDDEN"},
    )

    segment_preview_status: StringProperty(
        name="Preview Status",
        description="Segment Control preview status",
        default="",
        options={"HIDDEN"},
    )

    auto_subdivide_factor: FloatProperty(
        name="Auto Detail",
        description="How aggressively Auto Subdivide adds cuts in curved areas",
        default=0.5,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
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
        update=_update_profile_numeric,
    )

    profile_mid_radius: FloatProperty(
        name="Middle",
        description="Middle radius used by profile presets",
        default=0.6,
        min=0.0,
        precision=4,
        update=_update_profile_numeric,
    )

    profile_tip_radius: FloatProperty(
        name="Tip",
        description="Tip radius used by profile presets",
        default=0.05,
        min=0.0,
        precision=4,
        update=_update_profile_numeric,
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
        description="Collection to add to Collection Manager",
        type=bpy.types.Collection,
    )

    resolution_collections: CollectionProperty(
        name="Collections",
        description="Collections included in Collection Manager",
        type=CTK_PG_resolution_collection_item,
    )

    collection_rename_prefix: StringProperty(
        name="Prefix",
        description="Text added before the generated object name",
        default="",
    )

    collection_rename_base_name: StringProperty(
        name="Base Name",
        description="Base text used for batch object renaming",
        default="Object_",
    )

    collection_rename_suffix: StringProperty(
        name="Suffix",
        description="Text added after the generated object number",
        default="",
    )

    collection_rename_start: IntProperty(
        name="Start Number",
        description="First sequence number used for batch object renaming",
        default=1,
        min=0,
        max=999999,
    )

    collection_rename_padding: IntProperty(
        name="Padding",
        description="Digit padding used for batch object renaming",
        default=3,
        min=1,
        max=8,
    )

    collection_rename_object_type: EnumProperty(
        name="Object Type",
        description="Object type filter used by Collection Control batch rename",
        items=(
            ("ALL", "All", "Rename all object types"),
            ("CURVE", "Curve", "Rename curve objects only"),
            ("MESH", "Mesh", "Rename mesh objects only"),
            ("ARMATURE", "Armature", "Rename armature objects only"),
        ),
        default="ALL",
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
        description="Number of bones to generate. 0 follows the target segment range",
        default=0,
        min=0,
        max=256,
    )

    rig_fill_mode: EnumProperty(
        name="Fill Mode",
        description="How the custom rigging tool chooses the curve segment",
        items=(
            ("END_TO_END", "End To End", "Generate bones from curve root to tip"),
            ("FROM_ROOT", "From Root", "Read segment range from root toward tip"),
            ("FROM_TIP", "From Tip", "Read segment range from tip toward root"),
        ),
        default="END_TO_END",
    )

    rig_distribution_mode: EnumProperty(
        name="Distribution",
        description="Distribution mode for generated or selected bone chains",
        items=(
            ("EVEN", "Evenly", "Space joints evenly along the visual path"),
            ("CURVE", "Curve", "Space joints with extra density in curved areas"),
        ),
        default="EVEN",
        update=_update_bone_distribution_preview,
    )

    rig_curvature_bias: FloatProperty(
        name="Curvature Bias",
        description="How strongly bone distribution concentrates joints in curved areas",
        default=0.65,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_update_preview_settings,
    )

    rig_subdivide_cuts: IntProperty(
        name="Subdivide Cuts",
        description="Number of new bones inserted into each selected bone",
        default=1,
        min=1,
        max=16,
        update=_update_bone_subdivide_preview,
    )

    rig_decimate_factor: FloatProperty(
        name="Reduction Factor",
        description="Fraction of selected bones removed by Decimate Selected Bones",
        default=0.5,
        min=0.05,
        max=0.95,
        subtype="FACTOR",
        update=_update_bone_decimate_preview,
    )

    bone_preview_enabled: BoolProperty(
        name="Preview",
        description="Show a non-destructive Bone Control ghost preview for selected bone chains",
        default=False,
        update=_update_bone_preview_enabled,
    )

    bone_preview_operation: StringProperty(
        name="Bone Preview Operation",
        description="Internal Bone Control preview operation",
        default="DISTRIBUTE",
        options={"HIDDEN"},
    )

    bone_preview_signature: StringProperty(
        name="Bone Preview Signature",
        description="Internal Bone Control preview cache key",
        default="",
        options={"HIDDEN"},
    )

    rig_start_node: IntProperty(
        name="Start Segment",
        description="Direction-relative start segment boundary. 0 uses the fill start endpoint",
        default=0,
        min=0,
        max=10000,
    )

    rig_end_node: IntProperty(
        name="End Segment",
        description="Direction-relative end segment boundary. 0 uses the fill end endpoint",
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
                settings.rig_distribution_mode,
                settings.rig_curvature_bias,
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


class CTK_OT_apply_bone_distribution(bpy.types.Operator):
    bl_idname = "curve_toolkit.apply_bone_distribution"
    bl_label = "Apply Bone Distribution"
    bl_description = "Resample selected armature bone chains onto the active drawn curve"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None and _active_or_selected_armature(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        armature_obj = _active_or_selected_armature(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}
        if armature_obj is None:
            self.report({"ERROR"}, "Select an armature with selected bones.")
            return {"CANCELLED"}

        selected_names = _selected_bone_names(armature_obj)
        if not selected_names:
            self.report({"ERROR"}, "Select at least one armature bone.")
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        changed_count = 0
        skipped_count = 0

        try:
            _mode_set_object(context)
            path_candidates = _curve_path_candidates(context, curve_obj)
            if not path_candidates:
                self.report({"ERROR"}, "Active curve has no valid drawn path.")
                return {"CANCELLED"}

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
            if not _edit_bone_chains_are_connected(edit_bones, chains):
                self.report({"ERROR"}, "Selected bones must be connected chains.")
                return {"CANCELLED"}

            changed_count, skipped_count = _resample_edit_bone_chains_to_curve(
                path_candidates,
                armature_obj,
                edit_bones,
                chains,
                settings.rig_distribution_mode,
                settings.rig_curvature_bias,
            )
            if changed_count == 0:
                self.report({"ERROR"}, "Selected bones could not be matched to the active curve.")
                return {"CANCELLED"}

            _select_edit_bone_chains(edit_bones, chains)
        finally:
            if context.view_layer.objects.active == armature_obj and armature_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            curve_obj.select_set(True)
            armature_obj.select_set(True)
            context.view_layer.objects.active = curve_obj

        message = f"Updated {changed_count} selected bones."
        if skipped_count:
            message += f" Skipped {skipped_count} chains."
        _clear_ctk_previews("BONE")
        _store_current_preview_signature(context, "BONE")
        self.report({"INFO"}, message)
        return {"FINISHED"}


class CTK_OT_subdivide_selected_bones(bpy.types.Operator):
    bl_idname = "curve_toolkit.subdivide_selected_bones"
    bl_label = "Subdivide Selected Bones"
    bl_description = "Subdivide selected armature bone chains and resample them onto the active drawn curve"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None and _active_or_selected_armature(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        armature_obj = _active_or_selected_armature(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}
        if armature_obj is None:
            self.report({"ERROR"}, "Select an armature with selected bones.")
            return {"CANCELLED"}

        selected_names = _selected_bone_names(armature_obj)
        if not selected_names:
            self.report({"ERROR"}, "Select at least one armature bone.")
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        added_count = 0
        changed_count = 0
        skipped_count = 0
        expanded_chains = []

        try:
            _mode_set_object(context)
            path_candidates = _curve_path_candidates(context, curve_obj)
            if not path_candidates:
                self.report({"ERROR"}, "Active curve has no valid drawn path.")
                return {"CANCELLED"}

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
            if not _edit_bone_chains_are_connected(edit_bones, chains):
                self.report({"ERROR"}, "Selected bones must be connected chains.")
                return {"CANCELLED"}

            for chain in chains:
                expanded_chain, chain_added_count = _subdivide_edit_bone_chain(
                    edit_bones,
                    chain,
                    settings.rig_subdivide_cuts,
                )
                if len(expanded_chain) < 1:
                    skipped_count += 1
                    continue

                expanded_chains.append(expanded_chain)
                added_count += chain_added_count

            if added_count == 0:
                self.report({"ERROR"}, "No bones were added.")
                return {"CANCELLED"}

            changed_count, resample_skipped = _resample_edit_bone_chains_to_curve(
                path_candidates,
                armature_obj,
                edit_bones,
                expanded_chains,
                settings.rig_distribution_mode,
                settings.rig_curvature_bias,
            )
            skipped_count += resample_skipped
            if changed_count == 0:
                self.report({"ERROR"}, "Subdivided bones could not be matched to the active curve.")
                return {"CANCELLED"}

            _select_edit_bone_chains(edit_bones, expanded_chains)
        finally:
            if context.view_layer.objects.active == armature_obj and armature_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            curve_obj.select_set(True)
            armature_obj.select_set(True)
            context.view_layer.objects.active = curve_obj

        message = f"Added {added_count} bones and updated {changed_count} selected bones."
        if skipped_count:
            message += f" Skipped {skipped_count} chains."
        _clear_ctk_previews("BONE")
        _store_current_preview_signature(context, "BONE")
        self.report({"INFO"}, message)
        return {"FINISHED"}


class CTK_OT_decimate_selected_bones(bpy.types.Operator):
    bl_idname = "curve_toolkit.decimate_selected_bones"
    bl_label = "Decimate Selected Bones"
    bl_description = "Reduce selected armature bone chains and resample them onto the active drawn curve"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None and _active_or_selected_armature(context) is not None

    def execute(self, context):
        curve_obj = _active_curve(context)
        armature_obj = _active_or_selected_armature(context)
        if curve_obj is None:
            self.report({"ERROR"}, "Active object must be a Curve.")
            return {"CANCELLED"}
        if armature_obj is None:
            self.report({"ERROR"}, "Select an armature with selected bones.")
            return {"CANCELLED"}

        selected_names = _selected_bone_names(armature_obj)
        if not selected_names:
            self.report({"ERROR"}, "Select at least two connected armature bones.")
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        removed_count = 0
        changed_count = 0
        skipped_count = 0
        decimated_chains = []

        try:
            _mode_set_object(context)
            path_candidates = _curve_path_candidates(context, curve_obj)
            if not path_candidates:
                self.report({"ERROR"}, "Active curve has no valid drawn path.")
                return {"CANCELLED"}

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
            if not _edit_bone_chains_are_connected(edit_bones, chains):
                self.report({"ERROR"}, "Selected bones must be connected chains.")
                return {"CANCELLED"}
            if not any(len(chain) >= 2 for chain in chains):
                self.report({"ERROR"}, "Select at least two connected armature bones.")
                return {"CANCELLED"}

            for chain in chains:
                decimated_chain, chain_removed_count = _decimate_edit_bone_chain(
                    edit_bones,
                    chain,
                    settings.rig_decimate_factor,
                )
                if chain_removed_count == 0:
                    skipped_count += 1
                    continue

                decimated_chains.append(decimated_chain)
                removed_count += chain_removed_count

            if removed_count == 0:
                self.report({"ERROR"}, "No bones were removed.")
                return {"CANCELLED"}

            changed_count, resample_skipped = _resample_edit_bone_chains_to_curve(
                path_candidates,
                armature_obj,
                edit_bones,
                decimated_chains,
                settings.rig_distribution_mode,
                settings.rig_curvature_bias,
            )
            skipped_count += resample_skipped
            if changed_count == 0:
                self.report({"ERROR"}, "Decimated bones could not be matched to the active curve.")
                return {"CANCELLED"}

            _select_edit_bone_chains(edit_bones, decimated_chains)
        finally:
            if context.view_layer.objects.active == armature_obj and armature_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            curve_obj.select_set(True)
            armature_obj.select_set(True)
            context.view_layer.objects.active = curve_obj

        message = f"Removed {removed_count} bones and updated {changed_count} selected bones."
        if skipped_count:
            message += f" Skipped {skipped_count} chains."
        _clear_ctk_previews("BONE")
        _store_current_preview_signature(context, "BONE")
        self.report({"INFO"}, message)
        return {"FINISHED"}


class CTK_OT_clear_preview(bpy.types.Operator):
    bl_idname = "curve_toolkit.clear_preview"
    bl_label = "Clear Preview"
    bl_description = "Clear Curve Toolkit ghost preview objects"
    bl_options = {"REGISTER", "UNDO"}

    kind: bpy.props.EnumProperty(
        name="Preview",
        items=(
            ("SEGMENT", "Segment", "Clear Segment Control preview"),
            ("BONE", "Bone", "Clear Bone Control preview"),
            ("ALL", "All", "Clear all Curve Toolkit previews"),
        ),
        default="ALL",
    )

    def execute(self, context):
        if self.kind == "ALL":
            _clear_ctk_previews()
            _store_current_preview_signature(context, "SEGMENT")
            _store_current_preview_signature(context, "BONE")
            context.scene.curve_toolkit.segment_preview_status = ""
        else:
            _clear_ctk_previews(self.kind)
            _store_current_preview_signature(context, self.kind)
            if self.kind == "SEGMENT":
                context.scene.curve_toolkit.segment_preview_status = ""

        self.report({"INFO"}, "Cleared preview.")
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


class CTK_OT_segment_quick_subdivide(bpy.types.Operator):
    bl_idname = "curve_toolkit.segment_quick_subdivide"
    bl_label = "Quick Subdivide"
    bl_description = "Subdivide selected curve segments with exact shape preservation"
    bl_options = {"REGISTER", "UNDO"}

    cuts: bpy.props.IntProperty(name="Cuts", default=1, min=1, max=16)

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, _splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        previous_mode = curve_obj.mode
        _set_active_only(context, curve_obj)
        if curve_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        splines = _editable_splines(curve_obj)
        has_valid_segment = any(
            len(run) >= 2 for spline in splines for run in _selected_index_runs(spline)
        )
        if not has_valid_segment:
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, "Select at least 2 adjacent curve points to subdivide.")
            return {"CANCELLED"}

        added_count = 0
        try:
            for spline in splines:
                if not any(len(run) >= 2 for run in _selected_index_runs(spline)):
                    continue
                path_points = _evaluated_spline_path_points(context, curve_obj, spline)
                added_count += _subdivide_selected_spline_data(
                    context,
                    curve_obj,
                    spline,
                    self.cuts,
                    "NONE",
                    settings.curvature_bias,
                    path_points,
                )
        except Exception as exc:
            if curve_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, f"Subdivide failed: {exc}")
            return {"CANCELLED"}

        context.view_layer.update()
        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)

        _clear_ctk_previews("SEGMENT")
        _store_current_preview_signature(context, "SEGMENT")
        self.report({"INFO"}, f"Added {added_count} curve points (+{self.cuts} cut{'s' if self.cuts > 1 else ''} per segment).")
        return {"FINISHED"}


class CTK_OT_select_curve_segment(bpy.types.Operator):
    bl_idname = "curve_toolkit.select_curve_segment"
    bl_label = "Select Segment"
    bl_description = "Select root or tip segment (first 2 or last 2 points) of active curve splines"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        name="Mode",
        items=(
            ("ROOT", "Root Segment", "Select the first 2 points (root segment)"),
            ("TIP", "Tip Segment", "Select the last 2 points (tip segment)"),
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
                point_count = len(points)
                if point_count < 2:
                    continue

                for index, point in enumerate(points):
                    selected = (
                        (self.mode == "ROOT" and index in (0, 1))
                        or (self.mode == "TIP" and index in (point_count - 2, point_count - 1))
                    )
                    if spline.type == "BEZIER":
                        point.select_control_point = selected
                        point.select_left_handle = selected
                        point.select_right_handle = selected
                    else:
                        point.select = selected
                    changed_count += int(selected)

        label = "root" if self.mode == "ROOT" else "tip"
        self.report({"INFO"}, f"Selected {label} segment on {len(_target_curve_objects(context))} curve(s).")
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
        curve_obj, _splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        previous_mode = curve_obj.mode
        _set_active_only(context, curve_obj)
        if curve_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        splines = _editable_splines(curve_obj)
        selected_mode = _has_selected_points(splines)
        if self.mode != "FIT" and not selected_mode:
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, "Select at least 2 contiguous curve points to distribute.")
            return {"CANCELLED"}

        changed_count = 0

        for spline in splines:
            path_points = _evaluated_spline_path_points(context, curve_obj, spline)
            for run in _segment_distribution_runs(spline, selected_mode if self.mode == "FIT" else True):
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
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, "No valid open spline segment could be distributed.")
            return {"CANCELLED"}

        context.view_layer.update()
        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)

        if self.mode != "FIT":
            _clear_ctk_previews("SEGMENT")
            _store_current_preview_signature(context, "SEGMENT")
        label = "fit to visual path" if self.mode == "FIT" else self.mode.lower()
        self.report({"INFO"}, f"Updated {changed_count} curve points with {label}.")
        return {"FINISHED"}


class CTK_OT_segment_subdivide_selected(bpy.types.Operator):
    bl_idname = "curve_toolkit.segment_subdivide_selected"
    bl_label = "Subdivide Selected"
    bl_description = "Insert conventional cuts between selected points with optional distribution"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, _splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        previous_mode = curve_obj.mode
        _set_active_only(context, curve_obj)
        if curve_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        splines = _editable_splines(curve_obj)
        before_counts = [_control_point_count(spline) for spline in splines]
        has_valid_segment = False

        for spline in splines:
            selected_runs = [run for run in _selected_index_runs(spline) if len(run) >= 2]
            if selected_runs:
                has_valid_segment = True

        if not has_valid_segment:
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, "Select at least 2 contiguous curve points to subdivide.")
            return {"CANCELLED"}

        try:
            for spline in splines:
                if not any(len(run) >= 2 for run in _selected_index_runs(spline)):
                    continue
                path_points = _evaluated_spline_path_points(context, curve_obj, spline)
                _subdivide_selected_spline_data(
                    context,
                    curve_obj,
                    spline,
                    settings.subdivide_cuts,
                    settings.subdivide_distribution,
                    settings.curvature_bias,
                    path_points,
                )
        except Exception as exc:
            if curve_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, f"Subdivide failed: {exc}")
            return {"CANCELLED"}

        context.view_layer.update()
        added_count = sum(
            max(0, _control_point_count(curve_obj.data.splines[spline_index]) - before_count)
            for spline_index, before_count in enumerate(before_counts)
            if spline_index < len(curve_obj.data.splines)
        )

        if added_count == 0:
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, "No curve points were added.")
            return {"CANCELLED"}

        context.view_layer.update()
        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)

        _clear_ctk_previews("SEGMENT")
        _store_current_preview_signature(context, "SEGMENT")
        self.report({"INFO"}, f"Added {added_count} curve points.")
        return {"FINISHED"}


class CTK_OT_segment_decimate_selected(bpy.types.Operator):
    bl_idname = "curve_toolkit.segment_decimate_selected"
    bl_label = "Decimate Selected"
    bl_description = "Reduce selected curve points while preserving the evaluated visual path"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, _splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        previous_mode = curve_obj.mode
        _set_active_only(context, curve_obj)
        if curve_obj.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        splines = _editable_splines(curve_obj)
        if not any(len(run) >= 3 for spline in splines for run in _selected_index_runs(spline)):
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, "Select at least 3 contiguous curve points to decimate.")
            return {"CANCELLED"}

        try:
            removed_count = _decimate_selected_curve_data(
                context,
                curve_obj,
                settings.decimate_factor,
                settings.decimate_distribution_mode,
                settings.curvature_bias,
            )
        except Exception as exc:
            if curve_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, f"Decimate failed: {exc}")
            return {"CANCELLED"}

        context.view_layer.update()
        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)

        if removed_count == 0:
            self.report({"ERROR"}, "No curve points were removed.")
            return {"CANCELLED"}

        _clear_ctk_previews("SEGMENT")
        _store_current_preview_signature(context, "SEGMENT")
        self.report({"INFO"}, f"Removed {removed_count} curve points.")
        return {"FINISHED"}


class CTK_OT_segment_auto_subdivide(bpy.types.Operator):
    bl_idname = "curve_toolkit.segment_auto_subdivide"
    bl_label = "Auto Subdivide"
    bl_description = "Detect curved areas and add only the cuts needed before curve distribution"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _active_curve(context) is not None

    def execute(self, context):
        curve_obj, splines = _require_editable_open_curve(self, context)
        if curve_obj is None:
            return {"CANCELLED"}

        settings = context.scene.curve_toolkit
        selected_mode, runs_by_spline = _curve_selection_runs_or_full(splines)
        path_snapshots = {}
        cuts_by_spline = {}

        for spline_index, spline in enumerate(splines):
            runs = runs_by_spline[spline_index]
            candidate_segments = _segments_from_runs(runs)
            if not candidate_segments:
                continue

            path_points = _evaluated_spline_path_points(context, curve_obj, spline)
            if len(path_points) < 2 or not _has_valid_segment(path_points):
                continue

            cut_counts = _auto_subdivide_cut_counts(
                curve_obj,
                spline,
                candidate_segments,
                path_points,
                settings.auto_subdivide_factor,
            )
            if cut_counts:
                path_snapshots[spline_index] = path_points
                cuts_by_spline[spline_index] = cut_counts

        if not cuts_by_spline:
            self.report({"ERROR"}, "Auto Subdivide did not find any segment that needs cuts at this detail value.")
            return {"CANCELLED"}

        previous_mode = curve_obj.mode
        _set_active_only(context, curve_obj)
        added_count = 0
        refined_nurbs_splines = set()

        try:
            if curve_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")

            for spline_index in sorted(cuts_by_spline):
                spline = curve_obj.data.splines[spline_index]
                cut_counts = cuts_by_spline[spline_index]
                runs = runs_by_spline[spline_index]
                if spline.type == "NURBS" and _runs_cover_full_spline(spline, runs):
                    target_span_count = _auto_nurbs_target_span_count(spline, cut_counts)
                    order = max(2, min(int(getattr(spline, "order_u", 2)), _control_point_count(spline)))
                    target_point_count = target_span_count + order - 1
                    if target_point_count <= CTK_SEGMENT_PREVIEW_POINT_LIMIT:
                        before_count = _control_point_count(spline)
                        inserted_count = _refine_nurbs_spline_uniform(spline, target_span_count, selected=True)
                        if inserted_count:
                            added_count += max(0, _control_point_count(spline) - before_count)
                            refined_nurbs_splines.add(spline_index)
                            continue

                for segment_index, cuts in sorted(cuts_by_spline[spline_index].items(), reverse=True):
                    _set_curve_point_selection(curve_obj, selected=False)
                    spline = curve_obj.data.splines[spline_index]
                    if segment_index + 1 >= _control_point_count(spline):
                        continue

                    before_count = _control_point_count(spline)
                    _select_spline_index_range(spline, segment_index, segment_index + 1, selected=True)
                    _subdivide_selected_spline_data(
                        context,
                        curve_obj,
                        spline,
                        cuts,
                        "NONE",
                        settings.curvature_bias,
                        path_snapshots[spline_index],
                    )

                    after_count = _control_point_count(curve_obj.data.splines[spline_index])
                    added_count += max(0, after_count - before_count)

            if added_count == 0:
                if previous_mode != "OBJECT":
                    bpy.ops.object.mode_set(mode=previous_mode)
                self.report({"ERROR"}, "Auto Subdivide could not add cuts.")
                return {"CANCELLED"}

            context.view_layer.update()
            _set_curve_point_selection(curve_obj, selected=False)
            for spline_index in refined_nurbs_splines:
                if spline_index >= len(curve_obj.data.splines):
                    continue
                spline = curve_obj.data.splines[spline_index]
                _select_spline_index_range(spline, 0, _control_point_count(spline) - 1, selected=True)

            for spline_index, path_points in path_snapshots.items():
                if spline_index in refined_nurbs_splines:
                    continue
                if spline_index >= len(curve_obj.data.splines):
                    continue

                spline = curve_obj.data.splines[spline_index]
                cut_counts = cuts_by_spline[spline_index]
                runs = runs_by_spline[spline_index]

                if selected_mode:
                    for run in runs:
                        start_index = _mapped_original_index(run[0], cut_counts)
                        end_index = _mapped_original_index(run[-1], cut_counts)
                        _select_spline_index_range(spline, start_index, end_index, selected=True)
                else:
                    for segment_run in _contiguous_segment_runs(cut_counts.keys()):
                        start_index = _mapped_original_index(segment_run[0], cut_counts)
                        end_index = _mapped_original_index(segment_run[-1] + 1, cut_counts)
                        _select_spline_index_range(spline, start_index, end_index, selected=True)

                _apply_subdivide_distribution(
                    context,
                    curve_obj,
                    spline,
                    "CURVE",
                    settings.curvature_bias,
                    path_points,
                )

            context.view_layer.update()
        except Exception as exc:
            if curve_obj.mode != "OBJECT":
                bpy.ops.object.mode_set(mode="OBJECT")
            if previous_mode != "OBJECT":
                bpy.ops.object.mode_set(mode=previous_mode)
            self.report({"ERROR"}, f"Auto Subdivide failed: {exc}")
            return {"CANCELLED"}

        if previous_mode != "OBJECT":
            bpy.ops.object.mode_set(mode=previous_mode)

        self.report({"INFO"}, f"Auto Subdivide added {added_count} curve points.")
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
    bl_description = "Duplicate selected curves and mirror them across the selected global axis center"
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
        settings = context.scene.curve_toolkit
        mirrored_objects = []
        for source_obj in source_curves:
            mirrored_objects.append(_duplicate_mirror_curve(context, source_obj, settings.mirror_axis))

        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in mirrored_objects:
            obj.select_set(True)

        context.view_layer.objects.active = mirrored_objects[-1]
        context.view_layer.update()
        self.report({"INFO"}, f"Created {len(mirrored_objects)} mirrored curve duplicates across {settings.mirror_axis}.")
        return {"FINISHED"}


class CTK_OT_select_mirror_side(bpy.types.Operator):
    bl_idname = "curve_toolkit.select_mirror_side"
    bl_label = "Select Mirror Side"
    bl_description = "Select left or right side curve objects in the active mirror collection scope"
    bl_options = {"REGISTER", "UNDO"}

    side: EnumProperty(
        name="Side",
        items=(
            ("L", "Left", "Select left-side curve objects"),
            ("R", "Right", "Select right-side curve objects"),
        ),
        default="L",
    )

    @classmethod
    def poll(cls, context):
        return context.collection is not None

    def execute(self, context):
        target_objects = [
            obj
            for obj in _mirror_collection_curve_objects(context)
            if _mirror_side(obj.name) == self.side
        ]
        if not target_objects:
            side_name = "left" if self.side == "L" else "right"
            self.report({"ERROR"}, f"No {side_name}-side curve objects found in the mirror collection scope.")
            return {"CANCELLED"}

        if context.view_layer.objects.active is not None and context.view_layer.objects.active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        for obj in context.view_layer.objects:
            obj.select_set(False)
        for obj in target_objects:
            obj.select_set(True)

        context.view_layer.objects.active = target_objects[-1]
        context.view_layer.update()
        side_name = "left" if self.side == "L" else "right"
        self.report({"INFO"}, f"Selected {len(target_objects)} {side_name}-side curve objects.")
        return {"FINISHED"}


class CTK_OT_remove_mirror_duplicates(bpy.types.Operator):
    bl_idname = "curve_toolkit.remove_mirror_duplicates"
    bl_label = "Remove Duplicate"
    bl_description = "Remove opposite-side mirrored curve duplicates for selected curve objects"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(
            obj.type == "CURVE" and not _is_ctk_preview_object(obj)
            for obj in context.selected_objects
        )

    def execute(self, context):
        selected_curves = [
            obj
            for obj in context.selected_objects
            if obj.type == "CURVE" and not _is_ctk_preview_object(obj)
        ]
        selected_names = {obj.name for obj in selected_curves}
        scoped_objects = {
            obj.name: obj
            for obj in _mirror_collection_curve_objects(context)
        }
        removable_objects = []
        skipped_selected = 0
        skipped_without_side = 0

        for source_obj in selected_curves:
            if _mirror_side(source_obj.name) is None:
                skipped_without_side += 1
                continue

            counterpart_name = _mirror_side_name(source_obj.name)
            counterpart_obj = scoped_objects.get(counterpart_name)
            if counterpart_obj is None:
                continue
            if counterpart_obj.name in selected_names:
                skipped_selected += 1
                continue
            if counterpart_obj not in removable_objects:
                removable_objects.append(counterpart_obj)

        if not removable_objects:
            message = "No opposite-side mirror duplicates found in the mirror collection scope."
            if skipped_selected:
                message = "Opposite-side duplicates are selected too, so nothing was removed."
            elif skipped_without_side == len(selected_curves):
                message = "Selected curve objects do not have supported mirror side tokens."
            self.report({"ERROR"}, message)
            return {"CANCELLED"}

        if context.view_layer.objects.active is not None and context.view_layer.objects.active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")

        removed_count = 0
        for obj in removable_objects:
            curve_data = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            removed_count += 1
            if curve_data is not None and curve_data.users == 0:
                bpy.data.curves.remove(curve_data)

        context.view_layer.update()
        message = f"Removed {removed_count} mirror duplicate curve objects."
        if skipped_selected:
            message += f" Skipped {skipped_selected} selected counterparts."
        self.report({"INFO"}, message)
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
    bl_description = "Add the selected collection to Collection Manager"
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
        self.report({"INFO"}, f"Added {collection.name} to Collection Manager.")
        return {"FINISHED"}


class CTK_OT_remove_resolution_collection(bpy.types.Operator):
    bl_idname = "curve_toolkit.remove_resolution_collection"
    bl_label = "Remove Collection"
    bl_description = "Remove a collection from Collection Manager"
    bl_options = {"REGISTER", "UNDO"}

    index: IntProperty(name="Index", default=-1)

    def execute(self, context):
        settings = context.scene.curve_toolkit
        if self.index < 0 or self.index >= len(settings.resolution_collections):
            self.report({"ERROR"}, "Invalid Collection Manager collection index.")
            return {"CANCELLED"}

        item = settings.resolution_collections[self.index]
        collection_name = item.collection.name if item.collection is not None else "Missing Collection"
        settings.resolution_collections.remove(self.index)
        self.report({"INFO"}, f"Removed {collection_name} from Collection Manager.")
        return {"FINISHED"}


class CTK_OT_refresh_resolution_batch(bpy.types.Operator):
    bl_idname = "curve_toolkit.refresh_resolution_batch"
    bl_label = "Refresh"
    bl_description = "Refresh Resolution Control target counts"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.curve_toolkit
        collections = _resolution_batch_collections(settings)
        if not collections:
            self.report({"WARNING"}, "Add at least one collection to Collection Manager.")
            return {"FINISHED"}

        path_curves, bevel_references = _resolution_batch_targets_from_collections(collections)
        self.report(
            {"INFO"},
            f"Resolution Control found {len(path_curves)} path curves and {len(bevel_references)} bevel references from {len(collections)} collections.",
        )
        return {"FINISHED"}


class CTK_OT_rename_collection_objects(bpy.types.Operator):
    bl_idname = "curve_toolkit.rename_collection_objects"
    bl_label = "Batch Rename Objects"
    bl_description = "Batch rename objects in Collection Manager collections"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.curve_toolkit
        collections = _resolution_batch_collections(settings)
        if not collections:
            self.report({"ERROR"}, "Add at least one collection to Collection Manager.")
            return {"CANCELLED"}

        targets = _collection_manager_targets(settings, settings.collection_rename_object_type)
        if not targets:
            self.report({"ERROR"}, "No matching objects found in Collection Manager collections.")
            return {"CANCELLED"}

        prefix = settings.collection_rename_prefix
        base_name = settings.collection_rename_base_name
        suffix = settings.collection_rename_suffix
        start_number = settings.collection_rename_start
        padding = max(1, settings.collection_rename_padding)
        temporary_prefix = "__CTK_RENAME__"

        for index, obj in enumerate(targets):
            obj.name = f"{temporary_prefix}{index:06d}"

        for index, obj in enumerate(targets):
            number = str(start_number + index).zfill(padding)
            obj.name = f"{prefix}{base_name}{number}{suffix}"

        context.view_layer.update()
        self.report({"INFO"}, f"Renamed {len(targets)} objects.")
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
    bl_label = "Apply Preset"
    bl_description = "Load the selected radius preset into the numeric controls and apply it"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return bool(_target_curve_objects(context))

    def execute(self, context):
        settings = context.scene.curve_toolkit
        root_radius, mid_radius, tip_radius = _profile_preset_defaults(settings.profile_preset)

        global _PROFILE_UPDATE_RUNNING
        _PROFILE_UPDATE_RUNNING = True
        try:
            _set_profile_numeric_values(settings, root_radius, mid_radius, tip_radius)
        finally:
            _PROFILE_UPDATE_RUNNING = False

        changed_count = _apply_profile_settings_to_curves(context, settings, preset=settings.profile_preset)
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
        target_armature_obj = _active_or_selected_armature(context)
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
            segment_column = segment_box.column(align=False)
            segment_column.enabled = curve_obj is not None

            row = segment_column.row(align=True)
            row.prop(settings, "segment_preview_enabled")
            op = row.operator(CTK_OT_clear_preview.bl_idname, text="Clear Preview")
            op.kind = "SEGMENT"
            if settings.segment_preview_status:
                segment_column.label(text=settings.segment_preview_status, icon="INFO")

            quick_box = segment_column.box()
            quick_box.label(text="Quick Section Density (Hair)", icon="HAIR")
            row = quick_box.row(align=True)
            op = row.operator(CTK_OT_select_curve_segment.bl_idname, text="Select Root")
            op.mode = "ROOT"
            op = row.operator(CTK_OT_select_curve_segment.bl_idname, text="Select Tip")
            op.mode = "TIP"

            row = quick_box.row(align=True)
            op = row.operator(CTK_OT_segment_quick_subdivide.bl_idname, text="+1 Cut")
            op.cuts = 1
            op = row.operator(CTK_OT_segment_quick_subdivide.bl_idname, text="+2 Cuts")
            op.cuts = 2
            op = row.operator(CTK_OT_segment_quick_subdivide.bl_idname, text="+3 Cuts")
            op.cuts = 3

            segment_column.separator()
            segment_column.label(text="Raw Distribution")
            segment_column.prop(settings, "distribution_mode")
            bias_row = segment_column.row(align=True)
            bias_row.enabled = settings.distribution_mode == "CURVE"
            bias_row.prop(settings, "curvature_bias", slider=True)
            op = segment_column.operator(CTK_OT_segment_distribute.bl_idname, text="Apply Distribution")
            op.mode = settings.distribution_mode

            segment_column.separator()
            segment_column.separator()
            segment_column.label(text="Subdivide")
            segment_column.prop(settings, "subdivide_cuts")
            segment_column.prop(settings, "subdivide_distribution")
            bias_row = segment_column.row(align=True)
            bias_row.enabled = settings.subdivide_distribution == "CURVE"
            bias_row.prop(settings, "curvature_bias", slider=True)
            segment_column.operator(CTK_OT_segment_subdivide_selected.bl_idname)

            segment_column.separator()
            segment_column.label(text="Decimate")
            segment_column.prop(settings, "decimate_factor", slider=True)
            segment_column.prop(settings, "decimate_distribution_mode")
            bias_row = segment_column.row(align=True)
            bias_row.enabled = settings.decimate_distribution_mode == "CURVE"
            bias_row.prop(settings, "curvature_bias", slider=True)
            segment_column.operator(CTK_OT_segment_decimate_selected.bl_idname)

            segment_column.separator()
            segment_column.label(text="Auto")
            segment_column.prop(settings, "auto_subdivide_factor", slider=True)
            segment_column.operator(CTK_OT_segment_auto_subdivide.bl_idname)

            segment_column.separator()
            segment_column.separator()
            segment_column.label(text="Fit")
            op = segment_column.operator(CTK_OT_segment_distribute.bl_idname, text="Fit To Visual Path")
            op.mode = "FIT"

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

        bevel_box = layout.box()
        if self._draw_foldout(bevel_box, settings, "show_bevel_manager", "Bevel Manager", "CURVE_BEZCURVE"):
            bevel_box.prop(settings, "bevel_object")
            row = bevel_box.row(align=True)
            row.operator(CTK_OT_bevel_assign.bl_idname)
            row.operator(CTK_OT_bevel_copy_active.bl_idname)
            row = bevel_box.row(align=True)
            row.operator(CTK_OT_bevel_clear.bl_idname)
            row.operator(CTK_OT_bevel_select_same.bl_idname)

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

        resolution_box = layout.box()
        if self._draw_foldout(resolution_box, settings, "show_resolution_batch", "Collection Manager", "OUTLINER_COLLECTION"):
            collections_box = resolution_box.box()
            collections_box.label(text="Collections")
            row = collections_box.row(align=True)
            row.prop(settings, "resolution_collection")
            row.operator(CTK_OT_add_resolution_collection.bl_idname, text="", icon="ADD")

            collections = _resolution_batch_collections(settings)
            if settings.resolution_collections:
                collections_box.label(text=f"Registered: {len(collections)}")
                for index, item in enumerate(settings.resolution_collections):
                    row = collections_box.row(align=True)
                    row.label(text=item.collection.name if item.collection is not None else "Missing Collection")
                    op = row.operator(CTK_OT_remove_resolution_collection.bl_idname, text="", icon="X")
                    op.index = index
            else:
                collections_box.label(text=f"Registered: {len(collections)}")

            collection_control_box = resolution_box.box()
            collection_control_box.label(text="Collection Control")
            collection_control_box.prop(settings, "collection_rename_object_type")
            collection_control_box.prop(settings, "collection_rename_prefix")
            collection_control_box.prop(settings, "collection_rename_base_name")
            collection_control_box.prop(settings, "collection_rename_suffix")
            row = collection_control_box.row(align=True)
            row.prop(settings, "collection_rename_start")
            row.prop(settings, "collection_rename_padding")
            collection_control_box.operator(CTK_OT_rename_collection_objects.bl_idname)

            resolution_control_box = resolution_box.box()
            resolution_control_box.label(text="Resolution Control")
            path_curves, bevel_references = _resolution_batch_targets_from_collections(collections)
            resolution_control_box.label(text=f"Paths: {len(path_curves)}  Bevel Refs: {len(bevel_references)}")
            resolution_control_box.prop(settings, "path_resolution")
            resolution_control_box.prop(settings, "bevel_reference_resolution")
            resolution_control_box.operator(CTK_OT_refresh_resolution_batch.bl_idname)

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

        validation_box = layout.box()
        if self._draw_foldout(validation_box, settings, "show_validation_tools", "Validation", "CHECKMARK"):
            validation_box.operator(CTK_OT_validate_curves.bl_idname)
            validation_box.label(text=settings.validation_report)
            validation_box.operator(CTK_OT_select_validation_problems.bl_idname)

        mirror_box = layout.box()
        if self._draw_foldout(mirror_box, settings, "show_mirror", "Mirror", "MOD_MIRROR"):
            mirror_box.prop(settings, "mirror_axis")
            mirror_box.operator(CTK_OT_duplicate_mirror_selected_curves.bl_idname)
            row = mirror_box.row(align=True)
            op = row.operator(CTK_OT_select_mirror_side.bl_idname, text="Select L")
            op.side = "L"
            op = row.operator(CTK_OT_select_mirror_side.bl_idname, text="Select R")
            op.side = "R"
            mirror_box.operator(CTK_OT_remove_mirror_duplicates.bl_idname)

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
            segment_row = rigging_box.row(align=True)
            segment_row.enabled = settings.rig_fill_mode != "END_TO_END"
            segment_row.prop(settings, "rig_start_node")
            segment_row.prop(settings, "rig_end_node")

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

        bone_control_box = layout.box()
        if self._draw_foldout(bone_control_box, settings, "show_bone_control", "Bone Control", "ARMATURE_DATA"):
            row = bone_control_box.row(align=True)
            row.prop(settings, "bone_preview_enabled")
            op = row.operator(CTK_OT_clear_preview.bl_idname, text="Clear Preview")
            op.kind = "BONE"

            bone_control_box.prop(settings, "rig_distribution_mode")
            bias_row = bone_control_box.row(align=True)
            bias_row.enabled = settings.rig_distribution_mode == "CURVE"
            bias_row.prop(settings, "rig_curvature_bias", slider=True)

            bone_control_column = bone_control_box.column(align=True)
            bone_control_column.enabled = curve_obj is not None and target_armature_obj is not None
            bone_control_column.operator(CTK_OT_apply_bone_distribution.bl_idname)

            bone_control_box.separator()
            bone_control_box.label(text="Subdivide")
            bone_control_box.prop(settings, "rig_subdivide_cuts")
            bone_control_column = bone_control_box.column(align=True)
            bone_control_column.enabled = curve_obj is not None and target_armature_obj is not None
            bone_control_column.operator(CTK_OT_subdivide_selected_bones.bl_idname)

            bone_control_box.separator()
            bone_control_box.label(text="Decimate")
            bone_control_box.prop(settings, "rig_decimate_factor", slider=True)
            bone_control_column = bone_control_box.column(align=True)
            bone_control_column.enabled = curve_obj is not None and target_armature_obj is not None
            bone_control_column.operator(CTK_OT_decimate_selected_bones.bl_idname)


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
    CTK_OT_apply_bone_distribution,
    CTK_OT_subdivide_selected_bones,
    CTK_OT_decimate_selected_bones,
    CTK_OT_clear_preview,
    CTK_OT_invert_selected_bones,
    CTK_OT_reset_path,
    CTK_OT_reset_path_x_axis,
    CTK_OT_switch_direction,
    CTK_OT_set_origin,
    CTK_OT_snap_cursor,
    CTK_OT_segment_quick_subdivide,
    CTK_OT_select_curve_segment,
    CTK_OT_segment_distribute,
    CTK_OT_segment_subdivide_selected,
    CTK_OT_segment_decimate_selected,
    CTK_OT_segment_auto_subdivide,
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
    CTK_OT_select_mirror_side,
    CTK_OT_remove_mirror_duplicates,
    CTK_OT_set_fill_caps,
    CTK_OT_add_resolution_collection,
    CTK_OT_remove_resolution_collection,
    CTK_OT_refresh_resolution_batch,
    CTK_OT_rename_collection_objects,
    CTK_PT_tools,
)


def register():
    _remove_ctk_handlers()
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.curve_toolkit = PointerProperty(type=CTK_PG_settings)
    bpy.app.handlers.depsgraph_update_post.append(_ctk_curve_lock_handler)
    bpy.app.handlers.depsgraph_update_post.append(_ctk_preview_refresh_handler)
    bpy.app.handlers.save_pre.append(_ctk_clear_preview_save_handler)


def unregister():
    _remove_ctk_handlers()
    if hasattr(bpy.types.Scene, "curve_toolkit"):
        del bpy.types.Scene.curve_toolkit
    for cls in reversed(classes):
        if hasattr(cls, "bl_rna"):
            try:
                bpy.utils.unregister_class(cls)
            except RuntimeError as exc:
                if "missing bl_rna attribute" not in str(exc):
                    raise


if __name__ == "__main__":
    register()
