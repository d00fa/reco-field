# Reco Field

Local web control for a dual-camera Reco appliance, opening directly without a login.

Confirmed target hardware: Jetson Orin Nano Engineering Reference Developer
Kit Super, JetPack 6.2.3 (Ubuntu 22.04, L4T 36.5.2), two IMX477/Arducam CSI
cameras, and a Kingston 1 TB NVMe SSD mounted at `/srv/reco-ssd`. Production
recordings, snapshots and temporary media are restricted to that SSD; the
service refuses to start if the configured filesystem is missing. Orin Nano
has no NVENC; appliance output is limited to H.264 with
software `libx264`. Live capture uses `ultrafast`; offline exports use `veryfast`
at CRF 17.6 after a same-input Jetson comparison. The local relay includes silent AAC audio for streaming
compatibility. Mobile streaming creates one shared 6 Mbps rendition, then each
destination copies that stream. Original quality skips the extra encoder.
Microphone capture is not configured.

## Install the Debian package

Download the `.deb` and `SHA256SUMS` from
[Reco Field releases](https://github.com/d00fa/reco-field/releases). Version
`0.2.1` targets **ARM64 Ubuntu 22.04 / JetPack 6 / L4T 36**, with NVIDIA
OpenCV 4.8 and its Python bindings. It does not support JetPack 5, a desktop
Ubuntu PC, or an arbitrary camera driver. The tested configuration is the
Orin Nano Super and two IMX477 cameras described above.

```bash
sha256sum --check --ignore-missing SHA256SUMS
sudo apt install ./reco-field_0.2.1_arm64.deb
sudo reco-field-setup --device /dev/nvme0n1p1 --hostname reco-field-01
```

`--device` means an **existing, formatted ext4 SSD partition**. Check the device
name with `lsblk -f` first; the command never formats or erases a disk. Omit
`--device` if that SSD is already mounted at `/srv/reco-ssd`. Setup refuses the
system disk, SD/eMMC devices, conflicting mounts and a different saved UUID.
It adds an idempotent UUID mount to `/etc/fstab`, binds all media paths to that
SSD, creates the service account and enables the web, relay and Wi-Fi services.
A new package remains dormant until storage setup is complete; it cannot
silently start recording onto the SD card.

Use a different optional `--hostname` for each unit. The package generates
each unit's own setup Wi-Fi credentials; it never ships this prototype's
network credentials, streaming keys, recordings or calibration. Fresh settings
use the tested full-sensor IMX477 mode 0 at 4032×3040/30 and a 3072×864 panorama.
Measure each unit's lenses and complete stereo alignment in the web page before
recording. These defaults are not a calibrated camera profile.

Setup selects headless boot for the next restart, retaining the installed desktop
for recovery. Add `--keep-desktop` to retain the current boot target. It does not
remove OS packages or end the current desktop session. To restore desktop boot:

```bash
sudo systemctl set-default graphical.target
sudo systemctl start gdm
```

The recorded performance measurements used this unit's existing MAXN_SUPER
configuration and pinned `jetson_clocks`. The package does not select a numeric
power mode on other boards: choose a supported mode with NVIDIA's tools for
the exact board, cooling and power supply. It never restarts shared Argus or
changes another camera application's service.

## Upgrade or remove

Stop recordings, previews, calibration and offline exports first, then install
the newer `.deb` with `sudo apt install ./reco-field_VERSION_arm64.deb`.
The package checks that the app is idle, verifies the bundled payload, creates
an offline Python environment and smoke-tests it before switching releases.
No Rust compilation or PyPI download is needed during installation; `apt` may
download missing Ubuntu/NVIDIA runtime dependencies.

Updates retain `/etc/reco-field`, `/srv/reco-ssd/reco-field`, per-unit NetworkManager
settings and prior runtime releases. The old executable remains available while
the package is unpacked. If activation fails, the installer restores the prior
release and systemd units. A failed `postinst` leaves the Debian package
unconfigured; after fixing the reported issue, retry `sudo dpkg --configure reco-field`.
If the package reports active capture, finish that capture before retrying.
During the final switch, a short maintenance lock prevents new camera and
network work. If power is lost during setup, reboot clears the temporary
`/run/reco-field-upgrade` lock. Only remove that file manually after confirming
the installer process has exited.

`sudo apt remove reco-field` stops only Reco Field's services. Even `purge`
retains recordings, calibration, network state, the SSD mount and runtime
releases. This deliberate data preservation means removing the package does
not reclaim old recordings or release directories. Never delete them while
they contain needed footage or a rollback release.

## Build and publish an updated package

Every runtime or packaging change must increase `appliance/VERSION`, the Python
app and `pyproject.toml` versions, and add a release entry to
[`docs/RECO-FIELD-CHANGELOG.md`](../docs/RECO-FIELD-CHANGELOG.md).
The package workflow checks those values and rejects pull requests that change
runtime code without a version increase. Native builds run on a trusted
JetPack 6 machine; public pull requests do not execute on the camera.

After building and testing the native executable from the release source tree:

```bash
cargo build --release --locked -j 2 -p reco-cli --no-default-features \
  --features gstreamer,opencv-seam
python3 appliance/scripts/release_check.py \
  --native-binary target/release/reco --native-manifest-output native-source.json
python3 -m pip download --only-binary=:all: --no-deps \
  -r appliance/requirements-arm64.lock --dest /path/to/wheels
bash appliance/scripts/build-deb.sh \
  --reco target/release/reco --native-source-manifest native-source.json \
  --mediamtx /path/to/mediamtx --mediamtx-license /path/to/Mediamtx-LICENSE \
  --wheelhouse /path/to/wheels
```

Use the pinned MediaMTX 1.21.3 ARM64 binary and its MIT license; verify the
download against `packaging/SHA256SUMS`. Run the native-manifest command only
after compiling that exact tree. The builder checks the native binary hash,
all native source hashes, architecture, required CLI features and the exact
Python wheel inventory. Dependencies are locked in `requirements-arm64.lock`;
update and test that lock deliberately when changing dependencies.

The output in `appliance/dist` contains the `.deb`, corresponding source
archive, SHA-256 sums and a payload manifest. The Python app, installers, unit
templates and documentation are copied from the same frozen source archive.
The source archive excludes private device state, scratch captures and local
validation folders. Publish all four artifacts together under a
`reco-field-vVERSION` GitHub release after installing and testing the `.deb` on
a supported unit. GitHub CI validates the source and installer contracts; it
does not replace hardware acceptance testing or silently publish a binary.

## Install from source

From the extracted source archive:

```bash
sudo bash ./appliance/scripts/install.sh
```

Mount the recording SSD at `/srv/reco-ssd` before installing. The installer
requires at least 12 GiB free on the build volume and 1 GiB for the staged
release. Both build files and application data default to the SSD. To override
the build location:

```bash
sudo env RECO_FIELD_BUILD_DIR=/srv/reco-ssd/reco-build \
  bash ./appliance/scripts/install.sh
```

Before changing the host, the installer checks ports `8081`, `1936`, `8887`,
and `9998`. If an unrelated process owns one of them, installation stops with
the listener details and does not stop or modify that service.

The installer never formats storage. It records the mounted SSD's UUID and
adds a mount dependency to the service. Configure `/etc/fstab` to mount that
filesystem at `/srv/reco-ssd` on every boot. A missing or different disk never
falls back to recording on the SD card.

It installs the pinned Rust 1.92 toolchain, MediaMTX 1.21.3, Python 3.10
service dependencies, Reco 0.5.4, three systemd units, and a dedicated
`recofield` user. The source is copied into an accessible build workspace,
the full release is built and smoke-tested under that user, and only then is
`/opt/reco-field/current` switched atomically. Activation failure restores the
previous release and units. Existing files under `/srv/reco-ssd/reco-field` and
`/etc/reco-field` are preserved on updates.

JetPack 6 supplies FFmpeg 4.4 development libraries. `crates/reco-io/build.rs`
detects that ABI from pkg-config and selects the FFmpeg 4 VAAPI spelling at
compile time; the installer does not rewrite upstream Rust sources. The build
is also checked against the source tree's current FFmpeg API.

Open `http://YOUR-UNIT-HOSTNAME.local:8081` from a phone on the same network. There is
no username or password: anyone on that network can control the appliance.
The browser creates its session automatically; same-origin and CSRF checks
still reject commands from unrelated websites. Use a trusted LAN or private
hotspot and do not expose port8081 directly to the internet.

## Operate

The control service starts at boot without recording or broadcasting. In the
web UI:

1. Use Setup → Cameras to select the sensor mode, capture size, view, output size,
   and NVIDIA AE/AWB controls. Capture must match the lens profile's sensor
   crop. This rig's 4032x3040 profiles require mode 0 at 4032x3040/30;
   encoded output is configured separately. Mode 1 at 3840x2160 needs a lens profile
   for that crop. Measured profiles also require the exact sensor ID, mode and
   capture dimensions they were fitted for. Full-match endurance is untested.
2. Use Setup → Alignment to capture bounded left/right setup views, check focus and
   overlap, measure each lens or import measured profiles, then run geometric
   calibration. Legacy profiles can be shared between cameras, but a measured
   profile is specific to its sensor. Changing a profile requires fresh
   alignment before recording. Stereo matching does not measure lens distortion.
3. Add stream destinations. Ingest URLs and keys are stored privately and only
   masked values are returned to the browser.
4. In Live, use **Start preview** to check the stitched view without saving a
   recording. Choose **Panorama** or **Original cameras** beside Record. Original
   cameras saves two independent H.264 files while a smaller stitched viewfinder
   and outbound streaming run at up to 10 fps. Source-camera encoding is independent of that preview
   rate; measured file rates appear in Recordings after finalization.
5. Stop to finish the files. In Recordings, a complete Original cameras session
   offers separate downloads and **Create panorama**. Export uses the calibration
   saved with that session, preserves the originals and runs while cameras are
   idle. Its status and Cancel action remain visible until the job finishes.

Source recording retains the full sensor field of view at up to 2016×1520 per
camera on this rig. These are ISP-processed H.264 images, not raw sensor data.
Quality-mode files can be large, especially in noisy low light; SSD free space
and actual written bytes are shown in Live. The live preview is not saved as a
hidden panorama file. Capture and offline export stop safely if SSD checks fail.

The five-second offline comparison took 10.7 seconds with `veryfast`, versus
36.9 seconds with `medium`, at CRF 17.6 and 3072x864. A complete 32.333-second
panorama exported in 65.50 seconds with `veryfast` while one CPU core was also
compiling; the resulting file decoded fully and supported byte-range download.
Presets are not an equal-quality guarantee; the original camera files remain
available for another export. The CLI also accepts `--preset medium` for a slower
export. Offline jobs use CPU decoding and GPU stitching.

In a corrected three-minute Original cameras test, the two source files averaged
28.76 and 28.20 fps by their packet timestamp spans. The stitched viewfinder ran
at about 8.4 fps, with 91–95% CPU utilization across six cores and a temperature
near 71 C. Both source files finalized with ending clock anchors 2.69 ms apart;
this is software timing, not hardware exposure synchronization. Sustained 30 fps
source capture and full-match endurance have not been demonstrated.

With the app's shared mobile encoder feeding two local destinations, the source
files averaged about 26.08 and 25.82 fps at approximately 95% CPU utilization.
Both destinations received and fully decoded 1536x432, 10 fps H.264 plus AAC
from that source-mode viewfinder. Source recording plus streaming is CPU-limited
on this device; it is not a sustained 30 fps mode. These local tests do not
verify phone upload capacity or public-platform acceptance.

Outbound streams retry independently. Mobile targets up to 1920×540 at 6 Mbps
per destination, plus audio and network overhead. Native loopback relay failure
disables that output while preserving the local recording; start a new capture
session to restore its preview publisher.

Panorama uses a cylindrical projection fitted to the calibrated camera coverage.
Camera crop retains the perspective view. A wider output aspect ratio preserves
more horizontal coverage; the renderer logs the coverage implied by the saved
calibration at startup. This is not an independent optical measurement.
It cannot create a 180-degree view unless the lenses and mounting
cover it. Output width must be divisible by four for the NV12 encoder.

Camera and destination edits are disabled while capturing. Stop the session,
apply the change, then start again.

The appliance build enables `opencv-seam`. OpenCV selects a content-aware
colour-based graph-cut seam from reduced, aligned camera views after startup. The seam stays
fixed while the view geometry is unchanged, avoiding frame-to-frame seam jumps;
changing calibration or view geometry requires a new seam. Analysis runs in a
background worker while the existing GPU renderer handles full-resolution video.
Candidates must preserve camera coverage and pass a seam-disagreement check.
Until a valid seam is available, the renderer retains its geometric blend.

The native renderer matches exposure and colour using corresponding overlap
pixels, then blends detail and illumination at different scales on the GPU.
Pixels covered by only one camera retain their full brightness. Framing trims
uncovered lens boundaries from the recorded view. Accurate lens and rig
calibration still matter: blending cannot align nearby objects
at different depths or recover a view neither camera sees. Calibrate the
stationary rig against a detailed, distant scene with overlapping views.
OpenCV seam selection does not replace measured per-camera lens calibration or
synchronize sensor exposures.

### Measure each lens

In Setup → Alignment, expand **Measure each lens** and print the board on A4 landscape
at 100% scale.
Check that its squares measure 30 mm, mount it flat and keep lens focus fixed.
Select the left sensor and capture 16–32 views with the board at different
positions, distances and tilts, including the image edges. Hold the board and
rig still during each capture. Repeat for the right sensor.

**Solve lens** fits OpenCV's fisheye model and checks observations held out of
the fit. Central, repeated, poorly detected or inaccurate views are rejected;
collect better views instead of applying a poor result. **Apply profile** is
available only after validation. It backs up the existing profile and alignment,
then requires geometric calibration again. Capture views and temporary files
remain on the SSD. The current installed profiles remain nominal until this
physical measurement is completed.

Native capture reads each sensor's NVMM colour format and decodes BT.601 or
BT.709 with the reported range. Stitched video is tagged BT.709 limited range;
separate-camera H.264 files preserve this rig's BT.601 limited-range input.
Automatic white balance needs time to settle after startup; locking it at
startup can preserve the sensor's initial values. The digital-gain limit avoids
extra amplification, but cannot remove noise already present in low light.

Native camera capture pairs frames by their common GStreamer clock, replacing
the older view when the difference exceeds half a frame plus 2 milliseconds.
This prevents stale queued frames from being stitched together under load;
it does not synchronize the sensors' exposures in hardware. Timing and skipped
frame counts are logged in `camera.log`. Missing comparable timestamps are
explicitly reported as unsynchronized.

On the tested Orin Nano, the optimized paired panorama recorded at 29.36 fps
at 3072x864 over 115 seconds; 3840x1080 achieved 27.95 fps over 91 seconds.
Capture remains 4032x3040/30. The default stays 3072x864 for smoother capture.
The high-quality garage recording used about 88 Mbit/s because the noisy scene
compresses poorly. Original-quality destinations copy that stream. Mobile mode
uses the shared 6 Mbps rendition described above; allow for the combined upload
of all enabled destinations, audio and network overhead. Public-platform and
phone-hotspot streaming are not yet hardware-verified.

Other camera applications must be idle before recording, taking setup views,
or calibrating. Reco Field and recobox can run their web services together,
but they share the same physical cameras and NVIDIA Argus service. Restarting
that service from either application interrupts every active camera session.

## Capture stopped with an error

Version 0.2.1 reports the capture error ahead of buffered `GST_ARGUS` shutdown
messages. `Done Success` describes camera cleanup; it does not establish that
the recording succeeded. Process output is drained with a bounded wait before
the app reads the final diagnostic. Error selection is limited to the current
invocation and at most 64 KiB of recent logs.

An `Argus Correctable Error Status` with daemon `PIX_SHORT` or `CSIMUX_FRAME`
messages means the camera input failed. On the prototype, this stopped capture
while the encoder still finalized a playable partial recording. Existing files
are preserved; check the recording duration before relying on it. Start a new
recording to retry after the previous process has stopped. Reco Field does not
restart the shared Argus service automatically.

If it repeats, collect the application capture log and matching Argus/kernel
logs before rebooting:

```bash
sudo journalctl -u nvargus-daemon --since '10 minutes ago' --no-pager
sudo journalctl -k --since '10 minutes ago' --no-pager
```

The observed CSI fault is still under investigation. This diagnostic update
does not claim to repair the sensor driver or the physical camera connection.

## Phone hotspot

Open Setup → Network → Internet connection. Turn on the phone hotspot, enter its
network name and password, then choose Save & connect. The Jetson saves a
private NetworkManager profile and switches networks after replying to the
page. Reopen the device hostname shown by the page from the hotspot's network, or use
the Jetson address shown by the phone. Some phones restrict access between
hotspot clients.

An unsuccessful join restores the previous connection. If no Wi-Fi connection
is available for a minute, the Jetson starts its private setup access point.
The Camera Wi-Fi card shows a locally generated QR code. Scan it with another
phone's camera and tap Join, then choose Open camera (`http://10.42.0.1:8081`).
Save QR downloads a PNG you can keep or print for later. On the same phone,
use Copy beside the name/password and join through the phone's Wi-Fi settings;
a browser cannot change the phone's Wi-Fi connection by itself.

Edit Wi-Fi changes the camera network name and password. Leaving the password
blank preserves it. If Camera Wi-Fi is active, it restarts after saving and
phones must rejoin using the new QR code; editing an inactive camera network
does not interrupt the existing router or hotspot connection. Failed updates
restore the prior camera network where possible and report recovery failures.
It retries the saved phone hotspot when that network becomes visible. Wi-Fi
changes are blocked during camera operations. QR images are generated on the
Jetson without an external QR service and are served without browser caching.

## Paths

The configured Jetson boots headless; the desktop remains installed for recovery.
To restore desktop operation over SSH:

```bash
sudo systemctl set-default graphical.target
sudo systemctl start gdm
```

Return to appliance mode with `sudo systemctl set-default multi-user.target`
and `sudo systemctl stop gdm`. Stopping GDM ends the local desktop session.

- Settings and calibration: `/srv/reco-ssd/reco-field/config`
- Recordings: `/srv/reco-ssd/reco-field/recordings`
- Setup snapshots: `/srv/reco-ssd/reco-field/snapshots`
- Bounded logs: `/srv/reco-ssd/reco-field/logs`
- Private Wi-Fi profiles: `/etc/NetworkManager/system-connections/reco-field-*`
- Service configuration: `/etc/reco-field`
- Application releases: `/opt/reco-field/releases`; active release:
  `/opt/reco-field/current`

Useful checks:

```bash
systemctl status reco-field-relay reco-field-network reco-field
journalctl -u reco-field -u reco-field-relay -u reco-field-network -n 100 --no-pager
curl http://127.0.0.1:8081/api/session
```

## Remove a legacy source installation

For a Debian installation, use `sudo apt remove reco-field` as described above.
The following command applies only to the older source installer:

```bash
sudo bash ./appliance/scripts/uninstall.sh
```

Uninstall removes the application and units. Settings, credentials, stream
keys, snapshots, and recordings remain under `/srv/reco-ssd/reco-field`.
Saved NetworkManager profiles are also preserved.

## Hardware Boundaries

The app distinguishes readiness, startup, live camera capture, and geometric
calibration. It never treats process startup as a valid camera session or a
successful calibration. The simulator is enabled only with
`RECO_FIELD_SIMULATOR=1` and is labeled in the UI.

Current acceptance results and remaining checks are documented in
the [public implementation guide](../docs/RECO-FIELD.md) and
[change record](../docs/RECO-FIELD-CHANGELOG.md). Source-level and synthetic-media
tests cannot prove installed Argus capture, target encode throughput, focus,
overlap, exposure quality, or sustained recording. Geometric calibration needs
both cameras looking at a detailed scene with sufficient overlap. Per-lens
physical calibration remains deferred, and no Veo Cam 3 image-quality parity is
claimed from the garage tests.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r appliance/requirements-dev.txt
pytest appliance/tests
```

The frontend has no build step or runtime CDN. `hls.js` and its license are
packaged in `reco_field/static`.
