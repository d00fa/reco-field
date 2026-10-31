# Reco Field change record

This cumulative record describes the appliance work through **31 October 2026**
on top of Reco Video Stitcher 0.5.4. It separates shipped behavior, device
configuration and unverified capabilities. Version 0.2.0 is the first portable
Debian appliance release; the earlier development deployment was version 0.1.0.

For architecture, measured results and operating limits, see
[Reco Field](RECO-FIELD.md). For installation, updates and recovery, see the
[operating guide](../appliance/README.md).

## 0.2.0 — 2026-10-31

### Added: portable installation and upgrades

- ARM64 Debian package with prebuilt native tools and bundled Python dependencies.
- Per-unit SSD setup, persistent mounting and transactional activation with rollback.
- Preserved recordings, calibration, network settings and previous release on upgrade.
- Version and source checks, matching source archive, checksums and release manifest.
- Actual device hostname in network reconnect links for multi-unit installations.

The features below are included from the initial development deployment.

### Added: boot-ready local application

- Python FastAPI/Uvicorn control service with a responsive, locally served web
  interface, bundled HLS player and no runtime CDN or frontend build chain.
- Dedicated application account and three systemd services for the application,
  MediaMTX relay and privileged network helper.
- Camera-operation ownership across preview, recording, snapshots, geometric
  calibration and lens measurement; offline export excludes concurrent capture.
- Explicit startup, frame-readiness, recording, finalization, failure and idle
  states; bounded startup/calibration deadlines and process-group cleanup.
- Atomic settings and calibration replacement, bounded/redacted process logs,
  safe download paths, disk-space checks and preservation of existing data.
- Hardware preflight for the camera backend, native executable, Vulkan, media
  tools, storage and relay; explicitly labeled development simulation.
- Typed NVIDIA ISP exposure, analog gain, ISP digital gain, white-balance mode
  and lock controls, with range validation and active-session edit locks.

### Added: recording and streaming

- Local Matroska panorama recording with same-origin HLS preview and downloads
  after finalization.
- Real preview-only capture, without a hidden temporary panorama recording.
- Original-camera sessions containing independent left/right H.264 files,
  timestamp/packet metadata, colour metadata and saved lens/rig calibration.
- A throttled stitched viewfinder for source recording, independent of source
  encoding rate, plus recorded file metadata and actual sizes in the library.
- Offline panorama export using saved session calibration and common-interval
  timestamp resampling; explicit repeated/skipped frame counts, progress,
  cancellation, input validation and incomplete-output download protection.
- RTMP, RTMPS and SRT destination support, platform presets, masked stream keys
  and independent per-destination retry/failure state.
- One shared mobile encoder targeting 6 Mbps before per-destination stream copy;
  original-quality mode bypasses the mobile encode.
- Silent AAC for ingest compatibility. Microphone capture remains unconfigured.

### Changed: panorama and image processing

- Added cylindrical panorama geometry fitted to valid calibrated coverage while
  retaining the perspective camera crop.
- Corrected sensor aspect ratio in setup snapshots and added separate left/right
  lens profile import with alignment invalidation when a profile changes.
- Replaced the dark overlap fade with coverage-normalized GPU multiband blending.
  Pixels seen by only one camera retain full brightness; framing trims uncovered
  boundaries from the recorded view.
- Added overlap-based colour/exposure matching and per-input NVMM colour
  matrix/range handling. The tested sensors are BT.601 limited; the panorama is
  converted and tagged BT.709 limited.
- Added OpenCV colour graph-cut seam selection on reduced aligned images,
  bounded background analysis, candidate validation and geometric fallback.
  Accepted masks remain stable until geometry changes.
- Replaced FIFO pairing with comparable GStreamer host-clock pairing and stale
  frame replacement. The acceptance window is half a frame plus 2 ms; this does
  not add hardware exposure synchronization.
- Added OpenCV ChArUco/fisheye per-camera lens measurement with 16–32 diverse
  observations, held-out accuracy checks, rejected poor fits, backed-up profile
  application and required geometric recalibration.

### Improved: runtime cost and reliability

- Feed libx264 NV12 directly on the native path, removing a scalar chroma
  deinterleave while preserving planar and RGBA callers.
- Bound GPU readback work by submission/slot and drain it correctly; cache seam
  pyramids and warp views with geometry/ownership invalidation.
- Avoid allocating detection and replay resources while those paths are unused.
- Add aggregated capture, readback, CPU copy and encoder phase measurements.
- Keep the live software encoder on `ultrafast` after slower presets failed to
  leave enough CPU headroom. Use `veryfast`, CRF 17.6 and four encoder threads
  for offline exports after a same-input preset comparison.
- Select CPU decode explicitly for offline jobs, avoiding unsuccessful desktop
  NVDEC probing while retaining GPU stitching.
- Cache device telemetry for two seconds across clients. Bound and cache media
  metadata probing instead of launching it for every file on every status poll.
- Move HLS segment storage onto the SSD and apply a 512 MiB Go soft memory limit
  to the relay; retain the SSD mount and UUID checks for relay startup.
- Bound native secondary-stream startup and writes so a stalled relay cannot
  indefinitely block local file output. Write primary packets/trailer first;
  disable a failed secondary publisher for the remainder of that capture.

