"""Create a cute rounded Claude octopus (Clawd-style) in Blender 5.2.

One fixed project path; this script creates the scene and saves it.
Run: blender --python create_octopus.py
"""
import math
import os

import bpy
import mathutils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLEND_PATH = os.path.join(BASE_DIR, "claude_octopus.blend")
PREVIEW_PATH = os.path.join(BASE_DIR, "previews", "preview_round1.png")
DONE_FLAG = os.path.join(BASE_DIR, "done_round1.flag")

if os.path.exists(DONE_FLAG):
    os.remove(DONE_FLAG)
os.makedirs(os.path.dirname(PREVIEW_PATH), exist_ok=True)

# Blender 5.x: bpy.context.active_object is gone
def active():
    return bpy.context.view_layer.objects.active


def shade_smooth(obj):
    for poly in obj.data.polygons:
        poly.use_smooth = True


# ----------------------------------------------------------------------------
# Clean scene
# ----------------------------------------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)

# ----------------------------------------------------------------------------
# Materials
# ----------------------------------------------------------------------------
CLAUDE_ORANGE = (0.835, 0.447, 0.322)   # Claude coral
DARK_WARM = (0.145, 0.098, 0.078)       # eyes / mouth
BLUSH = (0.937, 0.529, 0.412)           # cheeks
WHITE = (1.0, 1.0, 1.0)


def make_mat(name, color, roughness=0.5, specular=0.35):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    for spec_name in ("Specular IOR Level", "Specular"):
        if spec_name in bsdf.inputs:
            bsdf.inputs[spec_name].default_value = specular
            break
    return mat


mat_body = make_mat("ClaudeBody", CLAUDE_ORANGE, roughness=0.55)
mat_dark = make_mat("DarkWarm", DARK_WARM, roughness=0.3)
mat_blush = make_mat("Blush", BLUSH, roughness=0.9)
mat_highlight = make_mat("EyeHighlight", WHITE, roughness=0.05)
mat_ground = make_mat("Ground", (0.93, 0.90, 0.87), roughness=0.95)

# ----------------------------------------------------------------------------
# Body: chubby sphere, center z=1.02, radii (1.0, 1.0, 0.94)
# ----------------------------------------------------------------------------
BODY_CENTER = (0.0, 0.0, 1.02)
BODY_RADII = (1.0, 1.0, 0.94)

bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=48, radius=1.0,
                                     location=BODY_CENTER)
body = active()
body.name = "Body"
body.scale = BODY_RADII
shade_smooth(body)
body.data.materials.append(mat_body)
subsurf = body.modifiers.new("Subsurf", 'SUBSURF')
subsurf.levels = 2
subsurf.render_levels = 2

# ----------------------------------------------------------------------------
# Taper object shared by all tentacles (full radius at root -> tiny at tip)
# ----------------------------------------------------------------------------
tc_data = bpy.data.curves.new("TaperCurve", type='CURVE')
tc_data.dimensions = '2D'
spline = tc_data.splines.new('POLY')
spline.points.add(1)
spline.points[0].co = (0.0, 1.0, 0.0, 1.0)
spline.points[1].co = (1.0, 0.05, 0.0, 1.0)
taper_obj = bpy.data.objects.new("TaperObject", tc_data)
taper_obj.location = (-8.0, -8.0, 0.0)  # keep out of the way; local coords are what count
bpy.context.collection.objects.link(taper_obj)

# ----------------------------------------------------------------------------
# Eight stubby tentacles that curl up at the tips
# ----------------------------------------------------------------------------
TENTACLE_PROFILE = [
    # (radius fraction of body R, world z)
    (0.28, 0.34),   # root tucked inside the lower body
    (0.60, 0.14),   # sloping down/outward
    (0.90, 0.05),   # resting near the ground
    (1.10, 0.16),   # start of the upward curl
    (1.16, 0.40),   # tip pointing up
]


def make_tentacle(index, total=8):
    azimuth = -math.pi / 2 + (2 * math.pi * index / total)
    curve_data = bpy.data.curves.new(f"Tentacle{index:02d}", type='CURVE')
    curve_data.dimensions = '3D'
    curve_data.bevel_depth = 0.21
    curve_data.bevel_resolution = 6
    curve_data.use_fill_caps = True
    curve_data.taper_object = taper_obj
    curve_data.use_map_taper = True
    spline = curve_data.splines.new('BEZIER')
    spline.bezier_points.add(len(TENTACLE_PROFILE) - 1)
    for bp, (frac, z) in zip(spline.bezier_points, TENTACLE_PROFILE):
        r = frac * BODY_RADII[0]
        bp.co = (r * math.cos(azimuth), r * math.sin(azimuth), z)
        bp.handle_left_type = 'AUTO'
        bp.handle_right_type = 'AUTO'
    obj = bpy.data.objects.new(f"Tentacle{index:02d}", curve_data)
    bpy.context.collection.objects.link(obj)
    obj.data.materials.append(mat_body)
    return obj


for i in range(8):
    make_tentacle(i)

# ----------------------------------------------------------------------------
# Face helpers
# ----------------------------------------------------------------------------
def sph_dir(azimuth_deg, elevation_deg):
    a = math.radians(azimuth_deg)
    e = math.radians(elevation_deg)
    return (math.cos(e) * math.sin(a), math.cos(e) * math.cos(a), math.sin(e))


def surface_point(azimuth_deg, elevation_deg, offset=1.0):
    """Point on (or offset from) the body surface along its normal."""
    d = sph_dir(azimuth_deg, elevation_deg)
    return (BODY_CENTER[0] + d[0] * BODY_RADII[0] * offset,
            BODY_CENTER[1] + d[1] * BODY_RADII[1] * offset,
            BODY_CENTER[2] + d[2] * BODY_RADII[2] * offset)


