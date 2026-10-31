//! Live stereo camera capture via GStreamer.
//!
//! Builds a GStreamer pipeline per camera, pulls frames from
//! `appsink` in dedicated threads, and pairs them into stereo
//! frame pairs via a bounded channel.
//!
//! Supports two capture modes:
//! - **I420** (YUV420P): three separate planes, works everywhere
//! - **NV12**: Y + interleaved UV, native NVIDIA ISP output - skips
//!   format conversion on Jetson for lower latency

use gstreamer as gst;
use gstreamer::prelude::*;
use gstreamer_app as gst_app;
use reco_core::source::{
    FramePair, Nv12Data, Nv12FramePair, SourceError, SourceInfo, StereoFrame, YuvData,
};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

/// ISP exposure mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, serde::Serialize)]
pub enum ExposureMode {
    /// Let the NVIDIA ISP adjust exposure continuously.
    #[default]
    Auto,
    /// Fix exposure to a single nanosecond value.
    Manual,
}

/// White-balance mode exposed by `nvarguscamerasrc`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, serde::Serialize)]
pub enum AwbMode {
    /// Disable white-balance correction.
    Off,
    /// Let the NVIDIA ISP choose the white-balance gains.
    #[default]
    Auto,
    Incandescent,
    Fluorescent,
    Tungsten,
    Daylight,
    Cloudy,
    Twilight,
    Shade,
    WarmFluorescent,
}

impl AwbMode {
    fn from_str_loose(value: &str) -> Option<Self> {
        match value {
            "off" => Some(Self::Off),
            "auto" => Some(Self::Auto),
            "incandescent" => Some(Self::Incandescent),
            "fluorescent" => Some(Self::Fluorescent),
            "tungsten" => Some(Self::Tungsten),
            "daylight" => Some(Self::Daylight),
            "cloudy" => Some(Self::Cloudy),
            "twilight" => Some(Self::Twilight),
            "shade" => Some(Self::Shade),
            "warm-fluorescent" => Some(Self::WarmFluorescent),
            _ => None,
        }
    }

    fn nvargus_value(self) -> u8 {
        match self {
            Self::Off => 0,
            Self::Auto => 1,
            Self::Incandescent => 2,
            Self::Fluorescent => 3,
            Self::Tungsten => 4,
            Self::Daylight => 5,
            Self::Cloudy => 6,
            Self::Twilight => 7,
            Self::Shade => 8,
            Self::WarmFluorescent => 9,
        }
    }
}

/// Optional NVIDIA ISP controls applied when each camera pipeline starts.
///
/// Defaults omit every property, preserving existing pipelines. Callers can
/// set a sensor mode and bounded AE/AWB/gain values through the CLI or service.
#[derive(Debug, Clone, PartialEq, serde::Serialize)]
pub struct CameraControls {
    pub sensor_mode: Option<u32>,
    pub exposure_mode: ExposureMode,
    pub exposure_min_ns: Option<u64>,
    pub exposure_max_ns: Option<u64>,
    pub gain_min: Option<f32>,
    pub gain_max: Option<f32>,
    pub digital_gain_min: Option<f32>,
    pub digital_gain_max: Option<f32>,
    pub awb_mode: AwbMode,
    pub awb_lock: bool,
}

impl Default for CameraControls {
    fn default() -> Self {
        Self {
            sensor_mode: None,
            exposure_mode: ExposureMode::Auto,
            exposure_min_ns: None,
            exposure_max_ns: None,
            gain_min: None,
            gain_max: None,
            digital_gain_min: None,
            digital_gain_max: None,
            awb_mode: AwbMode::Auto,
            awb_lock: false,
        }
    }
}

impl CameraControls {
    /// Parse a white-balance mode name used by the CLI.
    pub fn awb_mode_from_str(value: &str) -> Option<AwbMode> {
        AwbMode::from_str_loose(value)
    }

    /// Validate values before they are interpolated into a GStreamer pipeline.
    pub fn validate(&self) -> Result<(), String> {
        if self.sensor_mode.is_some_and(|mode| mode > 255) {
            return Err("sensor mode must be in 0..=255".into());
        }

        match (self.exposure_min_ns, self.exposure_max_ns) {
            (Some(min), Some(max)) => {
                if min == 0 || max < min || max > 1_000_000_000 {
                    return Err(
                        "exposure range must be 1..=1,000,000,000 ns with min <= max".into(),
                    );
                }
                if self.exposure_mode == ExposureMode::Manual && min != max {
                    return Err("manual exposure requires equal minimum and maximum values".into());
                }
            }
            (None, None) => {}
            _ => return Err("exposure minimum and maximum must be set together".into()),
        }

        match (self.gain_min, self.gain_max) {
            (Some(min), Some(max)) => {
                if !min.is_finite() || !max.is_finite() || min < 0.1 || max > 64.0 || max < min {
                    return Err("gain range must be finite and within 0.1..=64".into());
                }
            }
            (None, None) => {}
            _ => return Err("gain minimum and maximum must be set together".into()),
        }

        match (self.digital_gain_min, self.digital_gain_max) {
            (Some(min), Some(max)) => {
                if !min.is_finite() || !max.is_finite() || min < 0.1 || max > 8.0 || max < min {
                    return Err("digital gain range must be finite and within 0.1..=8".into());
                }
            }
            (None, None) => {}
            _ => return Err("digital gain minimum and maximum must be set together".into()),
        }

        if self.awb_mode == AwbMode::Off && self.awb_lock {
            return Err("AWB lock cannot be enabled while white balance is off".into());
        }
        Ok(())
    }
}

/// Camera capture configuration.
#[derive(Debug, Clone, serde::Serialize)]
pub struct CameraConfig {
    /// Capture width in pixels.
    pub width: u32,
    /// Capture height in pixels.
    pub height: u32,
    /// Capture frame rate.
    pub fps: u32,
    /// Left camera device or sensor ID.
    ///
    /// - Jetson: sensor index as string ("0", "1")
    /// - Linux: V4L2 device path ("/dev/video0")
    pub left_device: String,
    /// Right camera device or sensor ID.
    pub right_device: String,
}

/// Capture pixel format for the GStreamer pipeline.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaptureFormat {
    /// YUV420P: three separate planes (Y, U, V). Works everywhere.
    I420,
    /// NV12: Y + interleaved UV. Native NVIDIA ISP output - avoids
    /// a format conversion step on Jetson.
    Nv12,
}

/// Validate a device string before interpolating it into a GStreamer pipeline.
///
/// Accepted formats per platform:
/// - Jetson (nvarguscamerasrc): numeric sensor ID only, e.g. `"0"`, `"1"`
/// - macOS (avfvideosrc): numeric device index only
/// - Windows (mfvideosrc): numeric device index only
/// - Linux V4L2: path matching `/dev/video<digits>`, e.g. `/dev/video0`
///
/// Returns `Err` with a descriptive message if the device string does not
/// match the expected pattern, preventing injection of arbitrary GStreamer
/// elements or shell metacharacters into the pipeline description.
fn validate_device_string(device: &str) -> Result<(), String> {
    if is_tegra() || cfg!(target_os = "macos") || cfg!(target_os = "windows") {
        // Numeric index only (one or more digits, nothing else)
        if device.chars().all(|c| c.is_ascii_digit()) && !device.is_empty() {
            Ok(())
        } else {
            Err(format!(
                "invalid device string {device:?}: expected a numeric index (e.g. \"0\")"
            ))
        }
    } else {
        // Linux V4L2: must be exactly /dev/video<digits>
        let valid = device.starts_with("/dev/video")
            && device["/dev/video".len()..]
                .chars()
                .all(|c| c.is_ascii_digit())
            && device.len() > "/dev/video".len();
        if valid {
            Ok(())
        } else {
            Err(format!(
                "invalid device string {device:?}: expected a V4L2 path like \"/dev/video0\""
            ))
        }
    }
}

/// Build the platform-appropriate GStreamer pipeline string.
///
/// Returns an error if `device` fails [`validate_device_string`].
fn build_pipeline_string(
    device: &str,
    width: u32,
    height: u32,
    fps: u32,
    format: CaptureFormat,
    controls: &CameraControls,
) -> Result<String, String> {
    validate_device_string(device)?;
    validate_controls_for_platform(controls)?;

    let fmt_str = match format {
        CaptureFormat::I420 => "I420",
        CaptureFormat::Nv12 => "NV12",
    };

    let pipeline = if is_tegra() {
        build_nvargus_pipeline_string(device, width, height, fps, format, controls)?
    } else if cfg!(target_os = "macos") {
        format!(
            "avfvideosrc device-index={device} ! \
             video/x-raw,width={width},height={height},framerate={fps}/1 ! \
             videoconvert ! \
             video/x-raw,format={fmt_str} ! \
             appsink name=sink emit-signals=false sync=false"
        )
    } else if cfg!(target_os = "windows") {
        format!(
            "mfvideosrc device-index={device} ! \
             video/x-raw,width={width},height={height},framerate={fps}/1 ! \
             videoconvert ! \
             video/x-raw,format={fmt_str} ! \
             appsink name=sink emit-signals=false sync=false"
        )
    } else {
        // Linux: generic V4L2
        format!(
            "v4l2src device={device} ! \
             video/x-raw,width={width},height={height},framerate={fps}/1 ! \
             videoconvert ! \
             video/x-raw,format={fmt_str} ! \
             appsink name=sink emit-signals=false sync=false"
        )
    };

    Ok(pipeline)
}

