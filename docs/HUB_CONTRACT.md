# Hub application contract

Application ID: `zectrix-nas-gallery`. Manifest API version: `1`. Capabilities: `display`, `keys`. The first release is `macos-aarch64` only, requiring macOS 15 or later. The native helper is built with an explicit `arm64-apple-macosx15.0` deployment target to match the bundled Python framework minimum.

## Process and package

Hub starts `bin/zectrix-nas-gallery` and supplies `ZECTRIX_HUB_URL`, `ZECTRIX_APP_TOKEN`, `ZECTRIX_APP_ID`, `ZECTRIX_APP_ROOT`, `ZECTRIX_APP_DATA` and an allocated local HTTP port. The app prefers `ZECTRIX_APP_PORT`, with `ZECTRIX_VOCAB_PORT` as the current Hub v1 fallback.

The local web server binds only to `127.0.0.1`. `GET /api/boot` must respond while background indexing and prefetching are still starting, allowing Hub's readiness probe to complete. User data lives under `ZECTRIX_APP_DATA`, separately from the installed version payload.

The release is a self-contained PyInstaller executable with the web assets and native Vision helper embedded. The helper is copied into the private data directory after verifying its SHA-256 against the bundled source, then given mode `0700`. It is not executed directly from a non-executable install payload location.

## Required image feature

This app needs the 2026-10-17-compatible Hub build with these additive Runtime features. Older Hub builds with the same package version string can lack them.

`GET /runtime/status` returns the foreground lease and:

```json
{
  "active": true,
  "lease": 1,
  "device": {"connected": true},
  "features": {"image": {"formats": ["mono", "gray4", "gray16"]}}
}
```

To show a frame, the backend sends `POST /runtime/image` with the PNG body and headers:

```text
Authorization: Bearer <per-process Hub app token>
Content-Type: image/png
X-Lease: <current foreground lease>
X-Image-Format: gray16
```

The input is a 400×300 continuous-tone PNG. The Hub SDK performs palette-matched Gray16 conversion and owns the device connection. App code does not open a UDP connection to the device. App switching invalidates stale leases; inactive apps can prepare data but cannot display it.

A successful completion response includes:

```json
{"device_reported": true, "result": "Ok(())", "elapsed_ms": 2500}
```

A rejection before submission uses `submitted: false`, allowing the frame to wait for foreground activation. An unknown result after possible submission must not cause automatic replay. A device completion report is not independent optical verification.

Keys arrive through `GET /runtime/events?after=<cursor>`. The app validates the event lease before dispatch and coalesces navigation while a frame is outstanding.

## Local management API

The browser receives its own per-process UI token from `/api/boot`. It never receives `ZECTRIX_APP_TOKEN`. Mutations carry `X-App-Token` and are restricted to the loopback Host/Origin.

| Route | Purpose |
| --- | --- |
| `GET /api/boot` | `{token, version}`; readiness probe and local UI session |
| `GET /api/status` | `{config, player, runtime}` |
| `POST /api/config` | Save local settings; requires UI token |
| `POST /api/control` | `{command: previous\|next\|pause\|resume\|rescan}`; requires UI token |
| `GET /screen.png` | Current processed 400×300 frame |

Settings fields are `photo_root`, `cache_dir`, `interval_seconds`, `cache_max_mb`, `prefetch_frames` and `autoplay`. They are user data and are excluded from the release payload and public repository.

`player` reports catalog counts, date stamp metadata, cache usage, prefetch depth, next change time and the current frame. `runtime` reports `active`, `connected`, `compatible` and an optional user-facing error. The browser uses these fields to distinguish a missing source directory, an inactive app, a disconnected device and an incompatible Hub.

## Distribution boundaries

The manifest lists the self-contained executable's actual SHA-256. The release ZIP contains only the manifest and executable; no private configuration, photos, source indexes, caches, logs, tokens, firmware or device IDs are included. This repository does not vendor the Hub SDK. Building another platform requires a separate implementation of the local portrait helper and an independently tested package.