def add_ball(name, location, radius, mat, squash=None, align_y_to=None):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=24,
                                         radius=radius, location=location)
    obj = active()
    obj.name = name
    if squash:
        obj.scale = squash
    if align_y_to is not None:
        # squash acts in local space: aim local Y at the surface normal
        obj.rotation_euler = align_y_to.to_track_quat('Y', 'Z').to_euler()
    shade_smooth(obj)
    obj.data.materials.append(mat)
    return obj


# ----------------------------------------------------------------------------
# Face: eyes, catch lights, blush, smile
# ----------------------------------------------------------------------------
# Eyes sit slightly proud of the surface
eye_l = add_ball("EyeL", surface_point(24, 13, 0.985), 0.165, mat_dark)
eye_r = add_ball("EyeR", surface_point(-24, 13, 0.985), 0.165, mat_dark)

# Catch lights: offset from the eye center, outward and up, half sunk in
def add_catchlight(az_deg, eye):
    d = sph_dir(az_deg, 16)
    pos = (eye.location[0] + d[0] * 0.10,
           eye.location[1] + d[1] * 0.10,
           eye.location[2] + 0.11)
    return add_ball(f"Catchlight{az_deg:.0f}", pos, 0.048, mat_highlight)


add_catchlight(29.0, eye_l)
add_catchlight(-29.0, eye_r)

# Blush: flattened pastilles hugging the surface (local Y aimed at the normal)
blush_l = add_ball("BlushL", surface_point(44, -8, 1.0), 0.15, mat_blush,
                   squash=(1.0, 0.35, 0.72),
                   align_y_to=mathutils.Vector(sph_dir(44, -8)))
blush_r = add_ball("BlushR", surface_point(-44, -8, 1.0), 0.15, mat_blush,
                   squash=(1.0, 0.35, 0.72),
                   align_y_to=mathutils.Vector(sph_dir(-44, -8)))

# Smile: small beveled curve dipping down in the middle
smile_data = bpy.data.curves.new("Smile", type='CURVE')
smile_data.dimensions = '3D'
smile_data.bevel_depth = 0.028
smile_data.bevel_resolution = 4
smile_data.use_fill_caps = True
sp = smile_data.splines.new('BEZIER')
sp.bezier_points.add(2)
pts = [surface_point(a, -16, 1.01) for a in (-13, 0, 13)]
pts[1] = (pts[1][0], pts[1][1], pts[1][2] - 0.055)  # dip = smile
for bp, co in zip(sp.bezier_points, pts):
    bp.co = co
    bp.handle_left_type = 'AUTO'
    bp.handle_right_type = 'AUTO'
smile = bpy.data.objects.new("Smile", smile_data)
bpy.context.collection.objects.link(smile)
smile.data.materials.append(mat_dark)

# ----------------------------------------------------------------------------
# Ground
# ----------------------------------------------------------------------------
bpy.ops.mesh.primitive_plane_add(size=24, location=(0, 0, 0))
ground = active()
ground.name = "Ground"
ground.data.materials.append(mat_ground)

# ----------------------------------------------------------------------------
# Lights
# ----------------------------------------------------------------------------
def add_area(name, loc, energy, size, color=(1.0, 0.96, 0.92), rot=None):
    light_data = bpy.data.lights.new(name, type='AREA')
    light_data.energy = energy
    light_data.size = size
    light_data.color = color
    light = bpy.data.objects.new(name, light_data)
    light.location = loc
    if rot:
        light.rotation_euler = rot
    bpy.context.collection.objects.link(light)
    return light


add_area("Key", (3.4, 3.2, 5.0), 900, 4.0,
         rot=(math.radians(50), math.radians(18), math.radians(38)))
add_area("Fill", (-4.5, 2.5, 2.5), 300, 5.0, color=(0.9, 0.95, 1.0),
         rot=(math.radians(70), math.radians(-30), math.radians(-60)))
add_area("Rim", (-1.5, -4.5, 4.0), 500, 3.0,
         rot=(math.radians(115), 0, math.radians(180)))

world = bpy.data.worlds.new("Studio")
bpy.context.scene.world = world
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
bg.inputs[0].default_value = (0.78, 0.76, 0.74, 1.0)
bg.inputs[1].default_value = 0.85

# ----------------------------------------------------------------------------
# Camera
# ----------------------------------------------------------------------------
cam_data = bpy.data.cameras.new("Camera")
cam_data.lens = 60
cam = bpy.data.objects.new("Camera", cam_data)
bpy.context.collection.objects.link(cam)
bpy.context.scene.camera = cam
cam.location = (3.3, 4.4, 2.8)
direction = mathutils.Vector((0.0, 0.0, 1.05)) - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

# ----------------------------------------------------------------------------
# Render settings, save, render, done flag
# ----------------------------------------------------------------------------
scene = bpy.context.scene
scene.render.resolution_x = 1200
scene.render.resolution_y = 1200
scene.render.image_settings.file_format = 'PNG'
try:
    scene.eevee.taa_render_samples = 64
except AttributeError:
    pass

bpy.ops.wm.save_as_mainfile(filepath=BLEND_PATH)
scene.render.filepath = PREVIEW_PATH
bpy.ops.render.render(write_still=True)
with open(DONE_FLAG, "w") as fh:
    fh.write("ok\n")
print(f"OCTOPUS_DONE blend={BLEND_PATH} preview={PREVIEW_PATH}")