### Fixed: camera and media lifecycle defects

- Join camera workers and serialize Argus teardown to avoid the observed
  shutdown mutex failure; send EOS to both source branches before serialized
  NULL/drop so one file no longer gets a five-second extra tail.
- Request H.264 codec setup data when a secondary stream exists even if the
  primary output is a null muxer. Preview-only H.264/AAC now decodes correctly.
- Accept equal adjacent Matroska timestamps caused by millisecond precision
  while rejecting backwards timestamps and preserving manifest validation.
- Report exact completed export frame counts; keep originals unchanged through
  failed or cancelled exports and reject partial downloads as completed media.
- Follow capture generation when identifying the browser player and ignore
  obsolete HLS callbacks. Preview-to-source-recording transitions change stream
  dimensions without requiring a page reload.
- Preserve focused and unsaved fields during status polling, and show the
  actual active recording mode rather than only the saved preference.
- Handle the JetPack FFmpeg 4.4 VAAPI API spelling through build-time ABI
  detection, without installer source rewriting.
- Remove `ProtectClock=yes` from the affected service hardening because its
  implicit device restriction blocked actual GPU access; retain the dedicated
  account and compatible restrictions.

### Added and simplified: phone networking

- Root-owned NetworkManager helper behind a restricted local Unix socket, with
  private saved profiles, validation at both API/helper boundaries and camera
  busy checks.
- Delayed router/phone-hotspot switching after the web response, previous-network
  recovery on failure, and a setup access point after one minute without Wi-Fi.
- Retry of the saved target when it becomes visible.
- Replaced lengthy offline setup instructions with a Camera Wi-Fi card: local
  QR generation, PNG download, network/password copy, password reveal and an
  Open camera link.
- Added camera-network name/password editing. Blank password preserves the
  existing value; saved target and setup profile UUID are preserved. Inactive
  edits do not activate the access point or interrupt the router connection.
- Active edits delay access-point restart and attempt rollback after failure.
  Profiles remain private, QR URLs contain no credentials, QR responses are
  uncached, and validation errors no longer echo submitted secrets.
- Same-phone joining uses the operating system's Wi-Fi controls; the web page
  does not pretend to have permission to switch the phone's network itself.

### Changed: interface and access

- Reorganized the console around Live, Recordings and Setup, with a dominant
  uncropped viewfinder, grouped recordings and concise contextual controls.
- Added responsive light/dark styling, visible keyboard focus, labeled inputs,
  touch-sized controls, accessible status/error states and reduced-motion
  behavior, without adding a frontend framework.
- Added actual CPU, temperature, memory, uptime, storage and capture statistics;
  unavailable or stale readings are not presented as healthy zero values.
- Removed the login screen as explicitly requested. The app creates a browser
  session automatically and retains same-origin and CSRF checks. LAN reachability
  now grants control; this is a trusted-network appliance, not an internet-facing
  authenticated service.
- Restrict the appliance to validated H.264/libx264 output; unsupported HEVC and
  hardware-encoder selections are not offered as working options. Upstream CLI
  features remain separate from appliance support.

### Configured on the prototype: SSD and operating system

- Formatted the explicitly selected NVMe SSD as ext4 and configured mounting at
  boot. This was an operator-authorized operation on one device, not automatic
  installer behavior for unknown disks.
- Bound media paths to the mounted SSD UUID, added service mount dependencies
  and fail-closed checks, and prevented microSD fallback. Setup captures,
  temporary media, application logs and HLS segments use the SSD too.
- Set headless `multi-user.target` boot and stopped GDM, retaining the desktop
  packages and documented recovery commands. Measured idle memory fell by
  approximately 666 MiB.
- Preserved camera drivers, NetworkManager, SSH and the separately developed
  camera application's installation/services. Both applications still require
  exclusive use of the shared cameras.
- Removed a temporary build-time SSD swap file after memory pressure cleared.
  No OS packages were purged; no permanent swap change or clock overdrive was
  introduced. Existing MAXN_SUPER and clock configuration was retained.

### Installation and maintenance

- Added the source installer, pinned dependency downloads/checksums, accessible
  SSD build workspace, build-volume space checks and occupied-port refusal
  without stopping unrelated listeners.
- Stage and smoke-test a release before switching `/opt/reco-field/current`;
  restore the previous release and units if activation fails.
- Preserve application data and private settings on updates and default removal;
  exclude credentials, media, private QA data and generated build trees from
  source distribution.
- Keep the appliance package version, matching source, dependency pins and this
  change record together. The Debian release process and commands are documented
  in the operating guide as part of packaging this initial release.

### Verified and still outstanding

Hardware acceptance covers boot recovery, SSD refusal, live dual-camera capture,
browser HLS, source shutdown, complete file decode, byte-range download, offline
export/cancellation, relay failure and two local stream destinations. Native,
Python, network-helper and browser tests supplement those installed checks;
their latest totals and measured throughput are in [the overview](RECO-FIELD.md).

Physical per-lens measurement, outdoor/moving-player image quality, physical
phone joining and active access-point reconnect, public-platform streaming and
match-length endurance remain outstanding. Source recording plus streaming is
CPU-limited on the tested Orin Nano. AI tracking, microphone input, hardware
synchronization and Veo Cam 3 parity are not delivered claims.