fn validate_controls_for_platform(controls: &CameraControls) -> Result<(), String> {
    controls.validate()?;
    if !is_tegra() && controls != &CameraControls::default() {
        return Err("NVIDIA camera controls require nvarguscamerasrc on a Jetson target".into());
    }
    Ok(())
}

fn nvargus_control_properties(controls: &CameraControls) -> Result<String, String> {
    controls.validate()?;
    let mut properties = String::new();
    if let Some(mode) = controls.sensor_mode {
        properties.push_str(&format!(" sensor-mode={mode}"));
    }
    if let (Some(min), Some(max)) = (controls.exposure_min_ns, controls.exposure_max_ns) {
        properties.push_str(&format!(" exposuretimerange=\"{min} {max}\""));
    }
    if controls.exposure_mode == ExposureMode::Manual {
        properties.push_str(" aelock=true");
    }
    if let (Some(min), Some(max)) = (controls.gain_min, controls.gain_max) {
        properties.push_str(&format!(" gainrange=\"{min} {max}\""));
    }
    if let (Some(min), Some(max)) = (controls.digital_gain_min, controls.digital_gain_max) {
        properties.push_str(&format!(" ispdigitalgainrange=\"{min} {max}\""));
    }
    properties.push_str(&format!(" wbmode={}", controls.awb_mode.nvargus_value()));
    if controls.awb_lock {
        properties.push_str(" awblock=true");
    }
    Ok(properties)
}

fn build_nvargus_pipeline_string(
    device: &str,
    width: u32,
    height: u32,
    fps: u32,
    format: CaptureFormat,
    controls: &CameraControls,
) -> Result<String, String> {
    if device.is_empty() || !device.chars().all(|c| c.is_ascii_digit()) {
        return Err(format!("invalid nvargus sensor id {device:?}"));
    }
    let fmt_str = match format {
        CaptureFormat::I420 => "I420",
        CaptureFormat::Nv12 => "NV12",
    };
    let controls = nvargus_control_properties(controls)?;
    Ok(format!(
        "nvarguscamerasrc sensor-id={device}{controls} ! \
         video/x-raw(memory:NVMM),width={width},height={height},format=NV12,framerate={fps}/1 ! \
         nvvidconv ! \
         video/x-raw,format={fmt_str} ! \
         appsink name=sink emit-signals=false sync=false"
    ))
}

/// Detect if we're running on NVIDIA Tegra (Jetson platform).
fn is_tegra() -> bool {
    Path::new("/etc/nv_tegra_release").exists()
        || std::fs::read_to_string("/proc/device-tree/compatible")
            .unwrap_or_default()
            .contains("nvidia,tegra")
}

/// Extract I420 planes from a GStreamer buffer.
fn extract_i420(data: &[u8], width: u32, height: u32) -> Result<YuvData, SourceError> {
    let y_size = (width * height) as usize;
    let uv_size = ((width / 2) * (height / 2)) as usize;

    if data.len() < y_size + 2 * uv_size {
        return Err(SourceError::Read {
            reason: format!(
                "buffer too small: {} < {}",
                data.len(),
                y_size + 2 * uv_size
            ),
        });
    }

    Ok(YuvData {
        y: data[..y_size].to_vec(),
        u: data[y_size..y_size + uv_size].to_vec(),
        v: data[y_size + uv_size..y_size + 2 * uv_size].to_vec(),
    })
}

/// Extract NV12 planes from a GStreamer buffer.
///
/// NV12 layout: Y plane (width*height), then interleaved UV plane (width*height/2).
fn extract_nv12(data: &[u8], width: u32, height: u32) -> Result<Nv12Data, SourceError> {
    let y_size = (width * height) as usize;
    let uv_size = (width * (height / 2)) as usize;

    if data.len() < y_size + uv_size {
        return Err(SourceError::Read {
            reason: format!(
                "NV12 buffer too small: {} < {}",
                data.len(),
                y_size + uv_size
            ),
        });
    }

    Ok(Nv12Data {
        y: data[..y_size].to_vec(),
        uv: data[y_size..y_size + uv_size].to_vec(),
    })
}

/// Build and start a GStreamer pipeline, returning the pipeline and appsink.
fn build_capture_pipeline(
    device: &str,
    label: &'static str,
    width: u32,
    height: u32,
    fps: u32,
    format: CaptureFormat,
    controls: &CameraControls,
) -> Result<(gst::Pipeline, gst_app::AppSink), String> {
    if let Err(e) = gst::init() {
        return Err(format!("{label} GStreamer init failed: {e}"));
    }

    let pipeline_str = build_pipeline_string(device, width, height, fps, format, controls)?;
    log::info!("{label} pipeline: {pipeline_str}");

    let pipeline = gst::parse::launch(&pipeline_str)
        .map_err(|e| format!("{label} pipeline parse: {e}"))?
        .downcast::<gst::Pipeline>()
        .map_err(|_| format!("{label}: not a pipeline"))?;

    let appsink = pipeline
        .by_name("sink")
        .ok_or_else(|| format!("{label}: appsink not found"))?
        .downcast::<gst_app::AppSink>()
        .map_err(|_| format!("{label}: element is not an AppSink"))?;

    appsink.set_max_buffers(2);
    appsink.set_drop(true);
    // Capture stops consuming before EOS; queued frames must not block teardown.
    appsink.set_wait_on_eos(false);

    pipeline
        .set_state(gst::State::Playing)
        .map_err(|e| format!("{label} set_state(Playing): {e}"))?;

    Ok((pipeline, appsink))
}

/// Spawn a GStreamer capture thread for one camera (I420 output).
fn spawn_capture_thread(
    device: String,
    label: &'static str,
    width: u32,
    height: u32,
    fps: u32,
    controls: CameraControls,
) -> mpsc::Receiver<YuvData> {
    let (tx, rx) = mpsc::sync_channel::<YuvData>(2);

    std::thread::Builder::new()
        .name(format!("capture_{label}"))
        .spawn(move || {
            let (pipeline, appsink) = match build_capture_pipeline(
                &device,
                label,
                width,
                height,
                fps,
                CaptureFormat::I420,
                &controls,
            ) {
                Ok(p) => p,
                Err(e) => {
                    log::error!("{e}");
                    return;
                }
            };

            loop {
                let sample = match appsink.pull_sample() {
                    Ok(s) => s,
                    Err(_) => break,
                };

                let Some(buffer) = sample.buffer() else {
                    log::error!("{label}: sample has no buffer");
                    break;
                };

                let Ok(map) = buffer.map_readable() else {
                    log::error!("{label}: buffer map failed");
                    break;
                };

                match extract_i420(map.as_slice(), width, height) {
                    Ok(yuv) => {
                        if tx.send(yuv).is_err() {
                            break;
                        }
                    }
                    Err(e) => {
                        log::error!("{label}: {e}");
                        break;
                    }
                }
            }

            let _ = pipeline.set_state(gst::State::Null);
        })
        .expect("spawn capture thread");

    rx
}

/// Spawn a GStreamer capture thread for one camera (NV12 output).
///
/// The stop signal allows graceful shutdown: send EOS, wait for pipeline
/// to reach Null state, then exit. This prevents Argus teardown crashes.
fn spawn_nv12_capture_thread(
    device: String,
    label: &'static str,
    width: u32,
    height: u32,
    fps: u32,
    controls: CameraControls,
    stop: Arc<AtomicBool>,
) -> (mpsc::Receiver<Nv12Data>, std::thread::JoinHandle<()>) {
    let (tx, rx) = mpsc::sync_channel::<Nv12Data>(2);

    let thread = std::thread::Builder::new()
        .name(format!("capture_{label}"))
        .spawn(move || {
            let (pipeline, appsink) = match build_capture_pipeline(
                &device,
                label,
                width,
                height,
                fps,
                CaptureFormat::Nv12,
                &controls,
            ) {
                Ok(p) => p,
                Err(e) => {
                    log::error!("{e}");
                    return;
                }
            };

            loop {
                if stop.load(Ordering::Relaxed) {
                    break;
                }

                let sample = match appsink.pull_sample() {
                    Ok(s) => s,
                    Err(_) => break,
                };

                let Some(buffer) = sample.buffer() else {
                    log::error!("{label}: sample has no buffer");
                    break;
                };

                let Ok(map) = buffer.map_readable() else {
                    log::error!("{label}: buffer map failed");
                    break;
                };

                match extract_nv12(map.as_slice(), width, height) {
                    Ok(nv12) => {
                        if tx.send(nv12).is_err() {
                            break;
                        }
                    }
                    Err(e) => {
                        log::error!("{label}: {e}");
                        break;
                    }
                }
            }

            // Argus shares a client across sensors; serialize their teardown.
            static SHUTDOWN_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
            let _shutdown = SHUTDOWN_LOCK.lock().unwrap_or_else(|e| e.into_inner());
            log::info!("{label}: sending EOS for graceful shutdown");
            pipeline.send_event(gst::event::Eos::new());
            if let Some(bus) = pipeline.bus() {
                let _ = bus.timed_pop_filtered(
                    gst::ClockTime::from_seconds(3),
                    &[gst::MessageType::Eos, gst::MessageType::Error],
                );
            }
            let _ = pipeline.set_state(gst::State::Null);
            // Wait for state change to complete
            let _ = pipeline.state(gst::ClockTime::from_seconds(2));
            drop(appsink);
            drop(pipeline);
            log::info!("{label}: pipeline stopped");
        })
        .expect("spawn capture thread");

    (rx, thread)
}

