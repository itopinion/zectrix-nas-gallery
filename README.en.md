# ZECTRIX NAS Gallery

A separately installed ZECTRIX Hub app that picks portrait photos from a mounted NAS folder, fills the NOTE4's 400×300 screen in sixteen shades of gray, and adds the capture date at the bottom right.

The current release requires **macOS 15 or later on an Apple Silicon Mac**. The NAS provides the photo share; the player runs on the Mac. The install ZIP includes its Python/Pillow runtime and native portrait helper.

[Latest release](https://github.com/itopinion/zectrix-nas-gallery/releases/latest) · [中文](README.md) · [Hub contract](docs/HUB_CONTRACT.md)

## Requirements

- A NAS photo share mounted and readable in Finder.
- A Hub build with the **2026-10-17 image Runtime extension**, implementing **`POST /runtime/image` with Gray16 support**. `GET /runtime/status` must advertise `features.image.formats` containing `gray16`. A Hub version string such as `0.1.0` alone does not prove compatibility; older layout-only Hub builds need an update.
- The compatible image Runtime build has not yet been published as a public Hub release. The [Hub repository](https://github.com/itopinion/zectrix-hub) is private and requires access; publishing this app does not make every existing public Hub installer compatible.
- A NOTE4 in local push mode, connected through Hub on the same reachable LAN. Use the matched SDK/firmware and palette protocol, following the firmware installation instructions supplied by ZECTRIX.

## Install

1. Install the `macos-aarch64` ZIP using Hub's application installer.
2. Activate **NAS 相册**, then open its management page.
3. Enter the mounted photo folder's absolute local path. Use the app's default small-image cache or choose another local directory outside the source folder.
4. Save. The app indexes paths in the background, verifies candidates and starts playback.

Defaults are a **five-minute interval** and **six prepared 400×300 frames**. The settings page controls the photo folder, cache folder, interval, cache limit, prefetch count and autoplay.

| Short press | Action |
| --- | --- |
| Up | Previous displayed photo |
| Down | Random next photo |
| Confirm | Pause/resume autoplay |

The web page provides the same playback controls and a rescan action. Hub owns the device connection and app leases; this application does not open a second connection to the device.

## Portraits, dates and cache

Apple Vision runs locally to detect human faces. Camera metadata and screenshot markers are checked before display. The strict policy can skip valid photos with stripped metadata, small faces or low-confidence detections. It is not an authenticity detector.

Frames are cropped around the face to fill 400×300. Dates prefer original capture metadata and retain the photo's local calendar day. A camera-native digitization date is used only when justified; an explicit, unambiguous filename date can supplement missing metadata. Filesystem modification time and generic TIFF edit time are not substituted. Photos without a reliable date are skipped.

Only processed 400×300 frames are kept in the persistent image cache after device completion reports. Unshown prepared frames remain temporary and are removed on exit. The cache is bounded and older frames are evicted. Changing the photo library or cache directory clears the app-owned old small-image cache and library index before starting the new album. Original photos stay on the NAS; local app data contains settings, path metadata, verification results and display history.

When the NAS is unavailable, prepared or cached frames can still be used. A first run with an empty cache needs access to the photo source. The Mac, Hub and app must stay running. This package is not a Synology DSM/Linux executable.

Prefetching reduces source-read and processing delays. It does not remove the e-paper panel's refresh time. A completion report is the device's reported result; optical behavior is verified separately on hardware.

## Build

On an Apple Silicon Mac with Xcode Command Line Tools:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
PYTHON=.venv/bin/python ./scripts/build-macos.sh
```

Outputs are the Hub install ZIP, its SHA-256, the generated manifest and release metadata under `dist/`. The ZIP contains only `app.json` and the self-contained `bin/zectrix-nas-gallery` executable. The package script creates real file hashes and verifies Hub's size limits.

Do not commit user settings, private NAS paths/indexes, photos, caches, logs, device IDs, credentials or firmware. Runtime data belongs in the data directory supplied by Hub.

## License

[MIT](LICENSE) · [Third-party notices](THIRD_PARTY_NOTICES.md)
