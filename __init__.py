# SPDX-FileCopyrightText: 2026 Travis Forsyth
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Studio Render Presets
=====================
A small sidebar panel that applies render settings tuned by measurement rather
than by feel. Four presets: Quick, 2K, 4K, 8K.

The numbers behind the presets come from timed/measured renders of a studio
lightbox scene at 4096x4096, holding everything else constant:

    denoise off ................ 1.67 GB peak
    denoise RGB / Fast ......... 3.88 GB peak
    denoise RGB+Alb+Nrm / Acc .. 5.34 GB peak

The denoiser, not the raytracing, dominates memory at high resolution. That is
why the 8K preset disables in-render denoising and pays for cleanliness with
samples instead -- it is what makes 8K fit on a 16 GB unified-memory machine.

Tile size was tested at 2048 / 512 / off and moved peak memory by <20 MB,
because the denoiser runs on the assembled frame regardless. Auto-tiling is
still left on, since it protects GPU rendering.

Those measurements were taken on CPU in a 7 GB container on a near-empty
scene, so treat the panel's estimate as a sanity check with a deliberately
wide band -- not a promise. Watch Activity Monitor on your first big render.
"""

import bpy
import os
import math
import json
import time

from bpy.props import (
    EnumProperty, IntProperty, FloatProperty, BoolProperty, StringProperty,
    PointerProperty,
)
from bpy.types import Panel, Operator, PropertyGroup, AddonPreferences

# ---------------------------------------------------------------------------
#  Preset table
# ---------------------------------------------------------------------------
#  long_edge      : pixels on the longer axis
#  samples        : max adaptive samples
#  threshold      : adaptive sampling noise threshold (lower = cleaner/slower)
#  min_samples    : adaptive minimum before it may bail out
#  denoise        : "off" | "fast" | "full"
#  bounces        : (max, diffuse, glossy, transmission)
#  caustics       : reflective/refractive on
#  fast_gi        : approximate GI after N bounces (preview only)
#  depth          : output bit depth for EXR
#  fmt            : output file format

PRESETS = {
    'QUICK': dict(
        label="Quick", long_edge=1920, samples=64, threshold=0.05,
        min_samples=16, denoise="fast", bounces=(6, 2, 2, 6),
        caustics=False, fast_gi=True, fmt='PNG', depth='8',
        blurb="Fast look-dev pass. Low bounces, approximate GI, 8-bit PNG.",
    ),
    'P2K': dict(
        label="2K Production", long_edge=2048, samples=256, threshold=0.01,
        min_samples=32, denoise="full", bounces=(12, 6, 6, 12),
        caustics=True, fast_gi=False, fmt='OPEN_EXR', depth='16',
        blurb="Client-ready at web/print-small size. Full denoise, half-float EXR.",
    ),
    'P4K': dict(
        label="4K Production", long_edge=3840, samples=512, threshold=0.005,
        min_samples=64, denoise="full", bounces=(16, 8, 8, 16),
        caustics=True, fast_gi=False, fmt='OPEN_EXR', depth='16',
        blurb="The reliable hero render. Full denoise still fits comfortably.",
    ),
    'P8K': dict(
        label="8K Production", long_edge=7680, samples=2048, threshold=0.002,
        min_samples=64, denoise="off", bounces=(16, 8, 8, 16),
        caustics=True, fast_gi=False, fmt='OPEN_EXR', depth='32',
        blurb="Denoise OFF by design -- samples do the cleaning so it fits in RAM.",
    ),
}

ASPECTS = {
    '16_9': (16, 9),
    '3_2':  (3, 2),
    '1_1':  (1, 1),
    '4_5':  (4, 5),
}

# Measured anchor: 4096x4096 = 16.78 MP
ANCHOR_MP = 16.78
ANCHOR_GB = {"off": 1.67, "fast": 3.88, "full": 5.34}


def compute_resolution(long_edge, aspect_key):
    w_r, h_r = ASPECTS[aspect_key]
    if w_r >= h_r:
        w = long_edge
        h = int(round(long_edge * h_r / w_r))
    else:
        h = long_edge
        w = int(round(long_edge * w_r / h_r))
    # keep both even; codecs and tiling are happier
    return (w - w % 2), (h - h % 2)


def estimate_gb(mp, profile, overhead):
    """Conservative range, not a point estimate.

    Memory scaling measured clearly sub-linear (1.17 MP -> 1.24 GB,
    4.19 MP -> 3.55 GB, 16.8 MP -> 5.40 GB), so a single coefficient
    fitted at the top under-predicts everything below it. Below the
    anchor we bracket between proportional and near-flat; above it we
    bracket between a damped curve and proportional.
    """
    a = ANCHOR_GB[profile]
    linear = a * (mp / ANCHOR_MP)
    if mp <= ANCHOR_MP:
        lo, hi = linear, a
    else:
        lo, hi = a * (mp / ANCHOR_MP) ** 0.6, linear
    return lo + overhead, hi + overhead


def preset_profile(key):
    return PRESETS[key]["denoise"]


# ---------------------------------------------------------------------------
#  Lighting setups and backdrop tones
# ---------------------------------------------------------------------------
#  Lighting setups. Each light is:
#      (name, azimuth, elevation, distance/radius, power ratio, panel/radius)
#  Azimuth is degrees off the camera axis, positive counter-clockwise seen
#  from above; elevation is degrees above the subject.
#
#  `exposure` is a per-setup correction so that SWITCHING SETUP CHANGES THE
#  LOOK, NOT THE BRIGHTNESS. Rim-heavy setups throw most of their light away
#  from camera, so an equal-wattage rig would render much darker. Each value
#  below was solved by rendering an 18% grey sphere and driving it to the
#  same landing point, not estimated.

LIGHT_SETUPS = {
    'THREE_POINT': dict(
        label="Three Point", uses_key=True, exposure=1.05,
        blurb="Key, fill and rim. The dependable all-rounder.",
        lights=[
            ("Key",  None, None, 3.2, 1.00, 2.2),
            ("Fill", None,  15.0, 3.6, 0.35, 2.8),
            ("Rim",  155.0, 45.0, 3.0, 0.60, 1.2),
        ]),
    'CLAMSHELL': dict(
        label="Clamshell", uses_key=False, exposure=0.606,
        blurb="Big softbox above, bounce below, both on the camera axis. "
              "Very even - suits jewellery and small parts.",
        lights=[
            ("Top",    0.0, 62.0, 2.8, 1.00, 3.0),
            ("Bottom", 0.0, 10.0, 2.8, 0.45, 2.6),
        ]),
    'BUTTERFLY': dict(
        label="Butterfly", uses_key=False, exposure=0.606,
        blurb="Key high on the camera axis with a low fill. Symmetric "
              "shadows under overhangs.",
        lights=[
            ("Key",  0.0, 55.0, 3.0, 1.00, 2.0),
            ("Fill", 0.0, 12.0, 3.4, 0.30, 2.6),
            ("Rim",  180.0, 50.0, 3.0, 0.45, 1.2),
        ]),
    'DRAMATIC': dict(
        label="Dramatic", uses_key=True, exposure=1.4,
        blurb="One tight key and a hard rim, no fill. Deep shadows - pair "
              "with a dark backdrop.",
        lights=[
            ("Key", None, None, 3.0, 1.00, 1.0),
            ("Rim", -160.0, 40.0, 2.8, 0.90, 0.8),
        ]),
    'HIGH_KEY': dict(
        label="High Key", uses_key=False, exposure=0.719,
        blurb="Broad front, two side wraps and a backdrop wash. Shadowless "
              "white-background catalogue look.",
        lights=[
            ("Front", 0.0, 50.0, 3.4, 1.00, 4.0),
            ("Left", -70.0, 25.0, 3.6, 0.60, 3.0),
            ("Right", 70.0, 25.0, 3.6, 0.60, 3.0),
            ("Wash", 180.0, 55.0, 3.2, 0.70, 3.0),
        ]),
    'SILHOUETTE': dict(
        # Exposure is NOT solved to mid-grey like the others -- that would
        # destroy the effect. Chosen so the body sits near 0.15 with the edge
        # highlight around 0.82: dark enough to read as a silhouette, light
        # enough to still show form.
        label="Silhouette Rim", uses_key=False, exposure=1.0,
        prefers_tone='BLACK', cam_elevation=3.0,
        blurb="Narrow strip lights rake the edges so the outline glows and "
              "the body stays dark. Wants a black sweep.",
        lights=[
            # Tall narrow strips, almost directly behind, at roughly the
            # product's own height so the highlight runs down the vertical
            # edges instead of pooling on the top face. aspect 5 = strip.
            # 135 deg, not straight behind: a box's silhouette edge is the
            # corner where the front face meets the side, and only a raking
            # angle catches it. Further back rims a cylinder but misses a box.
            ("Rim L",  135.0, 22.0, 2.6, 1.00, 0.35, 5.0),
            ("Rim R", -135.0, 22.0, 2.6, 1.00, 0.35, 5.0),
            # Grazing, not overhead. At a steep angle this lights the top
            # FACE flat-on and blows it out; kept low it only edges it.
            ("Crown",  180.0, 42.0, 3.0, 0.22, 0.8, 3.0),
            # Just enough front light to admit the object has a face.
            ("Whisper",  0.0, 25.0, 4.0, 0.05, 3.0, 1.0),
        ]),
    'RIM_DUO': dict(
        label="Rim Duo", uses_key=False, exposure=2.597,
        blurb="Two back rims and a whisper of front fill. Edge-lit "
              "silhouette on black.",
        lights=[
            ("Rim L",  145.0, 35.0, 2.8, 1.00, 1.0),
            ("Rim R", -145.0, 35.0, 2.8, 1.00, 1.0),
            ("Fill",     0.0, 20.0, 3.8, 0.18, 3.0),
        ]),
}

#  Backdrop tones. Deliberately neutral (R=G=B): a tinted sweep colours every
#  bounce that reaches the product, which is exactly the fault we found in the
#  original template.
BACKDROP_TONES = {
    'WHITE': ("White", 0.85),
    'LIGHT': ("Light Grey", 0.55),
    'MID':   ("Mid Grey", 0.25),
    'DARK':  ("Dark Grey", 0.08),
    'BLACK': ("Black", 0.02),
}


# ---------------------------------------------------------------------------
#  Render-time estimation
# ---------------------------------------------------------------------------
#  Render time is hardware AND scene dependent, so nothing useful can be
#  shipped as a constant. It can however be MEASURED, and it turns out to
#  behave very well:
#
#      time = overhead + k * megapixels
#
#  Measured on a studio scene across 0.016 - 0.20 MP, that model fit with
#  R^2 = 0.99998, and a fit taken from only the two smallest points predicted
#  a render 5x larger to within 1%. The reason it holds is that samples-per-
#  pixel is resolution independent, so adaptive sampling behaves the same way
#  per pixel whatever the frame size; only the pixel count changes.
#
#  So: calibrate with two quick small renders, fit the line, extrapolate.
#  The fit is per (preset, device) because both change the slope, and it is
#  refined every time you complete a real render.
#
#  A warm-up render is discarded first. On Metal the first render of a
#  session pays kernel compilation, which would otherwise poison the
#  overhead term.

_BUSY = False            # suppress learning during calibration / region tiles
_PENDING = {}            # render_init -> render_complete handoff


def _calib_all(scene):
    try:
        return json.loads(scene.studio_render.calib_data or "{}")
    except (ValueError, AttributeError):
        return {}


def _calib_key(scene, preset_key):
    return f"{preset_key}|{scene.cycles.device}"


def get_calib(scene, preset_key):
    return _calib_all(scene).get(_calib_key(scene, preset_key))


def set_calib(scene, preset_key, a, b, n=1):
    data = _calib_all(scene)
    data[_calib_key(scene, preset_key)] = {"a": a, "b": b, "n": n}
    scene.studio_render.calib_data = json.dumps(data)


def estimate_seconds(scene, preset_key, mp):
    c = get_calib(scene, preset_key)
    if not c:
        return None
    return max(0.0, c["a"] + c["b"] * mp)


def fmt_time(sec):
    if sec is None:
        return "not calibrated"
    sec = int(round(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


# ---------------------------------------------------------------------------
#  Settings
# ---------------------------------------------------------------------------

def _setup_changed(st):
    """Some setups only read properly on a particular sweep. Switching to one
    moves the tone with it -- the user can still change it afterwards; this
    only fires when the setup itself changes."""
    pref = LIGHT_SETUPS.get(st.rig_setup, {}).get("prefers_tone")
    if pref and st.rig_backdrop_tone != pref:
        st.rig_backdrop_tone = pref


class StudioRenderSettings(PropertyGroup):
    preset: EnumProperty(
        name="Preset",
        items=[
            ('QUICK', "Quick",         PRESETS['QUICK']['blurb'], 'TIME', 0),
            ('P2K',   "2K",            PRESETS['P2K']['blurb'],   'RENDER_STILL', 1),
            ('P4K',   "4K",            PRESETS['P4K']['blurb'],   'RENDER_STILL', 2),
            ('P8K',   "8K",            PRESETS['P8K']['blurb'],   'RENDER_STILL', 3),
        ],
        default='P4K',
    )
    aspect: EnumProperty(
        name="Aspect",
        items=[
            ('16_9', "16:9", "Widescreen"),
            ('3_2',  "3:2",  "Classic photo"),
            ('1_1',  "1:1",  "Square"),
            ('4_5',  "4:5",  "Portrait"),
        ],
        default='1_1',
    )
    use_gpu: BoolProperty(
        name="Use GPU (Metal)", default=True,
        description="Render on the GPU. The CPU device is left off deliberately: "
                    "on unified memory it costs RAM for little gain",
    )
    use_region: BoolProperty(
        name="Render in Regions", default=False,
        description="Render as a grid of tiles and stitch them. Peak memory is "
                    "set by one tile, not the whole frame. Slower overall",
    )
    region_grid: IntProperty(
        name="Grid", default=2, min=2, max=4,
        description="N x N tiles. 2 means 4 tiles",
    )
    output_dir: StringProperty(
        name="Output", subtype='DIR_PATH', default="//renders/",
    )
    calib_data: StringProperty(
        name="Calibration", default="{}",
        description="Measured render-time fits, per preset and device (JSON)",
    )
    # ---- product studio rig ----
    rig_setup: EnumProperty(
        name="Setup",
        items=[(k, v["label"], v["blurb"]) for k, v in LIGHT_SETUPS.items()],
        default='THREE_POINT',
        update=lambda self, ctx: _setup_changed(self),
    )
    rig_backdrop_tone: EnumProperty(
        name="Tone",
        items=[(k, v[0], f"Neutral {v[0].lower()} sweep") for k, v in BACKDROP_TONES.items()]
              + [('CUSTOM', "Custom", "Pick your own colour")],
        default='LIGHT',
    )
    rig_backdrop_custom: bpy.props.FloatVectorProperty(
        name="Colour", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(0.55, 0.55, 0.55),
        description="Custom sweep colour. Saturated choices tint every bounce "
                    "that lands on the product",
    )
    rig_brightness: FloatProperty(
        name="Brightness", default=1.0, min=0.05, max=10.0, soft_max=3.0,
        description="Scales the whole rig. 1.0 puts a neutral grey subject "
                    "near mid-grey after AgX",
    )
    rig_softness: FloatProperty(
        name="Softness", default=1.0, min=0.1, max=4.0,
        description="Light panel size relative to the product. Bigger is "
                    "softer shadows and broader highlights",
    )
    rig_key_azimuth: FloatProperty(
        name="Key Angle", default=45.0, min=5.0, max=90.0,
        description="Key light angle off the camera axis, in degrees",
    )
    rig_key_elevation: FloatProperty(
        name="Key Height", default=40.0, min=0.0, max=85.0,
        description="Key light elevation above the product, in degrees",
    )
    rig_backdrop: BoolProperty(
        name="Seamless Backdrop", default=True,
        description="Add a cyclorama sweep behind and under the product",
    )
    rig_backdrop_width: FloatProperty(
        name="Width", default=8.0, min=2.0, max=30.0,
        description="Backdrop width as a multiple of product radius",
    )
    rig_backdrop_height: FloatProperty(
        name="Height", default=5.0, min=1.5, max=20.0,
        description="Backdrop wall height as a multiple of product radius",
    )
    rig_backdrop_fillet: FloatProperty(
        name="Sweep", default=2.0, min=0.2, max=10.0,
        description="Radius of the floor-to-wall curve, as a multiple of "
                    "product radius. Larger is a gentler sweep",
    )
    # ---- turntable ----
    tt_frames: IntProperty(
        name="Frames", default=120, min=2, max=2000,
        description="Frames for one full revolution. 120 at 24fps is a 5 second loop",
    )
    tt_clockwise: BoolProperty(
        name="Clockwise", default=True,
        description="Direction the product appears to turn, seen from above",
    )
    tt_set_output: BoolProperty(
        name="Set Output", default=True,
        description="Set the frame range and switch output to a 16-bit PNG "
                    "sequence in the render folder",
    )
    # ---- watermark ----
    wm_text: StringProperty(
        name="Text", default="Product Render",
        description="Watermark text. Leave blank for none",
    )
    wm_corner: EnumProperty(
        name="Corner",
        items=[('BL', "Bottom Left", ""), ('BR', "Bottom Right", ""),
               ('TL', "Top Left", ""), ('TR', "Top Right", "")],
        default='BL',
    )
    wm_size: FloatProperty(
        name="Size", default=0.030, min=0.005, max=0.25,
        description="Cap height as a fraction of frame height, so it stays the "
                    "same relative size at every render resolution",
    )
    wm_margin: FloatProperty(
        name="Margin", default=0.035, min=0.0, max=0.3,
        description="Inset from the frame edge, as a fraction of frame height",
    )
    wm_opacity: FloatProperty(
        name="Opacity", default=0.75, min=0.0, max=1.0,
        description="1.0 is solid; lower lets the render show through",
    )
    wm_strength: FloatProperty(
        name="Brightness", default=6.0, min=0.0, max=200.0,
        description="Emission strength. Under AgX a strength of 1.0 lands "
                    "near mid-grey, not white, so this defaults higher",
    )
    wm_color: bpy.props.FloatVectorProperty(
        name="Colour", subtype='COLOR', size=3, min=0.0, max=1.0,
        default=(1.0, 1.0, 1.0),
        description="White at 0.75 opacity is the default. For a DARK "
                    "watermark set this to pure black and opacity ~0.85: "
                    "black emits nothing, so the text reads as a shadow. "
                    "A near-black like 0.02 will not work - it still emits",
    )
    wm_font: StringProperty(
        name="Font", subtype='FILE_PATH', default="",
        description="Optional .ttf/.otf. Blank uses Blender's built-in font",
    )
    rig_add_camera: BoolProperty(
        name="Add Camera", default=False,
        description="Add an 85mm camera framing the product and make it active",
    )


class StudioRenderPrefs(AddonPreferences):
    bl_idname = __package__

    memory_budget: FloatProperty(
        name="Memory Budget (GB)", default=10.0, min=1.0, max=256.0,
        description="What you believe you can spend. On a 16 GB Mac, ~10 GB is "
                    "realistic after macOS and Blender",
    )
    scene_overhead: FloatProperty(
        name="Scene Overhead (GB)", default=1.5, min=0.0, max=64.0,
        description="Headroom for your model's geometry and textures, on top of "
                    "the measured baseline. Raise it for heavy models",
    )

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "memory_budget")
        col.prop(self, "scene_overhead")
        col.separator()
        box = col.box()
        box.label(text="Estimates are measured on a light scene and bracketed wide.", icon='INFO')
        box.label(text="Treat them as a sanity check, not a guarantee.")


def prefs(context):
    try:
        return context.preferences.addons[__package__].preferences
    except (KeyError, AttributeError):
        class _D:
            memory_budget = 10.0
            scene_overhead = 1.5
        return _D()


# ---------------------------------------------------------------------------
#  Core: apply a preset
# ---------------------------------------------------------------------------

def apply_preset(context, key, report=None):
    sc = context.scene
    rnd, cy = sc.render, sc.cycles
    st = sc.studio_render
    p = PRESETS[key]
    notes = []

    if rnd.engine != 'CYCLES':
        rnd.engine = 'CYCLES'
        notes.append("engine switched to Cycles")

    # ---- device -----------------------------------------------------------
    if st.use_gpu:
        try:
            cprefs = context.preferences.addons['cycles'].preferences
            types = [i.identifier for i in
                     cprefs.bl_rna.properties['compute_device_type'].enum_items]
            backend = next((b for b in ('METAL', 'OPTIX', 'CUDA', 'HIP', 'ONEAPI')
                            if b in types), None)
            if backend:
                cprefs.compute_device_type = backend
                cprefs.get_devices()
                for d in cprefs.devices:
                    d.use = (d.type != 'CPU')
                cy.device = 'GPU'
                notes.append(f"{backend} GPU")
            else:
                cy.device = 'CPU'
                notes.append("no GPU backend, using CPU")
        except Exception as exc:
            cy.device = 'CPU'
            notes.append(f"GPU setup failed ({exc}), using CPU")
    else:
        cy.device = 'CPU'

    # ---- resolution -------------------------------------------------------
    w, h = compute_resolution(p["long_edge"], st.aspect)
    rnd.resolution_x, rnd.resolution_y, rnd.resolution_percentage = w, h, 100

    # ---- sampling ---------------------------------------------------------
    cy.samples = p["samples"]
    cy.use_adaptive_sampling = True
    cy.adaptive_threshold = p["threshold"]
    cy.adaptive_min_samples = p["min_samples"]
    cy.time_limit = 0.0
    cy.use_light_tree = True
    cy.light_sampling_threshold = 0.005 if key != 'QUICK' else 0.01

    mx, df, gl, tr = p["bounces"]
    cy.max_bounces, cy.diffuse_bounces = mx, df
    cy.glossy_bounces, cy.transmission_bounces = gl, tr
    cy.transparent_max_bounces = mx
    cy.volume_bounces = 2 if key != 'QUICK' else 0

    cy.caustics_reflective = p["caustics"]
    cy.caustics_refractive = p["caustics"]
    cy.use_fast_gi = p["fast_gi"]
    if p["fast_gi"]:
        cy.ao_bounces_render = 1
        cy.ao_bounces = 1

    cy.sample_clamp_direct = 0.0
    cy.sample_clamp_indirect = 10.0
    cy.blur_glossy = 1.0
    cy.pixel_filter_type = 'BLACKMAN_HARRIS'
    cy.filter_width = 1.5

    # ---- denoising --------------------------------------------------------
    prof = p["denoise"]
    if prof == "off":
        cy.use_denoising = False
    else:
        cy.use_denoising = True
        if prof == "full":
            cy.denoising_input_passes = 'RGB_ALBEDO_NORMAL'
            cy.denoising_prefilter = 'ACCURATE'
            cy.denoising_quality = 'HIGH'
        else:
            cy.denoising_input_passes = 'RGB_ALBEDO'
            cy.denoising_prefilter = 'FAST'
            cy.denoising_quality = 'BALANCED'
        try:
            cy.denoising_use_gpu = (cy.device == 'GPU')
        except Exception:
            pass

    # ---- tiling / memory --------------------------------------------------
    cy.use_auto_tile = True
    cy.tile_size = 2048
    rnd.use_persistent_data = False
    rnd.use_motion_blur = False
    rnd.use_high_quality_normals = (key != 'QUICK')

    # ---- output -----------------------------------------------------------
    rnd.filepath = st.output_dir
    rnd.image_settings.file_format = p["fmt"]
    rnd.image_settings.color_mode = 'RGBA'
    rnd.image_settings.color_depth = p["depth"]
    if p["fmt"] == 'OPEN_EXR':
        try:
            rnd.image_settings.exr_codec = 'ZIP'
        except Exception:
            pass

    if report:
        report({'INFO'},
               f"{p['label']}: {w}x{h}, {p['samples']} samples, "
               f"denoise {prof}" + (" | " + ", ".join(notes) if notes else ""))
    return w, h, notes


class STUDIO_OT_apply_preset(Operator):
    bl_idname = "studio.apply_preset"
    bl_label = "Apply Preset"
    bl_description = "Apply the selected preset to this scene's render settings"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        apply_preset(context, context.scene.studio_render.preset, self.report)
        return {'FINISHED'}


class STUDIO_OT_apply_and_render(Operator):
    bl_idname = "studio.apply_and_render"
    bl_label = "Apply + Render"
    bl_description = "Apply the preset, then start the render"

    def execute(self, context):
        apply_preset(context, context.scene.studio_render.preset, self.report)
        bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Region render (modal, so the UI stays alive between tiles)
# ---------------------------------------------------------------------------

class STUDIO_OT_render_regions(Operator):
    bl_idname = "studio.render_regions"
    bl_label = "Render in Regions"
    bl_description = ("Render the frame as a grid of tiles and stitch them into "
                      "one image. Peak memory is set by a single tile")

    _timer = None
    _tiles = None
    _index = 0
    _paths = None
    _saved = None

    def _save_state(self, rnd):
        self._saved = dict(
            use_border=rnd.use_border, crop=rnd.use_crop_to_border,
            bx0=rnd.border_min_x, bx1=rnd.border_max_x,
            by0=rnd.border_min_y, by1=rnd.border_max_y,
            filepath=rnd.filepath, fmt=rnd.image_settings.file_format,
            depth=rnd.image_settings.color_depth,
        )

    def _restore_state(self, rnd):
        s = self._saved
        if not s:
            return
        rnd.use_border = s["use_border"]
        rnd.use_crop_to_border = s["crop"]
        rnd.border_min_x, rnd.border_max_x = s["bx0"], s["bx1"]
        rnd.border_min_y, rnd.border_max_y = s["by0"], s["by1"]
        rnd.filepath = s["filepath"]
        rnd.image_settings.file_format = s["fmt"]
        rnd.image_settings.color_depth = s["depth"]

    def invoke(self, context, event):
        sc = context.scene
        st = sc.studio_render
        rnd = sc.render

        apply_preset(context, st.preset, None)

        n = st.region_grid
        # Force divisibility so tile boundaries land on exact pixels.
        w, h = rnd.resolution_x, rnd.resolution_y
        w2, h2 = w - (w % n), h - (h % n)
        if (w2, h2) != (w, h):
            rnd.resolution_x, rnd.resolution_y = w2, h2
            self.report({'INFO'}, f"Resolution trimmed to {w2}x{h2} to divide by {n}")

        if rnd.resolution_x < n or rnd.resolution_y < n:
            self.report({'ERROR'}, "Resolution too small for that grid")
            return {'CANCELLED'}

        self._save_state(rnd)
        # EXR float out: stitching needs real pixel values, not display-encoded ones
        rnd.image_settings.file_format = 'OPEN_EXR'
        rnd.image_settings.color_depth = '32'
        rnd.use_border = True
        rnd.use_crop_to_border = True

        self._tiles = [(r, c) for r in range(n) for c in range(n)]
        self._index = 0
        self._paths = {}

        base = bpy.path.abspath(st.output_dir)
        try:
            os.makedirs(base, exist_ok=True)
        except OSError as exc:
            self.report({'ERROR'}, f"Cannot create {base}: {exc}")
            self._restore_state(rnd)
            return {'CANCELLED'}
        self._base = base

        wm = context.window_manager
        wm.progress_begin(0, len(self._tiles))
        self._timer = wm.event_timer_add(0.05, window=context.window)
        wm.modal_handler_add(self)
        self.report({'INFO'}, f"Region render started: {len(self._tiles)} tiles. ESC to cancel.")
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC':
            self.report({'WARNING'}, "Region render cancelled")
            return self._cleanup(context, cancelled=True)

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        sc = context.scene
        rnd = sc.render
        st = sc.studio_render
        n = st.region_grid

        if self._index >= len(self._tiles):
            try:
                out = self._stitch(context)
                self.report({'INFO'}, f"Stitched -> {out}")
            except Exception as exc:
                self.report({'ERROR'}, f"Stitch failed: {exc}")
                return self._cleanup(context, cancelled=True)
            return self._cleanup(context, cancelled=False)

        r, c = self._tiles[self._index]
        rnd.border_min_x, rnd.border_max_x = c / n, (c + 1) / n
        rnd.border_min_y, rnd.border_max_y = r / n, (r + 1) / n
        path = os.path.join(self._base, f"_tile_r{r}c{c}.exr")
        rnd.filepath = path[:-4]
        global _BUSY
        _BUSY = True
        try:
            bpy.ops.render.render(write_still=True)
        except Exception as exc:
            self.report({'ERROR'}, f"Tile r{r}c{c} failed: {exc}")
            return self._cleanup(context, cancelled=True)
        finally:
            _BUSY = False
        self._paths[(r, c)] = path
        self._index += 1
        context.window_manager.progress_update(self._index)
        return {'PASS_THROUGH'}

    def _stitch(self, context):
        import numpy as np
        sc = context.scene
        n = sc.studio_render.region_grid
        W, H = sc.render.resolution_x, sc.render.resolution_y
        tw, th = W // n, H // n

        out = np.zeros((H, W, 4), dtype=np.float32)
        for (r, c), p in self._paths.items():
            img = bpy.data.images.load(p)
            try:
                buf = np.empty(len(img.pixels), dtype=np.float32)
                img.pixels.foreach_get(buf)
                if buf.size != tw * th * 4:
                    raise RuntimeError(
                        f"tile r{r}c{c} is {buf.size // 4} px, expected {tw*th}")
                # Blender image rows run bottom-to-top, matching border_min_y
                out[r * th:(r + 1) * th, c * tw:(c + 1) * tw] = buf.reshape(th, tw, 4)
            finally:
                bpy.data.images.remove(img)

        name = "StudioStitched"
        if name in bpy.data.images:
            bpy.data.images.remove(bpy.data.images[name])
        final = bpy.data.images.new(name, W, H, alpha=True, float_buffer=True)
        final.pixels.foreach_set(out.reshape(-1))
        final.filepath_raw = os.path.join(self._base, "stitched.exr")
        final.file_format = 'OPEN_EXR'
        final.save()

        for p in self._paths.values():
            try:
                os.remove(p)
            except OSError:
                pass
        return final.filepath_raw

    def _cleanup(self, context, cancelled):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        try:
            wm.progress_end()
        except Exception:
            pass
        self._restore_state(context.scene.render)
        return {'CANCELLED'} if cancelled else {'FINISHED'}


# ---------------------------------------------------------------------------
#  Scene check
# ---------------------------------------------------------------------------

def scene_warnings(context):
    from mathutils import Vector
    sc = context.scene
    out = []

    # viewport shading vs render
    bad = 0
    for scr in bpy.data.screens:
        for area in scr.areas:
            if area.type != 'VIEW_3D':
                continue
            for sp in area.spaces:
                if sp.type == 'VIEW_3D':
                    sh = sp.shading
                    if not (sh.use_scene_lights and sh.use_scene_world
                            and sh.use_scene_lights_render and sh.use_scene_world_render):
                        bad += 1
    if bad:
        out.append(f"{bad} viewport(s) ignore scene lights/world - what you see is not what renders")

    # lights
    lamps = [o for o in sc.objects if o.type == 'LIGHT']
    if not lamps:
        out.append("no lights in the scene")
    else:
        tot = 0.0
        for o in lamps:
            d = o.matrix_world.translation.length
            if d > 0:
                tot += o.data.energy / (4 * math.pi * d * d)
        if tot < 3.0:
            out.append(f"lights deliver only ~{tot:.2f} W/m2 at the origin - likely underlit")

    # world texture
    w = sc.world
    if w and w.node_tree:
        for nd in w.node_tree.nodes:
            if nd.bl_idname == 'ShaderNodeTexEnvironment' and nd.image:
                img = nd.image
                if not img.packed_file and img.filepath:
                    if not os.path.exists(bpy.path.abspath(img.filepath)):
                        out.append(f"environment texture missing: {img.filepath}")
                    else:
                        out.append("environment texture is not packed - it can go missing")
                if os.path.splitext(img.filepath)[1].lower() not in ('.exr', '.hdr'):
                    out.append("environment is an LDR image, not an HDRI - weak as a light source")

    # transparent film into a format with no alpha
    if sc.render.film_transparent and sc.render.image_settings.file_format in (
            'JPEG', 'JPEG2000', 'BMP'):
        out.append("Film>Transparent is on but the output format has no alpha - "
                   "background will save as black")

    # camera inside a mesh with outward normals
    cam = sc.camera
    if cam:
        cpos = cam.matrix_world.translation
        for o in sc.objects:
            if o.type != 'MESH' or not o.data.polygons:
                continue
            cs = [o.matrix_world @ Vector(cnr) for cnr in o.bound_box]
            mn = [min(v[i] for v in cs) for i in range(3)]
            mx = [max(v[i] for v in cs) for i in range(3)]
            if all(mn[i] <= cpos[i] <= mx[i] for i in range(3)):
                mw3 = o.matrix_world.to_3x3()
                polys = o.data.polygons
                # sample rather than scan: a dense enclosure could be millions
                step = max(1, len(polys) // 500)
                sampled = outward = 0
                for i in range(0, len(polys), step):
                    poly = polys[i]
                    ctr = o.matrix_world @ poly.center
                    nrm = (mw3 @ poly.normal).normalized()
                    sampled += 1
                    if nrm.dot((cpos - ctr).normalized()) <= 0:
                        outward += 1
                if sampled and outward > sampled * 0.5:
                    out.append(f"camera is inside '{o.name}' and its normals face away - "
                               "you are rendering backfaces")
                break
    return out


# Warnings are cached per scene. scene_warnings() walks objects and samples
# polygons, which must never run from a panel's draw() -- draw is called on
# every redraw, and that would stall the viewport on a heavy model.
_WARN_CACHE = {}


class STUDIO_OT_check_scene(Operator):
    bl_idname = "studio.check_scene"
    bl_label = "Check Scene"
    bl_description = "Look for the usual reasons a render comes out dark or wrong"

    def execute(self, context):
        warns = scene_warnings(context)
        _WARN_CACHE[context.scene.name] = warns
        if not warns:
            self.report({'INFO'}, "Scene check passed - nothing obviously wrong")
        else:
            for wmsg in warns:
                self.report({'WARNING'}, wmsg)
            self.report({'WARNING'}, f"{len(warns)} issue(s) - see the Info log")
        return {'FINISHED'}


class STUDIO_OT_fix_viewport(Operator):
    bl_idname = "studio.fix_viewport"
    bl_label = "Match Viewport to Render"
    bl_description = ("Turn on scene lights and scene world in every 3D viewport, "
                      "so preview and final render agree")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = 0
        for scr in bpy.data.screens:
            for area in scr.areas:
                if area.type != 'VIEW_3D':
                    continue
                for sp in area.spaces:
                    if sp.type == 'VIEW_3D':
                        sh = sp.shading
                        sh.use_scene_lights = sh.use_scene_world = True
                        sh.use_scene_lights_render = sh.use_scene_world_render = True
                        n += 1
        if context.scene.name in _WARN_CACHE:
            _WARN_CACHE[context.scene.name] = scene_warnings(context)
        self.report({'INFO'}, f"{n} viewport(s) now use scene lighting")
        return {'FINISHED'}


class STUDIO_OT_calibrate(Operator):
    bl_idname = "studio.calibrate"
    bl_label = "Calibrate Timing"
    bl_description = ("Measure this machine on this scene with two quick small "
                      "renders, then extrapolate render times for every preset")

    def execute(self, context):
        global _BUSY
        sc = context.scene
        st = sc.studio_render
        key = st.preset
        p = PRESETS[key]
        rnd, cy = sc.render, sc.cycles

        apply_preset(context, key, None)

        saved = (rnd.resolution_x, rnd.resolution_y, rnd.resolution_percentage,
                 rnd.filepath, rnd.use_border, rnd.use_crop_to_border)
        rnd.use_border = False
        rnd.use_crop_to_border = False
        rnd.resolution_percentage = 100

        _BUSY = True
        try:
            # --- warm-up, discarded: pays kernel compilation on GPU ---------
            w, h = compute_resolution(64, st.aspect)
            rnd.resolution_x, rnd.resolution_y = max(w, 2), max(h, 2)
            bpy.ops.render.render(write_still=False)

            # --- two timed points, 4x apart in pixel count -------------------
            pts = []
            for edge in (128, 256):
                w, h = compute_resolution(edge, st.aspect)
                rnd.resolution_x, rnd.resolution_y = max(w, 2), max(h, 2)
                mp = rnd.resolution_x * rnd.resolution_y / 1_000_000.0
                t0 = time.perf_counter()
                bpy.ops.render.render(write_still=False)
                pts.append((mp, time.perf_counter() - t0))
        except Exception as exc:
            self.report({'ERROR'}, f"Calibration failed: {exc}")
            return {'CANCELLED'}
        finally:
            _BUSY = False
            (rnd.resolution_x, rnd.resolution_y, rnd.resolution_percentage,
             rnd.filepath, rnd.use_border, rnd.use_crop_to_border) = saved

        (mp0, t0_), (mp1, t1_) = pts
        if mp1 <= mp0 or t1_ <= t0_:
            self.report({'WARNING'},
                        "Calibration renders were too fast to separate - "
                        "estimate will be rough")
            b = max(t1_, 1e-6) / max(mp1, 1e-6)
            a = 0.0
        else:
            b = (t1_ - t0_) / (mp1 - mp0)
            a = max(0.0, t0_ - b * mp0)

        set_calib(sc, key, a, b, n=1)

        w, h = compute_resolution(p["long_edge"], st.aspect)
        full = a + b * (w * h / 1_000_000.0)
        self.report({'INFO'},
                    f"Calibrated {p['label']} on {cy.device}: "
                    f"{a:.1f}s + {b:.1f}s/MP  ->  full frame ~{fmt_time(full)}")
        return {'FINISHED'}


class STUDIO_OT_clear_calibration(Operator):
    bl_idname = "studio.clear_calibration"
    bl_label = "Clear Calibration"
    bl_description = "Forget all measured timings for this scene"

    def execute(self, context):
        context.scene.studio_render.calib_data = "{}"
        self.report({'INFO'}, "Calibration cleared")
        return {'FINISHED'}


# --- learn from real renders ------------------------------------------------
#  Every completed render is another data point. We keep the overhead term
#  from calibration and refine only the slope, blending so one odd render
#  (a background app stealing the GPU, say) cannot wreck the estimate.

@bpy.app.handlers.persistent
def _on_render_init(scene, *args):
    if _BUSY:
        return
    rnd = scene.render
    if rnd.use_border and rnd.use_crop_to_border:
        return                        # a region tile, not a whole frame
    pct = rnd.resolution_percentage / 100.0
    mp = (rnd.resolution_x * pct) * (rnd.resolution_y * pct) / 1_000_000.0
    _PENDING[scene.name] = (time.perf_counter(), mp, scene.studio_render.preset)


@bpy.app.handlers.persistent
def _on_render_complete(scene, *args):
    rec = _PENDING.pop(scene.name, None)
    if _BUSY or rec is None:
        return
    t0, mp, key = rec
    actual = time.perf_counter() - t0
    if mp <= 0 or actual <= 0:
        return
    c = get_calib(scene, key)
    if c:
        implied = max(0.0, (actual - c["a"]) / mp)
        n = min(int(c.get("n", 1)), 5)          # keep it responsive to change
        b = (c["b"] * n + implied) / (n + 1)
        set_calib(scene, key, c["a"], b, n=n + 1)
    else:
        set_calib(scene, key, 0.0, actual / mp, n=1)


@bpy.app.handlers.persistent
def _on_render_cancel(scene, *args):
    _PENDING.pop(scene.name, None)


# ---------------------------------------------------------------------------
#  Product studio: 3-point rig + seamless cyclorama backdrop
# ---------------------------------------------------------------------------
#  Everything is derived from the bounding box of the selected object(s), so
#  the rig is correct whether the part is 5 mm or 5 m.
#
#  The important part is the light POWER. Irradiance at the subject falls off
#  as E = P / (4*pi*d^2), and d scales with object size -- so power must scale
#  with the SQUARE of the rig radius. A rig built with fixed wattage is the
#  reason a scaled-up scene renders dark. We therefore solve for power from a
#  target irradiance instead of hard-coding watts.
#
#  KEY_IRRADIANCE was tuned by rendering an 18% grey reference sphere and
#  measuring where it lands after AgX -- not by eye.

STUDIO_COLLECTION = "Studio"
RIG_TAG = "studio_rig"
WM_TAG = "studio_watermark"

KEY_IRRADIANCE = 1.2      # W/m^2 from the key at the subject.
                          # Tuned by measurement: an 18% grey sphere under
                          # this rig lands at 0.52 display after AgX, where
                          # correctly-exposed mid-grey should sit. Swept
                          # 1.46-4.88 W/m2 total; 2.34 was the best fit.
#  (per-light power ratios now live in LIGHT_SETUPS above)


def _selected_bounds(context):
    """World-space bounds of selected objects, modifiers applied."""
    from mathutils import Vector
    deps = context.evaluated_depsgraph_get()
    objs = [o for o in context.selected_objects
            if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
    if not objs and context.active_object:
        objs = [context.active_object]
    pts = []
    for o in objs:
        try:
            oe = o.evaluated_get(deps)
            mw = oe.matrix_world
            pts.extend([mw @ Vector(c) for c in oe.bound_box])
        except Exception:
            mw = o.matrix_world
            pts.extend([mw @ Vector(c) for c in o.bound_box])
    if not pts:
        return None
    mn = Vector((min(p[i] for p in pts) for i in range(3)))
    mx = Vector((max(p[i] for p in pts) for i in range(3)))
    return mn, mx, (mn + mx) * 0.5, (mx - mn)


def _front_vector(context, center):
    """Unit XY vector pointing from the subject toward the viewer."""
    from mathutils import Vector
    cam = context.scene.camera
    if cam:
        v = cam.matrix_world.translation - center
        v.z = 0.0
        if v.length > 1e-6:
            return v.normalized()
    return Vector((0.0, -1.0, 0.0))


def _purge_rig(context):
    """Remove any rig this add-on previously built."""
    doomed = [o for o in bpy.data.objects if o.get(RIG_TAG)]
    # A watermark parented to a camera we are about to delete would be
    # orphaned mid-air at its camera-space offset. Take it with us.
    dset = set(doomed)
    for o in [o for o in bpy.data.objects if o.get(WM_TAG)]:
        if o.parent in dset:
            data = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if data is not None and data.users == 0:
                try:
                    bpy.data.curves.remove(data)
                except Exception:
                    pass
    for o in doomed:
        data = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        for coll in (bpy.data.lights, bpy.data.meshes, bpy.data.cameras):
            if data is not None and data in coll.values() and data.users == 0:
                try:
                    coll.remove(data)
                except Exception:
                    pass
    c = bpy.data.collections.get(STUDIO_COLLECTION)
    if c and not c.objects:
        bpy.data.collections.remove(c)
    return len(doomed)


def _studio_collection(context):
    c = bpy.data.collections.get(STUDIO_COLLECTION)
    if c is None:
        c = bpy.data.collections.new(STUDIO_COLLECTION)
        context.scene.collection.children.link(c)
    return c


def _mk_area_light(coll, name, power, size, loc, target, aspect=1.0):
    """aspect = height/width of the panel. >1 gives a tall strip, which is
    what draws a crisp linear edge highlight rather than a broad soft one."""
    ld = bpy.data.lights.new(name, type='AREA')
    if abs(aspect - 1.0) < 1e-6:
        ld.shape = 'SQUARE'
        ld.size = size
    else:
        ld.shape = 'RECTANGLE'
        ld.size = size
        ld.size_y = size * aspect
    ld.energy = power
    ob = bpy.data.objects.new(name, ld)
    ob.location = loc
    ob[RIG_TAG] = True
    coll.objects.link(ob)
    con = ob.constraints.new('TRACK_TO')
    con.target = target
    con.track_axis = 'TRACK_NEGATIVE_Z'
    con.up_axis = 'UP_Y'
    return ob


def _backdrop_rgb(st):
    if st.rig_backdrop_tone == 'CUSTOM':
        c = st.rig_backdrop_custom
        return (c[0], c[1], c[2], 1.0)
    v = BACKDROP_TONES[st.rig_backdrop_tone][1]
    return (v, v, v, 1.0)


def _build_backdrop(coll, center, floor_z, width, front_depth, back_depth,
                    fillet, height, rgb=(0.55, 0.55, 0.55, 1.0), smooth=True):
    """Seamless cyclorama, built from explicit metric dimensions.

    Profile (depth, height), front to back:
        floor  (-front_depth, 0) .. (back_depth, 0)
        fillet quarter arc of radius `fillet` up to (back_depth+fillet, fillet)
        wall   straight up to (back_depth+fillet, height)

    `back_depth` is a FLAT run behind the subject before the curve starts.
    Keeping it separate from the fillet radius is what lets the caller push
    the wall back far enough to clear the rim light -- with the two welded
    together, the only way to gain clearance was a silly-looking sweep.
    """
    import bmesh

    prof = [(-front_depth, 0.0), (back_depth, 0.0)]
    segs = 16
    for i in range(1, segs + 1):
        t = (math.pi / 2) * (i / segs)
        prof.append((back_depth + fillet * math.sin(t),
                     fillet * (1.0 - math.cos(t))))
    if height > fillet:
        prof.append((back_depth + fillet, height))

    bm = bmesh.new()
    rows = []
    for x in (-width * 0.5, width * 0.5):
        rows.append([bm.verts.new((x, d, h)) for d, h in prof])
    bm.verts.ensure_lookup_table()
    for i in range(len(prof) - 1):
        bm.faces.new((rows[0][i], rows[0][i + 1], rows[1][i + 1], rows[1][i]))
    bm.normal_update()

    me = bpy.data.meshes.new("Studio Backdrop")
    bm.to_mesh(me)
    bm.free()

    ob = bpy.data.objects.new("Studio Backdrop", me)
    ob[RIG_TAG] = True
    coll.objects.link(ob)
    ob.location = (center.x, center.y, floor_z)

    up_facing = sum(1 for pl in me.polygons if pl.normal.z > 0)
    if up_facing < len(me.polygons) * 0.5:
        me.flip_normals()

    if smooth:
        for pl in me.polygons:
            pl.use_smooth = True

    mat = bpy.data.materials.get("Studio Backdrop")
    if mat is None:
        mat = bpy.data.materials.new("Studio Backdrop")
        mat.use_nodes = True
    bsdf = next((n for n in mat.node_tree.nodes
                 if n.bl_idname == 'ShaderNodeBsdfPrincipled'), None)
    if bsdf:
        # set every build, so changing the tone actually takes effect
        bsdf.inputs["Base Color"].default_value = rgb
        bsdf.inputs["Roughness"].default_value = 0.5
        try:
            bsdf.inputs["Metallic"].default_value = 0.0
        except Exception:
            pass
    me.materials.append(mat)
    return ob


# ---------------------------------------------------------------------------
#  Turntable
# ---------------------------------------------------------------------------
#  The rig orbits; the product never moves.
#
#  The obvious approach -- spin the product -- goes wrong on CAD imports,
#  because Blender rotates about the object ORIGIN and an imported origin is
#  routinely nowhere near the geometry. The part would swing in an arc rather
#  than turn on the spot. Fixing that means re-parenting the user's model to a
#  helper, which rearranges their scene.
#
#  Orbiting the camera, lights and backdrop together as one group is exactly
#  equivalent in the camera's frame: relative geometry is all that a render
#  sees, so the product appears to rotate under lighting that is identical on
#  every frame, and the model is left completely alone.
#
#  Not "orbit the camera alone" -- that would swing the camera through the rim
#  light and the lighting would change completely over the loop.

TT_TAG = "studio_turntable"
TT_PARENT_KEY = "studio_tt_prev_parent"


def _action_fcurves(ob):
    """Every F-curve on an object's action.

    Blender 4.4 moved actions to layers and slots and 5.0 removed
    `Action.fcurves` outright, so reach them through the channelbag; the old
    attribute is kept as a fallback for 4.2/4.3.
    """
    ad = ob.animation_data
    act = ad.action if ad else None
    if act is None:
        return []
    if hasattr(act, "layers"):
        out = []
        slot = getattr(ad, "action_slot", None)
        for layer in act.layers:
            for strip in layer.strips:
                bag = None
                if slot is not None and hasattr(strip, "channelbag"):
                    bag = strip.channelbag(slot)
                if bag is None:
                    for b in getattr(strip, "channelbags", []):
                        out.extend(b.fcurves)
                    continue
                out.extend(bag.fcurves)
        if out:
            return out
    return list(getattr(act, "fcurves", []))


def _turntable_empty():
    for o in bpy.data.objects:
        if o.get(TT_TAG) == "pivot":
            return o
    return None


def _purge_turntable(context):
    """Unparent everything we adopted, restoring prior parents, then drop the
    pivot. Children keep their world transform."""
    pivot = _turntable_empty()
    if pivot is None:
        return 0
    context.view_layer.update()
    for o in list(pivot.children):
        world = o.matrix_world.copy()
        prev = o.get(TT_PARENT_KEY)
        o.parent = bpy.data.objects.get(prev) if prev else None
        if o.parent is not None:
            o.matrix_parent_inverse = o.parent.matrix_world.inverted()
        o.matrix_world = world
        try:
            del o[TT_PARENT_KEY]
        except Exception:
            pass
    bpy.data.objects.remove(pivot, do_unlink=True)
    return 1


class STUDIO_OT_build_turntable(Operator):
    bl_idname = "studio.build_turntable"
    bl_label = "Build Turntable"
    bl_description = ("Orbit the studio rig around the product so it appears to "
                      "spin under fixed lighting. Your model is not touched")
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        from mathutils import Vector, Matrix
        sc = context.scene
        st = sc.studio_render

        rig = [o for o in bpy.data.objects if o.get(RIG_TAG)]
        if not rig:
            self.report({'ERROR'}, "Build the product studio first")
            return {'CANCELLED'}

        _purge_turntable(context)
        context.view_layer.update()

        # Pivot at the tracking target if we have one, else the rig centroid.
        tgt = next((o for o in rig if o.type == 'EMPTY'), None)
        if tgt is not None:
            centre = tgt.matrix_world.translation.copy()
        else:
            pts = [o.matrix_world.translation for o in rig]
            centre = sum(pts, Vector()) / len(pts)
            centre.z = 0.0

        pivot = bpy.data.objects.new("Studio Turntable", None)
        pivot.empty_display_type = 'SPHERE'
        pivot[TT_TAG] = "pivot"
        pivot.location = centre
        _studio_collection(context).objects.link(pivot)
        context.view_layer.update()

        # Adopt the camera, lights and backdrop -- but NOT the tracking target,
        # which is parented to the product and must stay put so the lights keep
        # aiming at the same spot as they swing around it.
        cam = sc.camera
        movers = [o for o in rig if o is not tgt and o.type in {'LIGHT', 'MESH', 'CAMERA'}]
        if cam is not None and cam not in movers:
            movers.append(cam)

        adopted = 0
        for o in movers:
            if o is pivot or o.parent is pivot:
                continue
            world = o.matrix_world.copy()
            if o.parent is not None:
                o[TT_PARENT_KEY] = o.parent.name
            o.parent = pivot
            o.matrix_parent_inverse = pivot.matrix_world.inverted()
            o.matrix_world = world
            adopted += 1

        # --- animate -------------------------------------------------------
        n = max(2, int(st.tt_frames))
        turn = math.radians(360.0) * (-1.0 if st.tt_clockwise else 1.0)
        pivot.rotation_mode = 'XYZ'
        pivot.rotation_euler = (0.0, 0.0, 0.0)
        pivot.keyframe_insert("rotation_euler", index=2, frame=1)
        pivot.rotation_euler = (0.0, 0.0, turn)
        # Key the full turn one frame PAST the last rendered frame. Keying it
        # on frame n would render frame n identical to frame 1 and the loop
        # would stutter on a repeat.
        pivot.keyframe_insert("rotation_euler", index=2, frame=n + 1)
        pivot.rotation_euler = (0.0, 0.0, 0.0)

        for fc in _action_fcurves(pivot):
            fc.extrapolation = 'LINEAR'
            for kp in fc.keyframe_points:
                kp.interpolation = 'LINEAR'       # constant speed, no ease

        sc.frame_start = 1
        sc.frame_end = n
        sc.frame_current = 1

        if st.tt_set_output:
            sc.render.image_settings.file_format = 'PNG'
            sc.render.image_settings.color_mode = 'RGBA'
            sc.render.image_settings.color_depth = '16'
            base = st.output_dir.rstrip("/\\")
            sc.render.filepath = f"{base}/turntable_"

        secs = n / max(sc.render.fps, 1)
        self.report({'INFO'},
                    f"Turntable: {n} frames, {secs:.1f}s at {sc.render.fps}fps, "
                    f"{adopted} rig objects orbiting "
                    f"({'clockwise' if st.tt_clockwise else 'anticlockwise'})")
        return {'FINISHED'}


class STUDIO_OT_remove_turntable(Operator):
    bl_idname = "studio.remove_turntable"
    bl_label = "Remove Turntable"
    bl_description = "Stop the orbit and put the rig back where it was"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = _purge_turntable(context)
        self.report({'INFO'}, "Turntable removed" if n else "No turntable found")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Watermark
# ---------------------------------------------------------------------------
#  A text object parented to the camera, so it sits at a fixed spot in frame
#  however the camera moves. Rendered as pure emission and made invisible to
#  every ray type except camera, so it cannot light the product, cast a
#  shadow, or show up in a reflection.
#
#  The alternative is Blender's own Output > Metadata > Burn Into Image. That
#  needs no objects at all, but its position, font and size are fixed by
#  Blender and it is rasterised at UI resolution, which looks coarse on an 8K
#  plate. This draws at render resolution instead.


def _purge_watermark():
    gone = 0
    for o in [o for o in bpy.data.objects if o.get(WM_TAG)]:
        data = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if data is not None and data.users == 0:
            try:
                bpy.data.curves.remove(data)
            except Exception:
                pass
        gone += 1
    return gone


def _watermark_material(color, opacity, strength):
    mat = bpy.data.materials.get("Studio Watermark")
    if mat is None:
        mat = bpy.data.materials.new("Studio Watermark")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    emit = nt.nodes.new('ShaderNodeEmission')
    trans = nt.nodes.new('ShaderNodeBsdfTransparent')
    mix = nt.nodes.new('ShaderNodeMixShader')
    out.location = (300, 0); mix.location = (100, 0)
    emit.location = (-120, -100); trans.location = (-120, 100)
    emit.inputs["Color"].default_value = (color[0], color[1], color[2], 1.0)
    emit.inputs["Strength"].default_value = strength
    mix.inputs["Fac"].default_value = opacity
    nt.links.new(trans.outputs[0], mix.inputs[1])
    nt.links.new(emit.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    try:
        mat.blend_method = 'BLEND'
    except Exception:
        pass
    return mat


def _make_watermark(context, report=None):
    """Build (or rebuild) the watermark on the current active camera.

    Shared by the operator and by the rig builder, which recreates the
    watermark after a rebuild so it lands on the NEW camera instead of
    being orphaned on the one it just replaced.
    """
    if True:
        from mathutils import Matrix
        sc = context.scene
        st = sc.studio_render
        cam = sc.camera
        if cam is None or cam.type != 'CAMERA':
            if report:
                report({'ERROR'}, "No active camera")
            return False

        _purge_watermark()
        cd = cam.data

        # --- how big is the frame, at the depth we will sit at? -------------
        rx = max(sc.render.resolution_x, 1)
        ry = max(sc.render.resolution_y, 1)
        depth = max(cd.clip_start * 4.0, 1e-4)

        if cd.type == 'ORTHO':
            half_w = cd.ortho_scale * 0.5
            half_h = half_w * ry / rx
            if ry > rx:
                half_h = cd.ortho_scale * 0.5
                half_w = half_h * rx / ry
        else:
            sw = cd.sensor_width
            if rx >= ry:                       # AUTO sensor fit: long edge
                sen_h, sen_v = sw, sw * ry / rx
            else:
                sen_h, sen_v = sw * rx / ry, sw
            half_w = depth * (sen_h * 0.5) / cd.lens
            half_h = depth * (sen_v * 0.5) / cd.lens

        # --- build the text -------------------------------------------------
        curve = bpy.data.curves.new("Studio Watermark", type='FONT')
        curve.body = st.wm_text or " "
        curve.size = (half_h * 2.0) * st.wm_size
        curve.align_y = 'BOTTOM'
        if st.wm_font:
            try:
                path = bpy.path.abspath(st.wm_font)
                if os.path.exists(path):
                    curve.font = bpy.data.fonts.load(path, check_existing=True)
            except Exception as exc:
                if report:
                    report({'WARNING'}, f"Could not load font: {exc}")

        ob = bpy.data.objects.new("Studio Watermark", curve)
        ob[WM_TAG] = True
        coll = _studio_collection(context)
        coll.objects.link(ob)

        margin_x = half_w * 2.0 * st.wm_margin * (ry / rx if ry > rx else 1.0)
        margin_y = half_h * 2.0 * st.wm_margin
        corner = st.wm_corner
        if corner in {'BL', 'TL'}:
            x = -half_w + margin_x
            curve.align_x = 'LEFT'
        else:
            x = half_w - margin_x
            curve.align_x = 'RIGHT'
        if corner in {'BL', 'BR'}:
            y = -half_h + margin_y
        else:
            y = half_h - margin_y - curve.size

        ob.parent = cam
        # Identity parent-inverse on purpose: it makes `location` read as
        # CAMERA space, which is the whole point. Setting it to the camera's
        # inverse (the usual reflex) would make these numbers world space and
        # fling the text off frame.
        ob.matrix_parent_inverse = Matrix.Identity(4)
        ob.location = (x, y, -depth)
        ob.rotation_euler = (0.0, 0.0, 0.0)

        mat = _watermark_material(st.wm_color, st.wm_opacity, st.wm_strength)
        curve.materials.append(mat)

        # camera rays only: no lighting the product, no shadow, no reflection
        for attr in ("visible_diffuse", "visible_glossy", "visible_transmission",
                     "visible_volume_scatter", "visible_shadow"):
            try:
                setattr(ob, attr, False)
            except Exception:
                pass

        if report:
            report({'INFO'},
                   f"Watermark '{curve.body}' added at "
                   f"{dict(BL='bottom left', BR='bottom right', TL='top left', TR='top right')[corner]}")
        return True


class STUDIO_OT_add_watermark(Operator):
    bl_idname = "studio.add_watermark"
    bl_label = "Add Watermark"
    bl_description = ("Place a text watermark in a corner of frame, parented to "
                      "the active camera")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.scene.camera is not None

    def execute(self, context):
        return {'FINISHED'} if _make_watermark(context, self.report) else {'CANCELLED'}


class STUDIO_OT_remove_watermark(Operator):
    bl_idname = "studio.remove_watermark"
    bl_label = "Remove Watermark"
    bl_description = "Delete the watermark text object"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = _purge_watermark()
        self.report({'INFO'}, f"Removed {n} watermark(s)" if n else "No watermark found")
        return {'FINISHED'}


class STUDIO_OT_build_rig(Operator):
    bl_idname = "studio.build_rig"
    bl_label = "Build Product Studio"
    bl_description = ("Add a 3-point light rig and seamless backdrop sized to the "
                      "selected object(s). The lights track the selection")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None or bool(context.selected_objects)

    def execute(self, context):
        from mathutils import Vector, Matrix

        bounds = _selected_bounds(context)
        if bounds is None:
            self.report({'ERROR'}, "Select the product first")
            return {'CANCELLED'}
        mn, mx, center, size = bounds
        radius = max(size.x, size.y, size.z) * 0.5
        if radius < 1e-6:
            self.report({'ERROR'}, "Selection has no measurable size")
            return {'CANCELLED'}

        st = context.scene.studio_render
        had_watermark = any(o.get(WM_TAG) for o in bpy.data.objects)
        _purge_rig(context)
        coll = _studio_collection(context)

        # --- tracking target -------------------------------------------------
        # An empty at the visual centre, parented to the product. Tracking the
        # object directly would aim at its ORIGIN, which on CAD imports is
        # often nowhere near the part.
        tgt = bpy.data.objects.new("Studio Target", None)
        tgt.empty_display_type = 'PLAIN_AXES'
        tgt.empty_display_size = radius * 0.25
        tgt.location = center
        tgt[RIG_TAG] = True
        coll.objects.link(tgt)
        anchor_ob = context.active_object
        if anchor_ob is not None and anchor_ob.get(RIG_TAG) is None:
            tgt.parent = anchor_ob
            tgt.matrix_parent_inverse = anchor_ob.matrix_world.inverted()

        # --- rig geometry ----------------------------------------------------
        front = _front_vector(context, center)
        up = Vector((0.0, 0.0, 1.0))

        def place(az_deg, el_deg, dist_k):
            d = radius * dist_k
            v = (Matrix.Rotation(math.radians(az_deg), 4, 'Z') @ front).normalized()
            el = math.radians(el_deg)
            pos = center + (v * math.cos(el) + up * math.sin(el)) * d
            return pos, d

        bright = st.rig_brightness
        soft = st.rig_softness

        setup = LIGHT_SETUPS[st.rig_setup]
        bright = bright * setup["exposure"]

        specs = []
        for spec in setup["lights"]:
            name, az, el, dist_k, ratio, size_k = spec[:6]
            aspect = spec[6] if len(spec) > 6 else 1.0
            # A None angle means "follow the Key Angle / Key Height sliders".
            # Only setups that declare uses_key have them; the panel hides the
            # sliders otherwise so they cannot silently do nothing.
            if az is None:
                az = st.rig_key_azimuth if name.startswith("Key") \
                     else -st.rig_key_azimuth * 1.35
            if el is None:
                el = st.rig_key_elevation
            specs.append((name, az, el, dist_k, ratio, size_k, aspect))
        made = []
        pushed = trimmed = False
        for name, az, el, dist_k, ratio, size_k, aspect in specs:
            pos, d = place(az, el, dist_k)
            panel = radius * size_k * soft
            # Worst-case reach from a panel's centre is half its diagonal,
            # whatever way the track-to constraint ends up turning it.
            reach = 0.5 * math.hypot(panel, panel * aspect)
            floor_min = mn.z + reach + radius * 0.05

            if pos.z < floor_min:
                # The panel would cut through the floor. Push the light OUT
                # along the same bearing rather than tilting it: azimuth and
                # elevation stay exactly as set, and because power is solved
                # from d^2 the exposure is unchanged by the move.
                sin_el = math.sin(math.radians(el))
                max_d = d * 1.8          # don't exile the light to fix a panel
                if sin_el > 0.15:
                    d_need = (floor_min - center.z) / sin_el
                    if d_need > d:
                        pos, d = place(az, el, min(d_need, max_d) / radius)
                        pushed = True
                else:
                    # Too shallow to fix by distance; lift it instead.
                    pos = pos.copy()
                    pos.z = floor_min
                    d = (pos - center).length
                    pushed = True

                # Still overhanging? Trim the panel rather than pushing it
                # further. Past a point, moving a big softbox away makes it
                # angularly SMALLER -- the opposite of what Softness is for.
                allowed = pos.z - mn.z - radius * 0.05
                if 0.0 < allowed < reach:
                    panel *= allowed / reach        # shrink both dims together
                    reach = allowed
                    trimmed = True

            # P = E * 4*pi*d^2 -- power scales with the SQUARE of distance
            power = KEY_IRRADIANCE * bright * ratio * 4.0 * math.pi * d * d
            ob = _mk_area_light(coll, f"Studio {name}", power, panel, pos, tgt,
                                aspect)
            made.append((name, power, d, pos, reach))

        # --- backdrop ---------------------------------------------------------
        #  Sized from where the lights ACTUALLY ended up, not from radius
        #  alone. A panel is a square of side `size`, so its worst-case reach
        #  from its centre is half the diagonal whatever its orientation.
        expanded = []
        if st.rig_backdrop:
            ang = math.atan2(front.y, front.x) + math.pi / 2
            origin = Vector((center.x, center.y, mn.z))
            ax = Vector((-front.y, front.x, 0.0))     # backdrop local +X
            ay = Vector((-front.x, -front.y, 0.0))    # backdrop local +Y (away from camera)

            clear = radius * 0.5                      # breathing room
            need_back = need_half_w = need_h = 0.0
            for _n, _pw, _d, lpos, reach in made:
                # Use the positions computed above. Reading matrix_world here
                # would be stale -- the depsgraph has not evaluated the new
                # objects yet, so it still reports the origin.
                v = lpos - origin
                need_back = max(need_back, v.dot(ay) + reach + clear)
                need_half_w = max(need_half_w, abs(v.dot(ax)) + reach + clear)
                need_h = max(need_h, v.z + reach + clear)

            want_w = radius * st.rig_backdrop_width
            want_h = radius * st.rig_backdrop_height
            fillet = radius * st.rig_backdrop_fillet
            front_depth = radius * st.rig_backdrop_width * 0.5
            back_depth = radius * 1.0

            if need_half_w * 2.0 > want_w:
                want_w = need_half_w * 2.0
                expanded.append("width")
            if need_h > want_h:
                want_h = need_h
                expanded.append("height")
            if need_back > back_depth:
                back_depth = need_back
                expanded.append("depth")

            back = _build_backdrop(coll, center, mn.z, want_w, front_depth,
                                   back_depth, fillet, want_h,
                                   _backdrop_rgb(st), True)
            back.rotation_euler = (0.0, 0.0, ang)

        # --- optional camera ---------------------------------------------------
        if st.rig_add_camera:
            cd = bpy.data.cameras.new("Studio Camera")
            cd.lens = 85.0                      # long enough to avoid CAD distortion
            # Clip planes MUST scale with the subject. A 10 mm part sits well
            # inside Blender's default 0.1 m near plane and renders as nothing.
            cd.clip_start = max(radius * 0.01, 1e-5)
            cd.clip_end = max(radius * 1000.0, 100.0)
            cam = bpy.data.objects.new("Studio Camera", cd)
            cam[RIG_TAG] = True
            coll.objects.link(cam)
            # Framing must respect BOTH axes. On a landscape frame the vertical
            # FOV is the narrower one, so sizing off sensor_width alone crops
            # tall parts. We project the real bounding-box corners into the
            # camera basis and solve for the distance that fits every one.
            rx = context.scene.render.resolution_x or 1
            ry = context.scene.render.resolution_y or 1
            sw = cd.sensor_width
            if rx >= ry:                      # Blender AUTO fit: long edge uses sensor_width
                sen_h, sen_v = sw, sw * ry / rx
            else:
                sen_h, sen_v = sw * rx / ry, sw
            tan_h = max(math.tan(math.atan(sen_h * 0.5 / cd.lens)), 1e-6)
            tan_v = max(math.tan(math.atan(sen_v * 0.5 / cd.lens)), 1e-6)

            # Camera height is part of the look: a high angle shows the top
            # face, which on a glossy part mirrors the lights straight back.
            el = math.radians(setup.get("cam_elevation", 12.0))
            offset = (front * math.cos(el) + up * math.sin(el)).normalized()
            fwd = -offset                     # camera looks this way
            right = fwd.cross(up)
            if right.length < 1e-6:
                right = Vector((1.0, 0.0, 0.0))
            right.normalize()
            cup = right.cross(fwd).normalized()

            need = 0.0
            for cx in (mn.x, mx.x):
                for cy in (mn.y, mx.y):
                    for cz in (mn.z, mx.z):
                        v = Vector((cx, cy, cz)) - center
                        depth = v.dot(fwd)
                        need = max(need,
                                   abs(v.dot(right)) / tan_h - depth,
                                   abs(v.dot(cup)) / tan_v - depth)
            dist = max(need * 1.12, radius * 1.5)
            cam.location = center + offset * dist
            con = cam.constraints.new('TRACK_TO')
            con.target = tgt
            con.track_axis = 'TRACK_NEGATIVE_Z'
            con.up_axis = 'UP_Y'
            context.scene.camera = cam

        # Re-apply the watermark onto whatever camera we now have.
        if had_watermark:
            _make_watermark(context, None)

        total_E = KEY_IRRADIANCE * bright * sum(l[4] for l in setup["lights"])
        msg = (f"{setup['label']} rig, radius {radius:.3g}: "
               + ", ".join(f"{n} {p:.0f}W @ {d:.3g}" for n, p, d, _q, _r in made)
               + f"  (~{total_E:.1f} W/m2 at subject)")
        if pushed:
            msg += " | some lights moved out to clear the floor (exposure unchanged)"
        if trimmed:
            msg += " | a panel was trimmed to fit above the floor"
        if expanded:
            msg += f" | backdrop {'/'.join(expanded)} enlarged to clear the lights"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class STUDIO_OT_remove_rig(Operator):
    bl_idname = "studio.remove_rig"
    bl_label = "Remove Studio"
    bl_description = "Delete the lights, backdrop and target this add-on created"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        n = _purge_rig(context)
        self.report({'INFO'}, f"Removed {n} studio object(s)" if n else "No studio rig found")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
#  Panel
# ---------------------------------------------------------------------------

class STUDIO_PT_render_presets(Panel):
    bl_label = "Studio Render"
    bl_idname = "STUDIO_PT_render_presets"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Studio"

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        st = sc.studio_render
        pr = prefs(context)
        p = PRESETS[st.preset]

        col = layout.column(align=True)
        col.prop(st, "preset", expand=True)

        box = layout.box()
        box.scale_y = 0.8
        for line in _wrap(p["blurb"], 34):
            box.label(text=line)

        row = layout.row(align=True)
        row.prop(st, "aspect", expand=True)

        w, h = compute_resolution(p["long_edge"], st.aspect)
        mp = w * h / 1_000_000.0

        info = layout.box()
        c = info.column(align=True)
        c.label(text=f"{w} x {h}   ({mp:.1f} MP)", icon='IMAGE_DATA')
        c.label(text=f"{p['samples']} samples, denoise: {p['denoise']}")

        if st.use_region:
            eff = mp / (st.region_grid ** 2)
            lo, hi = estimate_gb(eff, "off", pr.scene_overhead)
            c.label(text=f"per tile: {eff:.1f} MP", icon='MESH_GRID')
        else:
            lo, hi = estimate_gb(mp, p["denoise"], pr.scene_overhead)

        over = hi > pr.memory_budget
        crit = lo > pr.memory_budget
        icon = 'ERROR' if crit else ('INFO' if over else 'CHECKMARK')
        c.label(text=f"est. {lo:.1f}-{hi:.1f} GB / {pr.memory_budget:.0f} GB", icon=icon)
        if crit:
            c.label(text="Over budget - use regions", icon='BLANK1')
        elif over:
            c.label(text="Marginal - watch memory", icon='BLANK1')

        # ---- time estimate -------------------------------------------------
        cal = get_calib(sc, st.preset)
        if cal is None:
            c.label(text="time: not calibrated", icon='TIME')
        elif st.use_region:
            n2 = st.region_grid ** 2
            per = cal["a"] + cal["b"] * (mp / n2)
            c.label(text=f"time: ~{fmt_time(per * n2)} ({n2} tiles)", icon='TIME')
        else:
            c.label(text=f"time: ~{fmt_time(cal['a'] + cal['b'] * mp)}", icon='TIME')

        layout.prop(st, "use_gpu")
        layout.prop(st, "output_dir")

        layout.separator()
        sub = layout.column(align=True)
        sub.prop(st, "use_region")
        if st.use_region:
            sub.prop(st, "region_grid")

        layout.separator()
        row = layout.row(align=True)
        cal = get_calib(sc, st.preset)
        row.operator("studio.calibrate",
                     text="Calibrate" if cal is None else "Recalibrate",
                     icon='TIME')
        if cal is not None:
            row.operator("studio.clear_calibration", text="", icon='X')
        if cal is not None:
            sub = layout.row()
            sub.scale_y = 0.7
            sub.label(text=f"measured on {sc.cycles.device}, "
                           f"{int(cal.get('n', 1))} sample(s)")

        layout.separator()
        col = layout.column(align=True)
        col.scale_y = 1.2
        col.operator("studio.apply_preset", icon='CHECKMARK')
        if st.use_region:
            col.operator("studio.render_regions", icon='RENDER_STILL')
        else:
            col.operator("studio.apply_and_render", icon='RENDER_STILL')


class STUDIO_PT_product_studio(Panel):
    bl_label = "Product Studio"
    bl_idname = "STUDIO_PT_product_studio"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Studio"
    bl_parent_id = "STUDIO_PT_render_presets"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        st = context.scene.studio_render

        sel = [o for o in context.selected_objects
               if o.type in {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}]
        if sel:
            names = ", ".join(o.name for o in sel[:2])
            if len(sel) > 2:
                names += f" +{len(sel) - 2}"
            layout.label(text=f"Target: {names}", icon='OBJECT_DATA')
        else:
            layout.label(text="Select your product first", icon='ERROR')

        setup = LIGHT_SETUPS[st.rig_setup]
        layout.prop(st, "rig_setup", text="")
        box = layout.box()
        box.scale_y = 0.75
        for line in _wrap(setup["blurb"], 34):
            box.label(text=line)
        box.label(text=f"{len(setup['lights'])} lights: "
                       + ", ".join(l[0] for l in setup["lights"]))

        col = layout.column(align=True)
        col.prop(st, "rig_brightness")
        col.prop(st, "rig_softness")
        if setup["uses_key"]:
            col.prop(st, "rig_key_azimuth")
            col.prop(st, "rig_key_elevation")

        layout.prop(st, "rig_backdrop")
        if st.rig_backdrop:
            sub = layout.column(align=True)
            sub.prop(st, "rig_backdrop_tone", text="")
            if st.rig_backdrop_tone == 'CUSTOM':
                sub.prop(st, "rig_backdrop_custom", text="")
            sub.separator()
            sub.prop(st, "rig_backdrop_width")
            sub.prop(st, "rig_backdrop_height")
            sub.prop(st, "rig_backdrop_fillet")
            if st.rig_backdrop_tone in {'DARK', 'BLACK'}:
                r = layout.row()
                r.scale_y = 0.7
                r.label(text="Dark sweep = little bounce fill", icon='INFO')
        layout.prop(st, "rig_add_camera")

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("studio.build_rig", icon='LIGHT_AREA')
        row.operator("studio.remove_rig", text="", icon='TRASH')


class STUDIO_PT_turntable(Panel):
    bl_label = "Turntable"
    bl_idname = "STUDIO_PT_turntable"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Studio"
    bl_parent_id = "STUDIO_PT_render_presets"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        st = sc.studio_render
        live = _turntable_empty() is not None

        if not any(o.get(RIG_TAG) for o in bpy.data.objects):
            layout.label(text="Build the product studio first", icon='ERROR')

        col = layout.column(align=True)
        col.prop(st, "tt_frames")
        col.prop(st, "tt_clockwise")
        col.prop(st, "tt_set_output")

        n = max(2, st.tt_frames)
        fps = max(sc.render.fps, 1)
        box = layout.box()
        c = box.column(align=True)
        c.scale_y = 0.85
        c.label(text=f"{n} frames = {n / fps:.1f}s at {fps}fps", icon='TIME')

        # Per-frame time comes from the render-preset calibration, so the
        # total is only shown once that preset has been calibrated.
        cal = get_calib(sc, st.preset)
        p = PRESETS[st.preset]
        w, h = compute_resolution(p["long_edge"], st.aspect)
        if cal is not None:
            per = cal["a"] + cal["b"] * (w * h / 1_000_000.0)
            c.label(text=f"~{fmt_time(per * n)} total at {PRESETS[st.preset]['label']}",
                    icon='RENDER_ANIMATION')
        else:
            c.label(text="Calibrate for a total time estimate", icon='QUESTION')

        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("studio.build_turntable",
                     text="Rebuild Turntable" if live else "Build Turntable",
                     icon='FILE_REFRESH' if live else 'DRIVER_ROTATIONAL_DIFFERENCE')
        row.operator("studio.remove_turntable", text="", icon='TRASH')

        if live:
            r = layout.row()
            r.scale_y = 0.7
            r.label(text="Render with Ctrl+F12 (animation)", icon='INFO')


class STUDIO_PT_watermark(Panel):
    bl_label = "Watermark"
    bl_idname = "STUDIO_PT_watermark"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Studio"
    bl_parent_id = "STUDIO_PT_render_presets"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        st = context.scene.studio_render
        existing = [o for o in bpy.data.objects if o.get(WM_TAG)]

        if context.scene.camera is None:
            layout.label(text="No active camera", icon='ERROR')

        layout.prop(st, "wm_text", text="")
        row = layout.row(align=True)
        row.prop(st, "wm_corner", expand=True)
        col = layout.column(align=True)
        col.prop(st, "wm_size")
        col.prop(st, "wm_margin")
        col.prop(st, "wm_opacity")
        col.prop(st, "wm_strength")
        layout.prop(st, "wm_color", text="")
        layout.prop(st, "wm_font", text="")

        if existing:
            r = layout.row()
            r.scale_y = 0.7
            r.label(text="Re-apply after changing settings", icon='INFO')
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.operator("studio.add_watermark",
                     text="Update Watermark" if existing else "Add Watermark",
                     icon='FONT_DATA')
        row.operator("studio.remove_watermark", text="", icon='TRASH')


class STUDIO_PT_scene_check(Panel):
    bl_label = "Scene Check"
    bl_idname = "STUDIO_PT_scene_check"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Studio"
    bl_parent_id = "STUDIO_PT_render_presets"
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("studio.check_scene", icon='VIEWZOOM')
        col.operator("studio.fix_viewport", icon='SHADING_RENDERED')
        warns = _WARN_CACHE.get(context.scene.name)
        if warns is None:
            self.layout.label(text="Not checked yet", icon='INFO')
        elif not warns:
            self.layout.label(text="No issues found", icon='CHECKMARK')
        else:
            box = self.layout.box()
            box.scale_y = 0.75
            box.label(text=f"{len(warns)} issue(s):", icon='ERROR')
            for wmsg in warns:
                for line in _wrap(wmsg, 32):
                    box.label(text=line)


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 > width:
            lines.append(cur)
            cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


# ---------------------------------------------------------------------------

classes = (
    StudioRenderSettings,
    StudioRenderPrefs,
    STUDIO_OT_apply_preset,
    STUDIO_OT_apply_and_render,
    STUDIO_OT_render_regions,
    STUDIO_OT_check_scene,
    STUDIO_OT_fix_viewport,
    STUDIO_OT_calibrate,
    STUDIO_OT_clear_calibration,
    STUDIO_OT_build_rig,
    STUDIO_OT_remove_rig,
    STUDIO_OT_add_watermark,
    STUDIO_OT_remove_watermark,
    STUDIO_OT_build_turntable,
    STUDIO_OT_remove_turntable,
    STUDIO_PT_render_presets,
    STUDIO_PT_product_studio,
    STUDIO_PT_turntable,
    STUDIO_PT_watermark,
    STUDIO_PT_scene_check,
)


_HANDLERS = (
    ("render_init", _on_render_init),
    ("render_complete", _on_render_complete),
    ("render_cancel", _on_render_cancel),
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.studio_render = PointerProperty(type=StudioRenderSettings)
    for name, fn in _HANDLERS:
        lst = getattr(bpy.app.handlers, name)
        if fn not in lst:
            lst.append(fn)


def unregister():
    for name, fn in _HANDLERS:
        lst = getattr(bpy.app.handlers, name)
        if fn in lst:
            lst.remove(fn)
    _WARN_CACHE.clear()
    _PENDING.clear()
    del bpy.types.Scene.studio_render
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
