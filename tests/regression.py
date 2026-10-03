"""Run: blender --background --factory-startup --python-exit-code 1 --python tests/regression.py."""
import importlib.util
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Vector

spec = importlib.util.spec_from_file_location(
    'studio_render_presets', Path(__file__).resolve().parents[1] / '__init__.py')
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
addon.register()


def assert_matrix(actual, expected):
    assert max(abs(actual[i][j] - expected[i][j])
               for i in range(4) for j in range(4)) < 1e-5


def make_studio(scene, aspect):
    bpy.context.window.scene = scene
    bpy.ops.mesh.primitive_cube_add()
    product = bpy.context.object
    product.scale = (0.1, 5, 0.1)
    scene.render.resolution_x, scene.render.resolution_y = aspect
    scene.studio_render.rig_add_camera = True
    scene.studio_render.rig_backdrop = False
    bpy.context.view_layer.update()
    assert bpy.ops.studio.build_rig() == {'FINISHED'}
    assert bpy.ops.studio.add_watermark() == {'FINISHED'}
    return product


scene_a = bpy.context.scene
for obj in list(scene_a.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
product_a = make_studio(scene_a, (512, 512))
objects_a = set(scene_a.objects)
collection_a = addon._studio_collection(bpy.context)

scene_b = bpy.data.scenes.new('Second Studio')
product_b = make_studio(scene_b, (512, 640))
assert objects_a <= set(scene_a.objects), 'Building B deleted objects in A'
assert addon._studio_collection(bpy.context) != collection_a

for scene, product in ((scene_a, product_a), (scene_b, product_b)):
    bpy.context.window.scene = scene
    bpy.context.view_layer.update()
    parent = bpy.data.objects.new('Camera Parent', None)
    scene.collection.objects.link(parent)
    parent.location = (2, 3, 4)
    bpy.context.view_layer.update()
    world = scene.camera.matrix_world.copy()
    scene.camera.parent = parent
    scene.camera.matrix_parent_inverse = parent.matrix_world.inverted()
    scene.camera.matrix_world = world
    bpy.context.view_layer.update()
    originals = {o: (o.parent, o.matrix_basis.copy(), o.matrix_parent_inverse.copy())
                 for o in scene.objects if o.get(addon.RIG_TAG)}
    assert bpy.ops.studio.build_turntable() == {'FINISHED'}
    for frame in range(1, scene.frame_end + 1):
        scene.frame_set(frame)
        bpy.context.view_layer.update()
        corners = [world_to_camera_view(scene, scene.camera,
                                       product.matrix_world @ Vector(c))
                   for c in product.bound_box]
        assert all(0 <= p.x <= 1 and 0 <= p.y <= 1 and p.z > 0 for p in corners), frame
    scene.frame_set(31)
    assert bpy.ops.studio.build_turntable() == {'FINISHED'}
    scene.frame_set(31)
    assert bpy.ops.studio.remove_turntable() == {'FINISHED'}
    bpy.context.view_layer.update()
    for obj, (parent, basis, inverse) in originals.items():
        assert obj.parent == parent
        assert_matrix(obj.matrix_basis, basis)
        assert_matrix(obj.matrix_parent_inverse, inverse)

bpy.context.window.scene = scene_b
assert bpy.ops.studio.build_turntable() == {'FINISHED'}
objects_b = set(scene_b.objects)
bpy.context.window.scene = scene_a
assert addon._turntable_empty(bpy.context) is None
assert bpy.ops.studio.remove_rig() == {'FINISHED'}
assert objects_b <= set(scene_b.objects), 'Removing A deleted objects in B'
assert addon._turntable_empty(type('Context', (), {'scene': scene_b})()) is not None

# Check dispatch without starting an interactive render in background mode.
bpy.context.window.scene = scene_b
scene_b.studio_render.use_gpu = False
operator = addon.STUDIO_OT_apply_and_render
fake_operator = type('Operator', (), {'report': lambda *args: None})()
for output, animation in [('MP4', True), ('PNG_SEQ', False), ('AUTO', False)]:
    scene_b.studio_render.output_format = output
    with patch.object(addon, 'bpy', SimpleNamespace(ops=SimpleNamespace(
            render=SimpleNamespace(render=None)))):
        with patch.object(addon, 'apply_preset'):
            with patch.object(addon.bpy.ops.render, 'render') as render:
                addon.set_output_format(scene_b.render,
                                        'PNG_SEQ' if output == 'AUTO' else output)
                assert operator.execute(fake_operator, bpy.context) == {'FINISHED'}
                render.assert_called_once_with('INVOKE_DEFAULT', animation=animation,
                                               write_still=not animation)
print('PASS: scene isolation, 120-frame framing in square/portrait, rebuild/removal, render dispatch')
