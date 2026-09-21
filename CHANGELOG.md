# Changelog

## 1.7.0

- **Animation output.** MP4 (H.264 and H.265), WebM/VP9, QuickTime ProRes,
  lossless MKV/FFV1 and PNG sequence, with a quality and fps control. The
  turntable and the resolution presets both write through it, so choosing a
  video format once is enough.
- **Fixed the crash when the scene output was already set to video.** Blender
  4.5/5.x filter `file_format` by a new `media_type`, so while the scene was
  pointed at video the enum held only `FFMPEG` and the add-on's attempts to
  set `PNG` or `OPEN_EXR` raised `TypeError`. Build Turntable, Apply Preset
  and region rendering all hit this. They now set the media type first.
- Alpha is requested only from codecs that have it. H.264, H.265 and ProRes
  advertise RGB only, so the old unconditional `RGBA` would have raised on
  them too; WebM and FFV1 do keep their alpha.
- Scene Check warns about a video output with a single-frame range, region
  rendering that cannot produce a video, and transparent film going into a
  codec with no alpha.

## 1.6.1

- Declare the `files` permission and a copyright holder in the manifest, as the
  Blender Extensions Platform requires for an add-on that writes render tiles
  and reads a font file.
- Removed an unused import and an unused local.

## 1.6.0

- **Turntable.** The rig orbits the product for a seamless 360° loop; the model
  itself never moves, so CAD imports with off-centre origins turn on the spot
  rather than swinging in an arc. Total sequence time shown when calibrated.
- Fixed F-curve access for Blender 4.4+ / 5.0, where actions moved to layers
  and slots and `Action.fcurves` was removed.

## 1.5.0

- **Watermark.** Text in any corner, parented to the camera and drawn at render
  resolution. Invisible to every ray type except camera, so it cannot light the
  product, cast a shadow or appear in a reflection.

## 1.4.0

- **Silhouette Rim** setup — narrow strip lights rake the edges so the outline
  glows and the body stays dark. Adds rectangular panel support and per-setup
  camera elevation.

## 1.3.0

- Six lighting setups and five neutral backdrop tones, each setup carrying an
  exposure correction solved against an 18% grey sphere so switching changes
  the look and not the brightness.

## 1.2.1

- Backdrop is sized from actual light positions, so panels can no longer cut
  through the sweep. Lights that would clip the floor are pushed outward along
  the same bearing, leaving exposure unchanged.

## 1.2.0

- **Product Studio.** Three-point rig and seamless cyclorama sized from the
  selection, with light power solved from target irradiance so the rig is
  correct at any object scale.

## 1.1.0

- Measured render-time estimates with per-preset, per-device calibration that
  refines itself after every render.

## 1.0.0

- Initial release: Quick / 2K / 4K / 8K render presets, region rendering with
  stitching, and Scene Check.
