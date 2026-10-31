"""Round 2 adjustments on the Claude octopus project.

Loads claude_octopus.blend, adjusts camera / tentacle curl / blush / backdrop,
saves back to the same path and renders a new preview.
Run: blender claude_octopus.blend --python adjust_round2.py
"""
import math
import os

import bpy
import mathutils

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLEND_PATH = os.path.join(BASE_DIR, "claude_octopus.blend")
PREVIEW_PATH = os.path.join(BASE_DIR, "previews", "preview_round2.png")
DONE_FLAG = os.path.join(BASE_DIR, "done_round2.flag")

if os.path.exists(DONE_FLAG):
    os.remove(DONE_FLAG)

BODY_CENTER = (0.0, 0.0, 1.02)
BODY_RADII = (1.0, 1.0, 0.94)

# Softer, broader upward curl at the tentacle tips
TENTACLE_PROFILE = [
    (0.28, 0.34),
    (0.60, 0.14),
    (0.90, 0.05),
    (1.10, 0.08),
    (1.24, 0.32),
]


def sph_dir(azimuth_deg, elevation_deg):
    a = math.radians(azimuth_deg)
    e = math.radians(elevation_deg)
    return (math.cos(e) * math.sin(a), math.cos(e) * math.cos(a), math.sin(e))


def surface_point(azimuth_deg, elevation_deg, offset=1.0):
    d = sph_dir(azimuth_deg, elevation_deg)
    return (BODY_CENTER[0] + d[0] * BODY_RADII[0] * offset,
            BODY_CENTER[1] + d[1] * BODY_RADII[1] * offset,
            BODY_CENTER[2] + d[2] * BODY_RADII[2] * offset)


# ----------------------------------------------------------------------------
# 1. Camera: more frontal view so both cheeks and eyes read symmetric
# ----------------------------------------------------------------------------
cam = bpy.data.objects["Camera"]
cam.location = (1.7, 5.1, 2.9)
direction = mathutils.Vector((0.0, 0.0, 1.05)) - cam.location
cam.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()

# ----------------------------------------------------------------------------
# 2. Tentacles: softer curl
# ----------------------------------------------------------------------------
for obj in bpy.data.objects:
    if not obj.name.startswith("Tentacle"):
        continue
    index = int(obj.name[-2:])
    azimuth = -math.pi / 2 + (2 * math.pi * index / 8)
    spline = obj.data.splines[0]
    for bp, (frac, z) in zip(spline.bezier_points, TENTACLE_PROFILE):
        r = frac * BODY_RADII[0]
        bp.co = (r * math.cos(azimuth), r * math.sin(azimuth), z)
        bp.handle_left_type = 'AUTO'
        bp.handle_right_type = 'AUTO'

# ----------------------------------------------------------------------------
# 3. Blush: pull slightly toward the front so both are visible
# ----------------------------------------------------------------------------
for name, az in (("BlushL", 38.0), ("BlushR", -38.0)):
    obj = bpy.data.objects[name]
    obj.location = surface_point(az, -8, 1.0)
    obj.rotation_euler = mathutils.Vector(sph_dir(az, -8)).to_track_quat('Y', 'Z').to_euler()

# ----------------------------------------------------------------------------
# 4. Backdrop closer to ground tone to soften the horizon
# ----------------------------------------------------------------------------
bg = bpy.context.scene.world.node_tree.nodes["Background"]
bg.inputs[0].default_value = (0.88, 0.86, 0.83, 1.0)

# ----------------------------------------------------------------------------
# Save, render, done flag
# ----------------------------------------------------------------------------
bpy.ops.wm.save_mainfile(filepath=BLEND_PATH)
scene = bpy.context.scene
scene.render.filepath = PREVIEW_PATH
bpy.ops.render.render(write_still=True)
with open(DONE_FLAG, "w") as fh:
    fh.write("ok\n")
print(f"OCTOPUS_ROUND2_DONE preview={PREVIEW_PATH}")
