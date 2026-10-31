"""Round 4: fix linear vs sRGB colors on the Claude octopus project.

Principled BSDF Base Color stores LINEAR values; earlier rounds fed sRGB
numbers, which rendered washed-out. Convert the palette to linear.
Run: blender claude_octopus.blend --python adjust_round4.py
"""
import os

import bpy

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLEND_PATH = os.path.join(BASE_DIR, "claude_octopus.blend")
PREVIEW_PATH = os.path.join(BASE_DIR, "previews", "preview_final.png")
DONE_FLAG = os.path.join(BASE_DIR, "done_round4.flag")

if os.path.exists(DONE_FLAG):
    os.remove(DONE_FLAG)


def srgb_to_linear(channel):
    """sRGB [0,1] -> linear [0,1] (piecewise, matches Blender's picker)."""
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def hex_to_linear(rgb_hex):
    srgb = tuple(int(rgb_hex[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return tuple(srgb_to_linear(c) for c in srgb)


# Claude brand coral + supporting palette, authored in sRGB then linearized
PALETTE = {
    "ClaudeBody": hex_to_linear("D97757"),  # Claude coral
    "Blush": hex_to_linear("EE8F7B"),       # rosier than the body
    "DarkWarm": hex_to_linear("3A2A22"),    # eyes / mouth, warm near-black
}

for mat_name, color in PALETTE.items():
    mat = bpy.data.materials[mat_name]
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)

# Save, render, done flag
scene = bpy.context.scene
bpy.ops.wm.save_mainfile(filepath=BLEND_PATH)
scene.render.filepath = PREVIEW_PATH
bpy.ops.render.render(write_still=True)
with open(DONE_FLAG, "w") as fh:
    fh.write("ok\n")
print(f"OCTOPUS_ROUND4_DONE preview={PREVIEW_PATH}")
