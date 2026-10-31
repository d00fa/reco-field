# Third-Party Notices

Reco Field is distributed as part of the AGPL-3.0-only Reco source tree. See
the repository `LICENSE` for the complete license.

| Component | Version | License | Source |
|---|---|---|---|
| Reco video-stitcher | `cd3bf434b92c678a5585cd9be2330eda49782a8a` / 0.5.4 | AGPL-3.0-only | https://github.com/reco-project/video-stitcher |
| MediaMTX | 1.21.3 | MIT | https://github.com/bluenviron/mediamtx |
| hls.js | 1.7.4 | Apache-2.0 | https://github.com/video-dev/hls.js |
| FastAPI | 0.162.0 | MIT | https://github.com/fastapi/fastapi |
| Uvicorn | 0.54.0 | BSD-3-Clause | https://github.com/encode/uvicorn |
| python-qrcode | 8.2 | BSD-3-Clause | https://github.com/lincolnloop/python-qrcode |
| PyPNG | 0.20220715.0 | MIT | https://gitlab.com/drj11/pypng |
| OpenCV | 4.8.0 (JetPack system package) | Apache-2.0 | https://github.com/opencv/opencv |

MediaMTX release checksums and Rust toolchain download checksums are pinned in
`packaging/SHA256SUMS`. The hls.js license text is packaged at
`reco_field/static/HLS-LICENSE.txt`.

Runtime FFmpeg and GStreamer libraries are provided by JetPack/Ubuntu packages
and retain their upstream licenses.
