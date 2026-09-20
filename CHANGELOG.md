# Changelog

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
