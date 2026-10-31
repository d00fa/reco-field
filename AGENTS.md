# Reco Video Stitcher

Open-source GPU-accelerated panoramic sports camera software.

## Reco Field appliance releases

This fork ships the Jetson appliance as a versioned ARM64 Debian package. Every
shipped change to appliance behavior, native pipeline or deployment must update
`appliance/VERSION`, Python version metadata and `docs/RECO-FIELD-CHANGELOG.md`,
then rebuild the `.deb`, corresponding source archive, manifest and checksums.
Run the package release checks before publishing; never reuse a released version
for different contents. Keep hardware-tested claims separate from unverified work.

Never include recordings, private QA images, SSH/Wi-Fi/stream credentials,
NetworkManager profiles, device SSD UUIDs or measured per-unit calibration in a
package or Git commit. Installation and upgrade must preserve unit data, require
its configured SSD, and leave other applications and the shared camera service
alone. See `appliance/README.md` and `docs/RECO-FIELD.md` for the release contract.

## Project status

Active release work lives in GitHub issues, PRs, and milestones, not in this
file - check there for the current focus. For the latest stable version and
changelog, see the [GitHub Releases page](https://github.com/reco-project/video-stitcher/releases).
Per-crate consumer pain is logged in each crate's `FRICTION.md`
(e.g. `crates/reco-gui/FRICTION.md`, `crates/reco-obs/FRICTION.md`).

**Rule: document friction, don't work around it.** A reco-core API gap that a
consumer would otherwise hack around gets a `FRICTION.md` entry, not a
consumer-side workaround.

## Architecture (Rust + wgpu)

- `crates/reco-core/` — GPU stitching engine (library crate, no I/O deps)
- `crates/reco-cli/` — CLI binary (`reco stitch`, `reco info`, `reco calibrate`, `reco preview`)
- `crates/reco-io/` — Pluggable I/O backends (FFmpeg decode/encode, GStreamer, libcamera)
- `crates/reco-detect/` — AI detection backends (ORT CPU/GPU, TensorRT, NCNN, CoreML/Metal)
- `crates/reco-autocam/` — AI camera control (directors, trajectory smoothing, ROI filtering)
- `crates/reco-calibrate/` — Stereo camera calibration (AKAZE features, optimization)
- `crates/reco-gui/` — Slint GUI consumer (wgpu zero-copy preview)
- `crates/reco-obs/` — OBS Studio source plugin (async-frame ingestion + BGRA + interactive pan/zoom)

## Key commands

```bash
cargo build                   # Build all crates
cargo test --all              # Run all tests
cargo clippy --all-targets -- -D warnings   # Lint
cargo fmt --all -- --check    # Format check
cargo fmt --all               # Auto-format
cargo doc --no-deps --open    # Generate and open docs
cargo run -p reco-cli -- info # Show GPU info
cargo run -p reco-cli -- stitch left.mp4 right.mp4 -c match.json -o out.mp4
cargo run -p reco-cli -- preview left.mp4 right.mp4 -c match.json
cargo run --release -p reco-cli --features profiling -- stitch left.mp4 right.mp4 -c match.json -o out.mp4 --max-frames 300  # Profile 300 frames → reco-trace.json (open in ui.perfetto.dev)
```

## Build & contributing

- Build prerequisites (Rust version, FFmpeg development libraries, clang,
  pkg-config) and the feature-flag matrix: see [README.md](README.md).
- Contribution conventions (branch naming, PR template, CLA): see
  [CONTRIBUTING.md](CONTRIBUTING.md).

## Code standards

- `rustfmt` formatting (config in `rustfmt.toml`)
- `clippy` linting with `-D warnings` (zero warnings policy)
- Doc comments (`///`) on all public items
- Module-level docs (`//!`) explaining purpose
- Tests in each module (`#[cfg(test)] mod tests`)
- All PRs must pass: `cargo fmt --check && cargo clippy && cargo test`
- Clippy must also pass with `--features profiling`
- Keep commit messages and PR descriptions concise and technical (what
  changed + why), especially when written by an AI agent - no filler, no
  marketing tone
- `profiling` feature: opt-in `tracing` + `tracing-chrome` instrumentation (zero-cost when off)

## Context
- Public open-source project (AGPL-3.0) with a growing community and forum
- Users include football clubs, amateur sports teams — prioritize UX clarity
- Open alternative to proprietary sports camera solutions
- Targets: desktop (Win/macOS/Linux), NVIDIA Jetson, cloud, mobile

## When writing code
- Production-grade: handle errors, validate inputs at API boundaries
- Cross-platform (Windows/macOS/Linux) — avoid platform-specific assumptions
- Performance matters: this processes video frames in real time
- Modular: reco-core must be usable as a standalone Rust crate
- Explicit over implicit: no hidden defaults, no magic
- Verify changes actually run - exercise the binary or tests, not just `cargo check`
