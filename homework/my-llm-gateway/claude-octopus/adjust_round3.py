"""Round 3: color pass on the Claude octopus project.

AgX view transform desaturates the coral; switch to Standard and deepen the
body base color so the Claude orange reads as intended.
Run: blender claude_octopus.blend --python adjust_round3.py
"""
import os

import bpy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLEND_PATH = os.path.join(BASE_DIR, "claude_octopus.blend")
PREVIEW_PATH = os.path.join(BASE_DIR, "previews", "preview_round3.png")
DONE_FLAG = os.path.join(BASE_DIR, "done_round3.flag")

if os.path.exists(DONE_FLAG):
    os.remove(DONE_FLAG)

# 1. Punchier color: Standard view transform instead of AgX
scene = bpy.context.scene
scene.view_settings.view_transform = 'Standard'

# 2. Slightly deeper, saturated Claude coral for body / tentacles
body_color = (0.855, 0.408, 0.290)
for mat_name in ("ClaudeBody",):
    mat = bpy.data.materials[mat_name]
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*body_color, 1.0)

# Blush a touch pinker so it separates from the body under Standard
blush_color = (0.949, 0.482, 0.404)
bsdf = bpy.data.materials["Blush"].node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (*blush_color, 1.0)

# Save, render, done flag
bpy.ops.wm.save_mainfile(filepath=BLEND_PATH)
scene.render.filepath = PREVIEW_PATH
bpy.ops.render.render(write_still=True)
with open(DONE_FLAG, "w") as fh:
    fh.write("ok\n")
print(f"OCTOPUS_ROUND3_DONE preview={PREVIEW_PATH}")