/// Stereo camera source using GStreamer (I420 output).
///
/// Each camera runs in its own thread pulling frames from appsink.
/// A pairing thread zips left+right into `FramePair`s and sends
/// them through a bounded channel.
pub struct GstreamerCameraSource {
    rx: mpsc::Receiver<FramePair>,
    info: SourceInfo,
}

impl GstreamerCameraSource {
    /// Open a stereo camera source with threaded capture.
    pub fn open(config: &CameraConfig) -> Result<Self, SourceError> {
        Self::open_with_controls(config, &CameraControls::default())
    }

    /// Open a stereo camera source with optional NVIDIA ISP controls.
    pub fn open_with_controls(
        config: &CameraConfig,
        controls: &CameraControls,
    ) -> Result<Self, SourceError> {
        validate_controls_for_platform(controls).map_err(|reason| SourceError::Init {
            path: format!("{} + {}", config.left_device, config.right_device),
            reason,
        })?;
        gst::init().map_err(|e| SourceError::Init {
            path: format!("{} + {}", config.left_device, config.right_device),
            reason: format!("GStreamer init: {e}"),
        })?;

        let left_rx = spawn_capture_thread(
            config.left_device.clone(),
            "left",
            config.width,
            config.height,
            config.fps,
            controls.clone(),
        );
        let right_rx = spawn_capture_thread(
            config.right_device.clone(),
            "right",
            config.width,
            config.height,
            config.fps,
            controls.clone(),
        );

        let (tx, rx) = mpsc::sync_channel::<FramePair>(2);

        std::thread::Builder::new()
            .name("capture_pair".into())
            .spawn(move || {
                while let (Ok(left), Ok(right)) = (left_rx.recv(), right_rx.recv()) {
                    if tx.send(FramePair { left, right }).is_err() {
                        break;
                    }
                }
            })
            .expect("spawn pairing thread");

        let info = SourceInfo {
            width: config.width,
            height: config.height,
            fps: config.fps as f64,
            fps_rational: None,
            total_frames: None,
        };

        log::info!(
            "Camera source ready: {}x{} @ {} fps (I420, threaded)",
            config.width,
            config.height,
            config.fps
        );

        Ok(Self { rx, info })
    }
}

impl reco_core::source::FrameSource for GstreamerCameraSource {
    fn info(&self) -> SourceInfo {
        self.info.clone()
    }

    fn next_frame(&mut self) -> Result<Option<StereoFrame>, SourceError> {
        match self.rx.recv() {
            Ok(pair) => Ok(Some(StereoFrame::Yuv420p(pair))),
            Err(_) => Ok(None),
        }
    }
}

/// Stereo camera source using GStreamer (NV12 output).
///
/// Like `GstreamerCameraSource` but captures in NV12 format, which is
/// the native output of NVIDIA's ISP. This avoids the NV12->I420
/// conversion that nvvidconv would otherwise perform, saving CPU time
/// and memory bandwidth on Jetson.
///
/// Implements graceful shutdown via `Drop` to avoid Argus teardown crashes.
pub struct GstreamerNv12CameraSource {
    rx: mpsc::Receiver<Nv12FramePair>,
    info: SourceInfo,
    stop: Arc<AtomicBool>,
    threads: Vec<std::thread::JoinHandle<()>>,
}

impl GstreamerNv12CameraSource {
    /// Open a stereo NV12 camera source with threaded capture.
    pub fn open(config: &CameraConfig) -> Result<Self, SourceError> {
        Self::open_with_controls(config, &CameraControls::default())
    }

    /// Open a stereo NV12 source with optional NVIDIA ISP controls.
    pub fn open_with_controls(
        config: &CameraConfig,
        controls: &CameraControls,
    ) -> Result<Self, SourceError> {
        validate_controls_for_platform(controls).map_err(|reason| SourceError::Init {
            path: format!("{} + {}", config.left_device, config.right_device),
            reason,
        })?;
        gst::init().map_err(|e| SourceError::Init {
            path: format!("{} + {}", config.left_device, config.right_device),
            reason: format!("GStreamer init: {e}"),
        })?;

        let stop = Arc::new(AtomicBool::new(false));

        let (left_rx, left_thread) = spawn_nv12_capture_thread(
            config.left_device.clone(),
            "left",
            config.width,
            config.height,
            config.fps,
            controls.clone(),
            stop.clone(),
        );
        let (right_rx, right_thread) = spawn_nv12_capture_thread(
            config.right_device.clone(),
            "right",
            config.width,
            config.height,
            config.fps,
            controls.clone(),
            stop.clone(),
        );

        let (tx, rx) = mpsc::sync_channel::<Nv12FramePair>(2);

        let pair_thread = std::thread::Builder::new()
            .name("capture_pair".into())
            .spawn(move || {
                while let (Ok(left), Ok(right)) = (left_rx.recv(), right_rx.recv()) {
                    if tx.send(Nv12FramePair { left, right }).is_err() {
                        break;
                    }
                }
            })
            .expect("spawn pairing thread");

        let info = SourceInfo {
            width: config.width,
            height: config.height,
            fps: config.fps as f64,
            fps_rational: None,
            total_frames: None,
        };

        log::info!(
            "Camera source ready: {}x{} @ {} fps (NV12, threaded)",
            config.width,
            config.height,
            config.fps
        );

        Ok(Self {
            rx,
            info,
            stop,
            threads: vec![left_thread, right_thread, pair_thread],
        })
    }

    /// Signal capture threads to stop.
    pub fn stop(&self) {
        self.stop.store(true, Ordering::Relaxed);
    }

    /// Source metadata.
    pub fn info(&self) -> SourceInfo {
        self.info.clone()
    }

    /// Get the next stereo NV12 frame pair, or `None` if the source is exhausted.
    pub fn next_pair(&mut self) -> Result<Option<Nv12FramePair>, SourceError> {
        match self.rx.recv() {
            Ok(pair) => Ok(Some(pair)),
            Err(_) => Ok(None),
        }
    }
}

impl reco_core::source::FrameSource for GstreamerNv12CameraSource {
    fn info(&self) -> reco_core::source::SourceInfo {
        self.info.clone()
    }

    fn next_frame(
        &mut self,
    ) -> Result<Option<reco_core::source::StereoFrame>, reco_core::source::SourceError> {
        self.next_pair()
            .map(|opt| opt.map(reco_core::source::StereoFrame::Nv12))
    }
}

impl Drop for GstreamerNv12CameraSource {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        // Keep unblocking producers until Argus teardown finishes before library exit.
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(10);
        while self.threads.iter().any(|thread| !thread.is_finished()) {
            let _ = self.rx.recv_timeout(std::time::Duration::from_millis(50));
            if std::time::Instant::now() >= deadline {
                log::warn!("Camera teardown exceeded ten seconds");
                break;
            }
        }
        for thread in self.threads.drain(..) {
            if thread.is_finished() {
                let _ = thread.join();
            }
        }
    }
}

// ── NVMM zero-copy camera source (Jetson only) ─────────────────────

#[cfg(target_os = "linux")]
pub use nvmm_source::GstreamerNvmmCameraSource;
#[cfg(target_os = "linux")]
pub use nvmm_source::NvmmFramePair;

#[cfg(target_os = "linux")]
mod nvmm_source {
    use super::*;
    use crate::gstreamer::nvmm::{self, NvmmFrameInfo};
    use crate::gstreamer::source_recording::{
        SourceRecording, SourceRecordingConfig, SourceTiming,
    };
    use std::cell::Cell;
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    /// Stereo pair of NVMM frame metadata (DMA-buf fds + surface ptrs).
    pub struct NvmmFramePair {
        pub left: NvmmFrameInfo,
        pub right: NvmmFrameInfo,
    }

    struct CapturedNvmmFrame {
        info: NvmmFrameInfo,
        timestamp_ns: Option<u64>,
        clock: Option<gst::Clock>,
    }

    struct PairingLimits {
        initial_timeout: Duration,
        resync_timeout: Duration,
        max_drops: u32,
    }

    impl Default for PairingLimits {
        fn default() -> Self {
            Self {
                initial_timeout: Duration::from_secs(5),
                resync_timeout: Duration::from_secs(2),
                max_drops: 60,
            }
        }
    }

    fn release_frame(release: &mpsc::SyncSender<()>, label: &str) -> Result<(), SourceError> {
        release.try_send(()).map_err(|error| SourceError::Read {
            reason: format!("NVMM {label} frame release failed: {error}"),
        })
    }

    struct HeldNvmmFrame<'a> {
        frame: Option<CapturedNvmmFrame>,
        release: &'a mpsc::SyncSender<()>,
        label: &'static str,
    }

    impl HeldNvmmFrame<'_> {
        fn captured(&self) -> &CapturedNvmmFrame {
            self.frame.as_ref().unwrap()
        }

        fn release(mut self) -> Result<(), SourceError> {
            self.frame.take();
            release_frame(self.release, self.label)
        }

        fn into_info(mut self) -> NvmmFrameInfo {
            self.frame.take().unwrap().info
        }
    }

    impl Drop for HeldNvmmFrame<'_> {
        fn drop(&mut self) {
            if self.frame.take().is_some() {
                let _ = release_frame(self.release, self.label);
            }
        }
    }

    fn receive_frame<'a>(
        frames: &mpsc::Receiver<CapturedNvmmFrame>,
        release: &'a mpsc::SyncSender<()>,
        label: &'static str,
        deadline: Instant,
    ) -> Result<Option<HeldNvmmFrame<'a>>, SourceError> {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(SourceError::Read {
                reason: format!("NVMM {label} frame wait exceeded the pairing deadline"),
            });
        }
        match frames.recv_timeout(remaining) {
            Ok(frame) => Ok(Some(HeldNvmmFrame {
                frame: Some(frame),
                release,
                label,
            })),
            Err(mpsc::RecvTimeoutError::Disconnected) => Ok(None),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(SourceError::Read {
                reason: format!("NVMM {label} frame wait exceeded the pairing deadline"),
            }),
        }
    }

    fn comparable_timestamps(
        left: &CapturedNvmmFrame,
        right: &CapturedNvmmFrame,
    ) -> Option<(u64, u64)> {
        if left.clock.is_some() && left.clock == right.clock {
            Some((left.timestamp_ns?, right.timestamp_ns?))
        } else {
            None
        }
    }

    fn sample_clock_time(sample: &gst::Sample, pipeline: &gst::Pipeline) -> Option<u64> {
        let pts = sample.buffer()?.pts()?;
        let segment = sample.segment()?.downcast_ref::<gst::ClockTime>()?;
        let running_time = segment.to_running_time(pts)?;
        pipeline
            .base_time()?
            .nseconds()
            .checked_add(running_time.nseconds())
    }

    fn build_nvmm_pipeline_string(
        device: &str,
        config: &CameraConfig,
        controls: &CameraControls,
        recording: Option<&SourceRecordingConfig>,
    ) -> Result<String, String> {
        let width = config.width;
        let height = config.height;
        let fps = config.fps;

        // Test override: RECO_NVMM_LEFT / RECO_NVMM_RIGHT env vars pipe a video
        // file through nvv4l2decoder to produce identical NVMM NV12 output.
        let env_key = if device == "0" {
            "RECO_NVMM_LEFT"
        } else {
            "RECO_NVMM_RIGHT"
        };
        let sink = if let Some(recording) = recording {
            format!(
                "tee name=capture_tee \
                 capture_tee. ! queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream ! \
                 appsink name=sink emit-signals=false sync=false max-buffers=2 drop=true \
                 capture_tee. ! queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 ! \
                 nvvidconv compute-hw=GPU ! video/x-raw,format=I420,colorimetry=bt601,width={},height={} ! \
                 x264enc name=source_encoder speed-preset=ultrafast tune=zerolatency pass=qual quantizer={} \
                 bitrate=100000 vbv-buf-capacity=0 threads=3 sliced-threads=false key-int-max={} ! \
                 h264parse name=source_parser ! video/x-h264,alignment=au ! \
                 matroskamux ! filesink name=source_file sync=false",
                recording.width,
                recording.height,
                recording.crf,
                fps.saturating_mul(2),
            )
        } else {
            "appsink name=sink emit-signals=false sync=false max-buffers=4 drop=true".into()
        };
        if let Ok(path) = std::env::var(env_key) {
            log::info!("[nvmm] {env_key}={path} (file override, bypassing camera)");
            return Ok(format!(
                "filesrc name=override_source ! qtdemux ! h264parse ! nvv4l2decoder ! \
                 video/x-raw(memory:NVMM),format=NV12 ! {sink}"
            ));
        }

        validate_device_string(device)?;
        let controls = nvargus_control_properties(controls)?;
        Ok(format!(
            "nvarguscamerasrc sensor-id={device}{controls} ! \
             video/x-raw(memory:NVMM),width={width},height={height},format=NV12,framerate={fps}/1 ! \
             {sink}"
        ))
    }

    #[derive(Default)]
    struct CaptureState {
        timing: SourceTiming,
        error: Option<String>,
    }

    fn install_source_probes(
        pipeline: &gst::Pipeline,
        state: &Arc<Mutex<CaptureState>>,
    ) -> Result<(), String> {
        for (element, pad_name, encoded) in [
            ("source_encoder", "sink", false),
            ("source_parser", "src", true),
        ] {
            let pad = pipeline
                .by_name(element)
                .and_then(|element| element.static_pad(pad_name))
                .ok_or_else(|| format!("source recording pad missing: {element}/{pad_name}"))?;
            let weak_pipeline = pipeline.downgrade();
            let state = state.clone();
            pad.add_probe(gst::PadProbeType::BUFFER, move |pad, info| {
                if let (Some(buffer), Some(pipeline)) = (info.buffer(), weak_pipeline.upgrade())
                    && let Some(pts) = buffer.pts()
                {
                    let clock_time = pad
                        .sticky_event::<gst::event::Segment>(0)
                        .and_then(|event| {
                            event
                                .segment()
                                .downcast_ref::<gst::ClockTime>()
                                .and_then(|segment| segment.to_running_time(pts))
                        })
                        .and_then(|running| {
                            pipeline
                                .base_time()?
                                .nseconds()
                                .checked_add(running.nseconds())
                        });
                    let mut state = state.lock().unwrap_or_else(|e| e.into_inner());
                    let timing = &mut state.timing;
                    if encoded {
                        timing.packets += 1;
                        if timing.first_pts_ns.is_none() {
                            timing.first_pts_ns = Some(pts.nseconds());
                            timing.first_clock_time_ns = clock_time;
                            timing.caps = pad.current_caps().map(|caps| caps.to_string());
                        }
                        timing.last_pts_ns = Some(pts.nseconds());
                        timing.last_clock_time_ns = clock_time;
                    } else if timing.first_input_pts_ns.is_none() {
                        timing.first_input_pts_ns = Some(pts.nseconds());
                        timing.first_input_clock_time_ns = clock_time;
                    }
                }
                gst::PadProbeReturn::Ok
            })
            .ok_or_else(|| format!("cannot monitor source recording: {element}"))?;
        }
        Ok(())
    }

    static NVMM_SHUTDOWN_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn finish_nvmm_pipeline(
        pipeline: gst::Pipeline,
        appsink: gst_app::AppSink,
        bus: gst::Bus,
        mut eos_seen: bool,
        require_eos: bool,
    ) -> Result<bool, String> {
        let mut cleanup_error = None;
        if !eos_seen {
            pipeline.send_event(gst::event::Eos::new());
            match bus.timed_pop_filtered(
                gst::ClockTime::from_seconds(3),
                &[gst::MessageType::Eos, gst::MessageType::Error],
            ) {
                Some(message) => match message.view() {
                    gst::MessageView::Eos(..) => eos_seen = true,
                    gst::MessageView::Error(error) => {
                        cleanup_error = Some(format!("finalizing camera file: {}", error.error()));
                    }
                    _ => {}
                },
                None if require_eos => {
                    cleanup_error =
                        Some("camera file EOS was not confirmed before the deadline".into());
                }
                None => {}
            }
        }
        // Stop both inputs before waiting for the other sensor's serialized Argus teardown.
        let _shutdown = NVMM_SHUTDOWN_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        if let Err(error) = pipeline.set_state(gst::State::Null) {
            cleanup_error = Some(format!("camera shutdown: {error}"));
        }
        let (settled, current, _) = pipeline.state(gst::ClockTime::from_seconds(2));
        if settled.is_err() || current != gst::State::Null {
            cleanup_error = Some("camera did not reach NULL state before the deadline".into());
        }
        drop(appsink);
        drop(pipeline);
        cleanup_error.map_or(Ok(eos_seen), Err)
    }

    fn spawn_nvmm_capture_thread(
        device: String,
        label: &'static str,
        config: CameraConfig,
        controls: CameraControls,
        stop: Arc<AtomicBool>,
        recording: Option<SourceRecordingConfig>,
        state: Arc<Mutex<CaptureState>>,
    ) -> (
        mpsc::Receiver<CapturedNvmmFrame>,
        mpsc::SyncSender<()>,
        std::thread::JoinHandle<()>,
    ) {
        let (info_tx, info_rx) = mpsc::sync_channel::<CapturedNvmmFrame>(2);
        let (release_tx, release_rx) = mpsc::sync_channel::<()>(4);

        let thread = std::thread::Builder::new()
            .name(format!("nvmm_{label}"))
            .spawn(move || {
                let result = (|| -> Result<(), String> {
                let pipeline_str = build_nvmm_pipeline_string(&device, &config, &controls, recording.as_ref())?;

                log::info!("[{label}] NVMM pipeline: {pipeline_str}");

                let pipeline = gst::parse::launch(&pipeline_str)
                    .map_err(|e| format!("NVMM pipeline parse: {e}"))?
                    .downcast::<gst::Pipeline>()
                    .map_err(|_| "NVMM description is not a pipeline")?;
                    pipeline.use_clock(Some(&gst::SystemClock::obtain()));
                if let Some(file) = pipeline.by_name("override_source") {
                    let env_key = if device == "0" { "RECO_NVMM_LEFT" } else { "RECO_NVMM_RIGHT" };
                    file.set_property("location", std::env::var(env_key).map_err(|e| e.to_string())?);
                }
                if let Some(recording) = &recording {
                    let file = pipeline.by_name("source_file").ok_or("source filesink missing")?;
                    let path = recording.directory.join(format!("{label}.mkv"));
                    file.set_property("location", path.to_str().ok_or("source path is not UTF-8")?);
                    install_source_probes(&pipeline, &state)?;
                }
                let appsink = pipeline
                    .by_name("sink")
                    .ok_or("NVMM appsink not found")?
                    .downcast::<gst_app::AppSink>()
                    .map_err(|_| "NVMM sink is not an AppSink")?;
                appsink.set_max_buffers(4);
                appsink.set_drop(true);
                appsink.set_wait_on_eos(false);
                let mut prev_sample: Option<gst::Sample> = None;
                let mut first_frame = true;
                let mut eos_seen = false;
                let bus = pipeline.bus().ok_or("NVMM pipeline has no bus")?;
                let capture_result = (|| -> Result<(), String> {
                pipeline.set_state(gst::State::Playing).map_err(|e| format!("NVMM pipeline start: {e}"))?;
                log::info!("[{label}] NVMM capture started (sensor={device})");

                loop {
                    while let Some(message) = bus.pop() {
                        match message.view() {
                            gst::MessageView::Error(error) => return Err(format!("capture pipeline: {} ({:?})", error.error(), error.debug())),
                            gst::MessageView::Eos(..) => eos_seen = true,
                            _ => {},
                        }
                    }
                    if stop.load(Ordering::Relaxed) {
                        break;
                    }

                    let sample = match appsink.try_pull_sample(gst::ClockTime::from_mseconds(100)) {
                        Some(sample) => sample,
                        None if appsink.is_eos() => break,
                        None => continue,
                    };

                    let buffer = match sample.buffer() {
                        Some(b) => b,
                        None => {
                            return Err("NVMM sample has no buffer".into());
                        }
                    };

                    let map = match buffer.map_readable() {
                        Ok(m) => m,
                        Err(e) => {
                            return Err(format!("NVMM buffer map failed: {e}"));
                        }
                    };

                    let frame_info =
                        match unsafe { nvmm::extract_nvmm_frame_info(map.as_slice().as_ptr()) } {
                            Ok(info) => info,
                            Err(e) => {
                                return Err(format!("NVMM extract failed: {e}"));
                            }
                        };

                        // shortcut: source recording is validated for NV12 BT.601 limited; negotiate other matrices before enabling them.
                        if recording.is_some() && frame_info.color_format != 6 {
                            return Err(format!("source recording requires NV12 BT.601 limited (format 6), camera reported {}", frame_info.color_format));
                        }

                    drop(map);

                    let timestamp_ns = sample_clock_time(&sample, &pipeline);
                    let clock = pipeline.clock();
                    if first_frame {
                        log::info!(
                            "[{label}] NVMM first frame: color_format={} size={}x{} caps={:?} pts_ns={:?} clock_time_ns={timestamp_ns:?} clock={:?}",
                            frame_info.color_format,
                            frame_info.width,
                            frame_info.height,
                            sample.caps(),
                            buffer.pts().map(|time| time.nseconds()),
                            clock.as_ref().map(|clock| clock.name()),
                        );
                        first_frame = false;
                    }

                    // Release the previous sample now that the render thread
                    // has had a full frame to use the prior DMA-buf.
                    if let Some(prev) = prev_sample.take() {
                        drop(prev);
                    }

                    if info_tx
                        .send(CapturedNvmmFrame {
                            info: frame_info,
                            timestamp_ns,
                            clock,
                        })
                        .is_err()
                    {
                        break;
                    }

                    prev_sample = Some(sample);

                    // Wait for the render thread to signal it's done importing
                    // this frame's DMA-buf before we let the next pull_sample
                    // potentially recycle the buffer.
                    loop {
                        match release_rx.recv_timeout(Duration::from_millis(100)) {
                            Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                            Err(mpsc::RecvTimeoutError::Timeout) => {
                                if stop.load(Ordering::Relaxed) {
                                    break;
                                }
                            }
                        }
                    }
                }
                Ok(())
                })();

                    if let Err(error) = &capture_result {
                        state.lock().unwrap_or_else(|e| e.into_inner()).error = Some(format!("{label}: {error}"));
                        stop.store(true, Ordering::Relaxed);
                    }

                drop(prev_sample);
                let cleanup = finish_nvmm_pipeline(pipeline, appsink, bus, eos_seen, recording.is_some());
                log::info!("[{label}] NVMM capture stopped, finalization={cleanup:?}");
                capture_result.and(cleanup.map(|_| ()))
                })();
                if let Err(error) = result {
                    state.lock().unwrap_or_else(|e| e.into_inner()).error = Some(format!("{label}: {error}"));
                    stop.store(true, Ordering::Relaxed);
                    log::error!("[{label}] {error}");
                }
            })
            .expect("spawn NVMM capture thread");

        (info_rx, release_tx, thread)
    }

    /// Stereo NVMM camera source for zero-copy Vulkan import + NvBufSurfTransform.
    ///
    /// Each call to `next_pair()` returns DMA-buf fds and NvBufSurface pointers
    /// for both cameras. The caller must call `release_previous()` after GPU
    /// work (rendering + detection) is submitted, before calling `next_pair()`
    /// again - this signals the capture threads to release the old GstSample
    /// and pull the next frame.
    pub struct GstreamerNvmmCameraSource {
        left_rx: mpsc::Receiver<CapturedNvmmFrame>,
        right_rx: mpsc::Receiver<CapturedNvmmFrame>,
        left_release: mpsc::SyncSender<()>,
        right_release: mpsc::SyncSender<()>,
        info: SourceInfo,
        stop: Arc<AtomicBool>,
        pair_count: u64,
        pair_held: Cell<bool>,
        dropped_left: u64,
        dropped_right: u64,
        pairing_limits: PairingLimits,
        threads: Vec<std::thread::JoinHandle<()>>,
        worker_states: [Arc<Mutex<CaptureState>>; 2],
        recording: Option<SourceRecording>,
        finish_error: Option<String>,
    }

    impl GstreamerNvmmCameraSource {
        /// Open a stereo NVMM camera source.
        pub fn open(config: &CameraConfig) -> Result<Self, SourceError> {
            Self::open_with_controls(config, &CameraControls::default())
        }

        /// Open an NVMM source with optional NVIDIA ISP controls.
        pub fn open_with_controls(
            config: &CameraConfig,
            controls: &CameraControls,
        ) -> Result<Self, SourceError> {
            Self::open_with_recording(config, controls, None)
        }

        /// Open NVMM capture with optional independent pre-stitch recording.
        pub fn open_with_recording(
            config: &CameraConfig,
            controls: &CameraControls,
            recording: Option<SourceRecording>,
        ) -> Result<Self, SourceError> {
            validate_controls_for_platform(controls).map_err(|reason| SourceError::Init {
                path: format!("{} + {}", config.left_device, config.right_device),
                reason,
            })?;
            gst::init().map_err(|e| SourceError::Init {
                path: format!("{} + {}", config.left_device, config.right_device),
                reason: format!("GStreamer init: {e}"),
            })?;

            let stop = Arc::new(AtomicBool::new(false));
            let worker_states = [
                Arc::new(Mutex::new(CaptureState::default())),
                Arc::new(Mutex::new(CaptureState::default())),
            ];
            let recording_config = recording.as_ref().map(|r| r.config().clone());

            let (left_rx, left_release, left_thread) = spawn_nvmm_capture_thread(
                config.left_device.clone(),
                "left",
                config.clone(),
                controls.clone(),
                stop.clone(),
                recording_config.clone(),
                worker_states[0].clone(),
            );
            let (right_rx, right_release, right_thread) = spawn_nvmm_capture_thread(
                config.right_device.clone(),
                "right",
                config.clone(),
                controls.clone(),
                stop.clone(),
                recording_config,
                worker_states[1].clone(),
            );

            let info = SourceInfo {
                width: config.width,
                height: config.height,
                fps: config.fps as f64,
                fps_rational: None,
                total_frames: None,
            };

            log::info!(
                "NVMM stereo source: {}x{} @ {}fps, zero-copy DMA-buf + NvBufSurfTransform",
                config.width,
                config.height,
                config.fps,
            );

            Ok(Self {
                left_rx,
                right_rx,
                left_release,
                right_release,
                info,
                stop,
                pair_count: 0,
                pair_held: Cell::new(false),
                dropped_left: 0,
                dropped_right: 0,
                pairing_limits: PairingLimits::default(),
                threads: vec![left_thread, right_thread],
                worker_states,
                recording,
                finish_error: None,
            })
        }

        /// Get the next stereo NVMM frame pair.
        ///
        /// Waits for a timestamp-matched pair. Returns `None` on EOS.
        pub fn next_pair(&mut self) -> Result<Option<NvmmFramePair>, SourceError> {
            if let Some(reason) = self.worker_error() {
                return Err(SourceError::Read { reason });
            }
            if self.pair_held.get() {
                return Err(SourceError::Read {
                    reason: "release the previous NVMM pair before requesting another".into(),
                });
            }
            if self.stop.load(Ordering::Relaxed) {
                return Ok(None);
            }
            let result = self.read_pair();
            if !matches!(result, Ok(Some(_))) {
                self.stop.store(true, Ordering::Relaxed);
                self.release_queued();
            }
            if let Some(reason) = self.worker_error() {
                self.release_previous();
                Err(SourceError::Read { reason })
            } else {
                result
            }
        }

        fn read_pair(&mut self) -> Result<Option<NvmmFramePair>, SourceError> {
            let initial_deadline = Instant::now() + self.pairing_limits.initial_timeout;
            let Some(mut left) =
                receive_frame(&self.left_rx, &self.left_release, "left", initial_deadline)?
            else {
                return Ok(None);
            };
            let Some(mut right) = receive_frame(
                &self.right_rx,
                &self.right_release,
                "right",
                initial_deadline,
            )?
            else {
                return Ok(None);
            };
            let fps = if self.info.fps.is_finite() && self.info.fps > 0.0 {
                self.info.fps
            } else {
                30.0
            };
            let max_skew_ns = ((500_000_000.0 / fps) as u64).saturating_add(2_000_000);
            let resync_deadline = Instant::now() + self.pairing_limits.resync_timeout;
            let mut drops = 0;
            while let Some((left_ns, right_ns)) =
                comparable_timestamps(left.captured(), right.captured())
            {
                if left_ns.abs_diff(right_ns) <= max_skew_ns {
                    break;
                }
                if drops >= self.pairing_limits.max_drops || Instant::now() >= resync_deadline {
                    return Err(SourceError::Read {
                        reason: format!(
                            "NVMM stereo synchronization failed after {drops} drops: skew={:.3}ms exceeds {:.3}ms",
                            left_ns.abs_diff(right_ns) as f64 / 1_000_000.0,
                            max_skew_ns as f64 / 1_000_000.0,
                        ),
                    });
                }
                if left_ns < right_ns {
                    left.release()?;
                    self.dropped_left = self.dropped_left.saturating_add(1);
                    left = match receive_frame(
                        &self.left_rx,
                        &self.left_release,
                        "left resync",
                        resync_deadline,
                    )? {
                        Some(frame) => frame,
                        None => return Ok(None),
                    };
                } else {
                    right.release()?;
                    self.dropped_right = self.dropped_right.saturating_add(1);
                    right = match receive_frame(
                        &self.right_rx,
                        &self.right_release,
                        "right resync",
                        resync_deadline,
                    )? {
                        Some(frame) => frame,
                        None => return Ok(None),
                    };
                }
                drops += 1;
            }

            self.pair_count = self.pair_count.saturating_add(1);
            if self.pair_count == 1 || self.pair_count.is_multiple_of(300) {
                if let Some((left_ns, right_ns)) =
                    comparable_timestamps(left.captured(), right.captured())
                {
                    log::info!(
                        "NVMM stereo timing: pair={} right_minus_left_ms={:.3} dropped_left={} dropped_right={} (GStreamer timestamps, not hardware synchronization)",
                        self.pair_count,
                        (right_ns as f64 - left_ns as f64) / 1_000_000.0,
                        self.dropped_left,
                        self.dropped_right,
                    );
                } else {
                    log::info!(
                        "NVMM stereo timing: pair={} unsynchronized (missing timestamps or different clocks), dropped_left={} dropped_right={}",
                        self.pair_count,
                        self.dropped_left,
                        self.dropped_right,
                    );
                }
            }
            self.pair_held.set(true);
            Ok(Some(NvmmFramePair {
                left: left.into_info(),
                right: right.into_info(),
            }))
        }

        /// Signal capture threads that GPU work on the current frame is done.
        ///
        /// Must be called after rendering + detection are submitted for the
        /// frame returned by the most recent `next_pair()`.
        pub fn release_previous(&self) {
            if self.pair_held.replace(false) {
                for (release, label) in
                    [(&self.left_release, "left"), (&self.right_release, "right")]
                {
                    if let Err(error) = release_frame(release, label) {
                        self.stop.store(true, Ordering::Relaxed);
                        log::error!("{error}");
                    }
                }
            }
        }

        fn release_queued(&self) {
            for (frames, release, label) in [
                (&self.left_rx, &self.left_release, "left"),
                (&self.right_rx, &self.right_release, "right"),
            ] {
                for _ in 0..2 {
                    if frames.try_recv().is_err() {
                        break;
                    }
                    let _ = release_frame(release, label);
                }
            }
        }

        /// Source metadata.
        pub fn info(&self) -> SourceInfo {
            self.info.clone()
        }

        /// Stop capture gracefully.
        pub fn stop(&self) {
            self.stop.store(true, Ordering::Relaxed);
        }

        fn worker_error(&self) -> Option<String> {
            self.worker_states.iter().find_map(|state| {
                state
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .error
                    .clone()
            })
        }

        /// Join camera workers and verify file EOS before completing the manifest.
        /// Pass a processing failure to retain the source session as incomplete.
        pub fn finish(&mut self, capture_error: Option<&str>) -> Result<(), SourceError> {
            self.stop.store(true, Ordering::Relaxed);
            self.release_previous();
            let deadline = Instant::now() + Duration::from_secs(12);
            while self.threads.iter().any(|thread| !thread.is_finished())
                && Instant::now() < deadline
            {
                self.release_queued();
                std::thread::sleep(Duration::from_millis(10));
            }
            self.release_queued();
            let mut remaining = Vec::new();
            for thread in self.threads.drain(..) {
                if !thread.is_finished() {
                    remaining.push(thread);
                    self.finish_error =
                        Some("camera worker teardown exceeded twelve seconds".into());
                } else if thread.join().is_err() {
                    self.finish_error =
                        Some("camera worker panicked during capture or teardown".into());
                }
            }
            self.threads = remaining;
            let error = capture_error
                .map(str::to_owned)
                .or_else(|| self.finish_error.clone())
                .or_else(|| self.worker_error());
            if let Some(mut recording) = self.recording.take() {
                let timing = std::array::from_fn(|index| {
                    self.worker_states[index]
                        .lock()
                        .unwrap_or_else(|e| e.into_inner())
                        .timing
                        .clone()
                });
                if let Err(reason) = recording.finish(&timing, error.as_deref()) {
                    self.finish_error = Some(reason);
                }
            }
            self.finish_error = self.finish_error.clone().or(error);
            self.finish_error
                .clone()
                .map_or(Ok(()), |reason| Err(SourceError::Read { reason }))
        }
    }

    impl Drop for GstreamerNvmmCameraSource {
        fn drop(&mut self) {
            let error = self
                .recording
                .as_ref()
                .map(|_| "capture exited without explicit finalization");
            if let Err(error) = self.finish(error) {
                log::error!("NVMM teardown: {error}");
            }
        }
    }

    #[cfg(test)]
    mod timing_tests {
        use super::*;

        #[test]
        fn both_cameras_receive_eos_before_serialized_teardown() {
            gst::init().unwrap();
            let (sender, receiver) = mpsc::channel();
            let mut cameras = Vec::new();
            for index in 0..2 {
                let pipeline = gst::Pipeline::new();
                let source = gst::ElementFactory::make("fakesrc")
                    .property("is-live", true)
                    .build()
                    .unwrap();
                let appsink = gst_app::AppSink::builder()
                    .sync(false)
                    .async_(false)
                    .max_buffers(1)
                    .drop(true)
                    .wait_on_eos(false)
                    .build();
                pipeline.add_many([&source, appsink.upcast_ref()]).unwrap();
                source.link(&appsink).unwrap();
                let sender = sender.clone();
                appsink.static_pad("sink").unwrap().add_probe(
                    gst::PadProbeType::EVENT_DOWNSTREAM,
                    move |_, info| {
                        if info
                            .event()
                            .is_some_and(|event| event.type_() == gst::EventType::Eos)
                        {
                            let _ = sender.send(index);
                        }
                        gst::PadProbeReturn::Ok
                    },
                );
                let bus = pipeline.bus().unwrap();
                pipeline.set_state(gst::State::Playing).unwrap();
                cameras.push((pipeline, appsink, bus));
            }
            let shutdown = NVMM_SHUTDOWN_LOCK.lock().unwrap();
            let threads: Vec<_> = cameras
                .into_iter()
                .map(|(pipeline, appsink, bus)| {
                    std::thread::spawn(move || {
                        finish_nvmm_pipeline(pipeline, appsink, bus, false, true)
                    })
                })
                .collect();
            let first = receiver.recv_timeout(Duration::from_secs(2));
            let second = receiver.recv_timeout(Duration::from_secs(2));
            drop(shutdown);
            for thread in threads {
                assert!(thread.join().unwrap().unwrap());
            }
            assert_ne!(first.unwrap(), second.unwrap());
        }

        struct Feed {
            frames: mpsc::SyncSender<CapturedNvmmFrame>,
            releases: mpsc::Receiver<()>,
        }

        #[test]
        fn worker_failure_is_not_reported_as_clean_eos() {
            let (mut source, _left, _right) = source_and_feeds();
            source.worker_states[0].lock().unwrap().error = Some("left: disk write failed".into());
            assert!(
                source
                    .next_pair()
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("disk write failed")
            );
            assert!(
                source
                    .finish(None)
                    .unwrap_err()
                    .to_string()
                    .contains("disk write failed")
            );
        }

        fn source_and_feeds() -> (GstreamerNvmmCameraSource, Feed, Feed) {
            gst::init().unwrap();
            let (left_tx, left_rx) = mpsc::sync_channel(2);
            let (right_tx, right_rx) = mpsc::sync_channel(2);
            let (left_release, left_releases) = mpsc::sync_channel(4);
            let (right_release, right_releases) = mpsc::sync_channel(4);
            (
                GstreamerNvmmCameraSource {
                    left_rx,
                    right_rx,
                    left_release,
                    right_release,
                    info: SourceInfo {
                        width: 2,
                        height: 2,
                        fps: 30.0,
                        fps_rational: None,
                        total_frames: None,
                    },
                    stop: Arc::new(AtomicBool::new(false)),
                    pair_count: 0,
                    pair_held: Cell::new(false),
                    dropped_left: 0,
                    dropped_right: 0,
                    pairing_limits: PairingLimits::default(),
                    threads: Vec::new(),
                    worker_states: [
                        Arc::new(Mutex::new(CaptureState::default())),
                        Arc::new(Mutex::new(CaptureState::default())),
                    ],
                    recording: None,
                    finish_error: None,
                },
                Feed {
                    frames: left_tx,
                    releases: left_releases,
                },
                Feed {
                    frames: right_tx,
                    releases: right_releases,
                },
            )
        }

        fn frame(
            id: i32,
            timestamp_ms: Option<u64>,
            clock: Option<&gst::Clock>,
        ) -> CapturedNvmmFrame {
            CapturedNvmmFrame {
                info: NvmmFrameInfo {
                    color_format: 6,
                    dmabuf_fd: id,
                    width: 2,
                    height: 2,
                    y_offset: 0,
                    y_pitch: 2,
                    uv_offset: 4,
                    uv_pitch: 2,
                    total_size: 6,
                    surface_ptr: std::ptr::null_mut(),
                },
                timestamp_ns: timestamp_ms.map(|time| time * 1_000_000),
                clock: clock.cloned(),
            }
        }

        fn assert_no_release(feed: &Feed) {
            assert_eq!(feed.releases.try_recv(), Err(mpsc::TryRecvError::Empty));
        }

        fn wait_release(feed: &Feed) {
            feed.releases.recv_timeout(Duration::from_secs(1)).unwrap();
        }

        #[test]
        fn catchup_holds_newer_across_stale_backlog_in_both_directions() {
            for older_left in [true, false] {
                let (mut source, left, right) = source_and_feeds();
                let clock = gst::SystemClock::obtain();
                let (older, newer) = if older_left {
                    (&left, &right)
                } else {
                    (&right, &left)
                };
                older.frames.send(frame(0, Some(0), Some(&clock))).unwrap();
                newer
                    .frames
                    .send(frame(999, Some(150), Some(&clock)))
                    .unwrap();
                let reader = std::thread::spawn(move || {
                    let pair = source.next_pair();
                    (source, pair)
                });
                for next_ms in [33, 66, 100, 133] {
                    wait_release(older);
                    assert_no_release(newer);
                    older
                        .frames
                        .send(frame(next_ms as i32, Some(next_ms), Some(&clock)))
                        .unwrap();
                }
                let (mut source, result) = reader.join().unwrap();
                let pair = result.unwrap().unwrap();
                assert_eq!(
                    (pair.left.dmabuf_fd, pair.right.dmabuf_fd),
                    if older_left { (133, 999) } else { (999, 133) }
                );
                assert_eq!(
                    (source.dropped_left, source.dropped_right),
                    if older_left { (4, 0) } else { (0, 4) }
                );
                assert_no_release(older);
                assert_no_release(newer);
                assert!(source.next_pair().is_err());
                assert_no_release(older);
                assert_no_release(newer);
                source.release_previous();
                source.release_previous();
                wait_release(older);
                wait_release(newer);
                assert_no_release(older);
                assert_no_release(newer);
                drop(source);
                assert_eq!(
                    older.releases.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                );
                assert_eq!(
                    newer.releases.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                );
            }
        }

        #[test]
        fn duplicate_release_does_not_advance_producers_past_the_accepted_pair() {
            let (mut source, left, right) = source_and_feeds();
            let clock = gst::SystemClock::obtain();
            let (events, released) = mpsc::channel();
            let mut producers = Vec::new();
            for (label, feed) in [("left", left), ("right", right)] {
                let events = events.clone();
                let clock = clock.clone();
                producers.push(std::thread::spawn(move || {
                    for id in 1..=2 {
                        feed.frames
                            .send(frame(id, Some(id as u64 * 33), Some(&clock)))
                            .unwrap();
                        wait_release(&feed);
                        events.send((label, id)).unwrap();
                    }
                }));
            }
            let first = source.next_pair().unwrap().unwrap();
            assert_eq!((first.left.dmabuf_fd, first.right.dmabuf_fd), (1, 1));
            source.release_previous();
            source.release_previous();
            for _ in 0..2 {
                assert_eq!(released.recv_timeout(Duration::from_secs(1)).unwrap().1, 1);
            }
            let second = source.next_pair().unwrap().unwrap();
            assert_eq!((second.left.dmabuf_fd, second.right.dmabuf_fd), (2, 2));
            assert_eq!(
                released.recv_timeout(Duration::from_millis(40)),
                Err(mpsc::RecvTimeoutError::Timeout)
            );
            source.release_previous();
            for _ in 0..2 {
                assert_eq!(released.recv_timeout(Duration::from_secs(1)).unwrap().1, 2);
            }
            for producer in producers {
                producer.join().unwrap();
            }
            assert!(source.next_pair().unwrap().is_none());
        }

        #[test]
        fn eos_during_replacement_releases_only_the_held_peer() {
            for older_left in [true, false] {
                let (mut source, left, right) = source_and_feeds();
                let clock = gst::SystemClock::obtain();
                let (older, newer) = if older_left {
                    (left, right)
                } else {
                    (right, left)
                };
                older.frames.send(frame(1, Some(0), Some(&clock))).unwrap();
                newer
                    .frames
                    .send(frame(2, Some(100), Some(&clock)))
                    .unwrap();
                let reader = std::thread::spawn(move || {
                    let result = source.next_pair();
                    (source, result)
                });
                wait_release(&older);
                assert_no_release(&newer);
                drop(older.frames);
                let (mut source, result) = reader.join().unwrap();
                assert!(result.unwrap().is_none());
                wait_release(&newer);
                source.release_previous();
                assert!(source.next_pair().unwrap().is_none());
                assert_no_release(&newer);
                assert_eq!(older.releases.try_recv(), Err(mpsc::TryRecvError::Empty));
                drop(source);
                assert_eq!(
                    newer.releases.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                );
            }
        }

        #[test]
        fn initial_eos_releases_the_received_or_queued_peer_once() {
            for left_ends in [true, false] {
                let (mut source, left, right) = source_and_feeds();
                let (ended, peer) = if left_ends {
                    (left, right)
                } else {
                    (right, left)
                };
                peer.frames.send(frame(1, None, None)).unwrap();
                drop(ended.frames);
                assert!(source.next_pair().unwrap().is_none());
                wait_release(&peer);
                assert_no_release(&peer);
                drop(source);
                assert_eq!(
                    peer.releases.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                );
                assert_eq!(
                    ended.releases.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                );
            }
        }

        #[test]
        fn dropping_source_releases_an_accepted_pair_once() {
            let (mut source, left, right) = source_and_feeds();
            left.frames.send(frame(1, None, None)).unwrap();
            right.frames.send(frame(2, None, None)).unwrap();
            assert!(source.next_pair().unwrap().is_some());
            assert_no_release(&left);
            assert_no_release(&right);
            drop(source);
            wait_release(&left);
            wait_release(&right);
            assert_eq!(
                left.releases.try_recv(),
                Err(mpsc::TryRecvError::Disconnected)
            );
            assert_eq!(
                right.releases.try_recv(),
                Err(mpsc::TryRecvError::Disconnected)
            );
        }

        #[test]
        fn drop_joins_a_worker_and_releases_a_frame_after_the_old_grace_period() {
            let (mut source, left, _right) = source_and_feeds();
            let stop = source.stop.clone();
            let released = Arc::new(AtomicBool::new(false));
            let producer_released = released.clone();
            let producer = std::thread::spawn(move || {
                while !stop.load(Ordering::Relaxed) {
                    std::thread::sleep(Duration::from_millis(1));
                }
                std::thread::sleep(Duration::from_millis(600));
                left.frames.send(frame(1, None, None)).unwrap();
                wait_release(&left);
                producer_released.store(true, Ordering::Relaxed);
            });
            source.threads.push(producer);
            drop(source);
            assert!(released.load(Ordering::Relaxed));
        }

        #[test]
        fn missing_or_incomparable_clock_metadata_preserves_fifo() {
            let (mut source, left, right) = source_and_feeds();
            let clock = gst::SystemClock::obtain();
            let other = gst::glib::Object::new::<gst::SystemClock>().upcast::<gst::Clock>();
            assert_ne!(clock, other);
            for (left_time, left_clock, right_clock) in [
                (None, Some(&clock), Some(&clock)),
                (Some(0), None, Some(&clock)),
                (Some(0), Some(&clock), Some(&other)),
            ] {
                left.frames.send(frame(1, left_time, left_clock)).unwrap();
                right
                    .frames
                    .send(frame(2, Some(1000), right_clock))
                    .unwrap();
                let pair = source.next_pair().unwrap().unwrap();
                assert_eq!((pair.left.dmabuf_fd, pair.right.dmabuf_fd), (1, 2));
                assert_eq!((source.dropped_left, source.dropped_right), (0, 0));
                assert_no_release(&left);
                assert_no_release(&right);
                source.release_previous();
                wait_release(&left);
                wait_release(&right);
            }
        }

        #[test]
        fn never_converging_timestamps_stop_after_the_drop_budget() {
            let (mut source, left, right) = source_and_feeds();
            let clock = gst::SystemClock::obtain();
            left.frames.send(frame(0, Some(0), Some(&clock))).unwrap();
            right
                .frames
                .send(frame(1, Some(1000), Some(&clock)))
                .unwrap();
            let reader = std::thread::spawn(move || {
                let result = source.next_pair();
                (source, result)
            });
            for next_ms in 1..=60 {
                wait_release(&left);
                assert_no_release(&right);
                left.frames
                    .send(frame(next_ms as i32, Some(next_ms), Some(&clock)))
                    .unwrap();
            }
            let (source, result) = reader.join().unwrap();
            assert!(result.err().unwrap().to_string().contains("after 60 drops"));
            assert_eq!(source.dropped_left, 60);
            assert_eq!(source.pair_count, 0);
            wait_release(&left);
            wait_release(&right);
            assert_no_release(&left);
            assert_no_release(&right);
        }

        #[test]
        fn replacement_timeout_releases_the_newer_peer_once() {
            let (mut source, left, right) = source_and_feeds();
            source.pairing_limits.resync_timeout = Duration::from_millis(40);
            let clock = gst::SystemClock::obtain();
            left.frames.send(frame(0, Some(0), Some(&clock))).unwrap();
            right
                .frames
                .send(frame(1, Some(100), Some(&clock)))
                .unwrap();
            let started = Instant::now();
            let error = source.next_pair().err().unwrap().to_string();
            assert!(error.contains("left resync") && error.contains("deadline"));
            assert!(started.elapsed() < Duration::from_secs(1));
            wait_release(&left);
            wait_release(&right);
            assert_no_release(&left);
            assert_no_release(&right);
        }

        #[test]
        fn first_frame_deadline_is_shared_across_both_cameras() {
            let (mut source, left, right) = source_and_feeds();
            source.pairing_limits.initial_timeout = Duration::from_millis(300);
            let (started_tx, started_rx) = mpsc::channel();
            let reader = std::thread::spawn(move || {
                let started = Instant::now();
                started_tx.send(()).unwrap();
                let result = source.next_pair();
                (source, result, started.elapsed())
            });
            started_rx.recv().unwrap();
            std::thread::sleep(Duration::from_millis(200));
            left.frames.send(frame(0, None, None)).unwrap();
            let (_source, result, elapsed) = reader.join().unwrap();
            assert!(
                result
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("right frame wait")
            );
            assert!(elapsed < Duration::from_millis(450), "{elapsed:?}");
            wait_release(&left);
            assert_no_release(&left);
            assert_no_release(&right);
        }

        #[test]
        fn full_release_queue_returns_an_error_without_blocking() {
            let (mut source, left, right) = source_and_feeds();
            let clock = gst::SystemClock::obtain();
            for _ in 0..4 {
                source.left_release.try_send(()).unwrap();
            }
            left.frames.send(frame(0, Some(0), Some(&clock))).unwrap();
            right
                .frames
                .send(frame(1, Some(100), Some(&clock)))
                .unwrap();
            let started = Instant::now();
            assert!(
                source
                    .next_pair()
                    .err()
                    .unwrap()
                    .to_string()
                    .contains("frame release failed")
            );
            assert!(started.elapsed() < Duration::from_secs(1));
            for _ in 0..4 {
                wait_release(&left);
            }
            wait_release(&right);
            assert_no_release(&left);
            assert_no_release(&right);
        }

        #[test]
        fn timestamp_uses_segment_running_time_and_pipeline_base() {
            gst::init().unwrap();
            let pipeline = gst::Pipeline::new();
            pipeline.set_base_time(gst::ClockTime::from_seconds(100));
            let mut buffer = gst::Buffer::new();
            buffer
                .get_mut()
                .unwrap()
                .set_pts(gst::ClockTime::from_seconds(7));
            let mut segment = gst::FormattedSegment::<gst::ClockTime>::new();
            segment.set_start(gst::ClockTime::from_seconds(5));
            let sample = gst::Sample::builder()
                .buffer(&buffer)
                .segment(&segment)
                .build();
            assert_eq!(sample_clock_time(&sample, &pipeline), Some(102_000_000_000));
            assert_eq!(
                sample_clock_time(&gst::Sample::builder().build(), &pipeline),
                None
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        AwbMode, CameraControls, CaptureFormat, ExposureMode, GstreamerNv12CameraSource, Nv12Data,
        Nv12FramePair, SourceInfo, build_nvargus_pipeline_string, is_tegra,
        nvargus_control_properties, validate_controls_for_platform, validate_device_string,
    };
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    };

    // Helper: on non-Jetson Linux builds (the CI environment), V4L2 rules apply.
    // On macOS/Windows CI the numeric rules apply. We test the logic that is
    // actually compiled in, plus explicitly call the Tegra/numeric branch via
    // the shared predicate (numeric-only) which is the same for all three
    // non-V4L2 platforms.

    /// Numeric strings must be accepted on every non-V4L2 platform.
    #[test]
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    fn numeric_index_accepted() {
        assert!(validate_device_string("0").is_ok());
        assert!(validate_device_string("1").is_ok());
        assert!(validate_device_string("12").is_ok());
    }

    /// Non-numeric strings must be rejected on macOS/Windows.
    #[test]
    #[cfg(any(target_os = "macos", target_os = "windows"))]
    fn non_numeric_rejected_on_non_linux() {
        assert!(validate_device_string("").is_err());
        assert!(validate_device_string("cam0").is_err());
        assert!(validate_device_string("0 ! fakesrc").is_err());
        assert!(validate_device_string("/dev/video0").is_err());
    }

    /// Jetson uses sensor indices; other Linux hosts use V4L2 paths.
    #[test]
    #[cfg(target_os = "linux")]
    fn linux_device_matches_capture_backend() {
        for index in ["0", "1", "10"] {
            assert_eq!(validate_device_string(index).is_ok(), is_tegra());
            assert_eq!(
                validate_device_string(&format!("/dev/video{index}")).is_ok(),
                !is_tegra()
            );
        }
    }

    /// Injection attempts and malformed paths must be rejected on Linux.
    #[test]
    #[cfg(target_os = "linux")]
    fn injection_rejected_on_linux() {
        assert!(validate_device_string("").is_err());
        assert!(validate_device_string("/dev/video").is_err()); // no trailing digit
        assert!(validate_device_string("/dev/video0 ! fakesrc").is_err());
        assert!(validate_device_string("0 ! fakesrc").is_err());
        assert!(validate_device_string("/dev/video0a").is_err());
        assert!(validate_device_string("/dev/../etc/passwd").is_err());
        assert!(validate_device_string("video0").is_err());
    }

    #[test]
    fn capture_drop_drains_blocked_producer_and_joins() {
        let (tx, rx) = mpsc::sync_channel(1);
        let (started_tx, started_rx) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let stopped = Arc::new(AtomicBool::new(false));
        let producer_stop = stop.clone();
        let producer_stopped = stopped.clone();
        let thread = std::thread::spawn(move || {
            for _ in 0..3 {
                started_tx.send(()).unwrap();
                let empty = || Nv12Data {
                    y: vec![],
                    uv: vec![],
                };
                if tx
                    .send(Nv12FramePair {
                        left: empty(),
                        right: empty(),
                    })
                    .is_err()
                    || producer_stop.load(Ordering::Relaxed)
                {
                    break;
                }
            }
            producer_stopped.store(true, Ordering::Relaxed);
        });
        started_rx.recv().unwrap();
        started_rx.recv().unwrap();
        drop(GstreamerNv12CameraSource {
            rx,
            info: SourceInfo {
                width: 2,
                height: 2,
                fps: 30.0,
                fps_rational: None,
                total_frames: None,
            },
            stop,
            threads: vec![thread],
        });
        assert!(stopped.load(Ordering::Relaxed));
    }

    #[test]
    fn nvargus_controls_are_typed_and_bounded() {
        let controls = CameraControls {
            sensor_mode: Some(1),
            exposure_mode: ExposureMode::Manual,
            exposure_min_ns: Some(8_000_000),
            exposure_max_ns: Some(8_000_000),
            gain_min: Some(1.0),
            gain_max: Some(8.0),
            digital_gain_min: Some(1.0),
            digital_gain_max: Some(1.0),
            awb_mode: AwbMode::Daylight,
            awb_lock: true,
        };
        let properties = nvargus_control_properties(&controls).unwrap();
        assert!(properties.contains("sensor-mode=1"));
        assert!(properties.contains("exposuretimerange=\"8000000 8000000\""));
        assert!(properties.contains("aelock=true"));
        assert!(properties.contains("gainrange=\"1 8\""));
        assert!(properties.contains("ispdigitalgainrange=\"1 1\""));
        assert!(properties.contains("wbmode=5"));
        assert!(properties.contains("awblock=true"));

        let pipeline =
            build_nvargus_pipeline_string("0", 3840, 2160, 30, CaptureFormat::Nv12, &controls)
                .unwrap();
        assert!(pipeline.starts_with("nvarguscamerasrc sensor-id=0 sensor-mode=1"));
    }

    #[test]
    fn invalid_camera_controls_are_rejected() {
        let controls = CameraControls {
            exposure_mode: ExposureMode::Manual,
            exposure_min_ns: Some(4_000),
            exposure_max_ns: Some(8_000),
            ..CameraControls::default()
        };
        assert!(controls.validate().is_err());

        let controls = CameraControls {
            gain_min: Some(2.0),
            ..CameraControls::default()
        };
        assert!(controls.validate().is_err());
    }

    #[test]
    fn nvargus_controls_are_rejected_off_jetson() {
        if is_tegra() {
            return;
        }
        assert!(validate_controls_for_platform(&CameraControls::default()).is_ok());
        let controls = CameraControls {
            sensor_mode: Some(1),
            ..CameraControls::default()
        };
        assert!(validate_controls_for_platform(&controls).is_err());
    }
}
