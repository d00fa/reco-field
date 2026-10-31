//! Video encoder: RGBA frames → MP4 file (H.264, HEVC, or AV1).
//!
//! Wraps FFmpeg's muxer, encoder, and swscale to write RGBA pixel data
//! to an encoded MP4 file. Supports hardware-accelerated encoding
//! (NVENC, QSV, VideoToolbox, VAAPI) with automatic fallback to software.

extern crate ffmpeg_next as ffmpeg;

use ffmpeg::format::Pixel;
use ffmpeg::software::scaling::{context::Context as ScalingContext, flag::Flags as ScalingFlags};
use ffmpeg::util::frame::video::Video as VideoFrame;
use ffmpeg::{Rational, codec, encoder, format};
use std::ffi::{CString, c_void};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};
use thiserror::Error;

use super::hw_upload::{HardwareUpload, staging_pixel_format};

/// FFmpeg 4.x names the opaque VAAPI surface format `VAAPI_VLD`; 5.0+
/// renamed it to `VAAPI`.
#[cfg(reco_ffmpeg_vaapi_vld)]
pub(super) const VAAPI_PIXEL: Pixel = Pixel::VAAPI_VLD;
#[cfg(not(reco_ffmpeg_vaapi_vld))]
pub(super) const VAAPI_PIXEL: Pixel = Pixel::VAAPI;

/// Errors from the video encoder.
#[derive(Debug, Error)]
pub enum EncodeError {
    /// FFmpeg error.
    #[error("FFmpeg: {0}")]
    Ffmpeg(#[from] ffmpeg::Error),

    /// No encoder available for the requested codec.
    #[error("no encoder found for codec '{0}' — is FFmpeg built with the right encoder?")]
    CodecNotFound(String),

    /// Frame data has wrong size.
    #[error("frame data size mismatch: expected {expected} bytes, got {actual}")]
    FrameSizeMismatch { expected: usize, actual: usize },
}

/// Output video codec.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum VideoCodec {
    /// H.264 / AVC — widest compatibility.
    #[default]
    H264,
    /// H.265 / HEVC — better quality per bit, good hardware support.
    Hevc,
    /// AV1 — best compression, requires modern hardware for encoding.
    Av1,
}

impl VideoCodec {
    /// Parse from a string (case-insensitive).
    pub fn from_str_loose(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "h264" | "avc" | "h.264" => Some(Self::H264),
            "hevc" | "h265" | "h.265" => Some(Self::Hevc),
            "av1" => Some(Self::Av1),
            _ => None,
        }
    }

    fn candidates(self) -> &'static [EncoderCandidate] {
        match self {
            Self::H264 => H264_ENCODERS,
            Self::Hevc => HEVC_ENCODERS,
            Self::Av1 => AV1_ENCODERS,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Self::H264 => "H.264",
            Self::Hevc => "HEVC",
            Self::Av1 => "AV1",
        }
    }
}

struct EncoderCandidate {
    name: &'static str,
    is_hardware: bool,
    /// Pixel format the encoder is opened with. For most encoders this is the
    /// CPU swscale target (NV12 or YUV420P). `Pixel::VAAPI` is a sentinel
    /// meaning "GPU surface, sw_format = NV12": the CPU stages frames in NV12
    /// (see `staging_pixel_format`) and uploads them to a VAAPI surface before
    /// encoding (see `hw_upload`).
    pixel_format: Pixel,
}

/// Known H.264 encoder candidates, in preference order.
///
/// Covers: NVIDIA (NVENC), Intel (QSV), Apple (VideoToolbox), AMD (AMF/VAAPI),
/// embedded Linux (V4L2 M2M), Android (MediaCodec), Windows fallback (MF),
/// and software (libx264).
const H264_ENCODERS: &[EncoderCandidate] = &[
    EncoderCandidate {
        name: "h264_nvenc",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "h264_qsv",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "h264_videotoolbox",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "h264_amf",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "h264_vaapi",
        is_hardware: true,
        pixel_format: VAAPI_PIXEL,
    },
    EncoderCandidate {
        name: "h264_v4l2m2m",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "h264_mediacodec",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "h264_mf",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "libx264",
        is_hardware: false,
        pixel_format: Pixel::NV12,
    },
];

/// Known HEVC encoder candidates, in preference order.
const HEVC_ENCODERS: &[EncoderCandidate] = &[
    EncoderCandidate {
        name: "hevc_nvenc",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "hevc_qsv",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "hevc_videotoolbox",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "hevc_amf",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "hevc_vaapi",
        is_hardware: true,
        pixel_format: VAAPI_PIXEL,
    },
    EncoderCandidate {
        name: "hevc_v4l2m2m",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "hevc_mediacodec",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "hevc_mf",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "libx265",
        is_hardware: false,
        pixel_format: Pixel::YUV420P,
    },
];

/// Known AV1 encoder candidates, in preference order.
const AV1_ENCODERS: &[EncoderCandidate] = &[
    EncoderCandidate {
        name: "av1_nvenc",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "av1_qsv",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "av1_amf",
        is_hardware: true,
        pixel_format: Pixel::NV12,
    },
    EncoderCandidate {
        name: "av1_vaapi",
        is_hardware: true,
        pixel_format: VAAPI_PIXEL,
    },
    EncoderCandidate {
        name: "libsvtav1",
        is_hardware: false,
        pixel_format: Pixel::YUV420P,
    },
    EncoderCandidate {
        name: "libaom-av1",
        is_hardware: false,
        pixel_format: Pixel::YUV420P,
    },
];

/// Information about an available encoder.
#[derive(Debug, Clone)]
pub struct EncoderInfo {
    /// FFmpeg codec name (e.g., `"h264_nvenc"`, `"libx264"`).
    pub name: String,
    /// Human-readable description from FFmpeg.
    pub description: String,
    /// Whether this is a hardware-accelerated encoder.
    pub is_hardware: bool,
}

/// Detect which encoders are available for a given codec.
///
/// Returns encoders in preference order (hardware first, then software).
pub fn available_encoders(codec: VideoCodec) -> Vec<EncoderInfo> {
    crate::init();
    codec
        .candidates()
        .iter()
        .filter_map(|c| {
            encoder::find_by_name(c.name).map(|codec| EncoderInfo {
                name: c.name.to_string(),
                description: codec.description().to_string(),
                is_hardware: c.is_hardware,
            })
        })
        .collect()
}

/// Detect which H.264 encoders are available (convenience wrapper).
pub fn available_h264_encoders() -> Vec<EncoderInfo> {
    available_encoders(VideoCodec::H264)
}

fn auto_candidate_allowed(name: &str) -> bool {
    if name.ends_with("_nvenc") && !cuda_runtime_available() {
        return false;
    }

    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    if name.ends_with("_qsv") {
        return false;
    }

    #[cfg(target_os = "linux")]
    if name.ends_with("_qsv")
        && let Some(vaapi_name) = vaapi_peer_encoder(name)
        && encoder::find_by_name(vaapi_name).is_some()
    {
        return false;
    }

    #[cfg(not(target_os = "macos"))]
    if name.ends_with("_videotoolbox") {
        return false;
    }

    #[cfg(not(target_os = "windows"))]
    if name.ends_with("_amf") || name.ends_with("_mf") {
        return false;
    }

    #[cfg(not(target_os = "linux"))]
    if name.ends_with("_vaapi") || name.ends_with("_v4l2m2m") {
        return false;
    }

    #[cfg(not(target_os = "android"))]
    if name.ends_with("_mediacodec") {
        return false;
    }

    true
}

#[cfg(target_os = "linux")]
fn vaapi_peer_encoder(name: &str) -> Option<&'static str> {
    match name {
        "h264_qsv" => Some("h264_vaapi"),
        "hevc_qsv" => Some("hevc_vaapi"),
        "av1_qsv" => Some("av1_vaapi"),
        _ => None,
    }
}

fn cuda_runtime_available() -> bool {
    #[cfg(any(target_os = "linux", target_os = "windows"))]
    {
        reco_core::interop::cuda::is_cuda_available()
    }

    #[cfg(not(any(target_os = "linux", target_os = "windows")))]
    {
        false
    }
}

/// Encoder quality preset.
#[derive(Debug, Clone, Copy, Default)]
pub enum Quality {
    /// Prioritize speed over quality (for previewing / testing).
    Fast,
    /// Balanced speed and quality.
    #[default]
    Balanced,
    /// Prioritize quality (for final output).
    High,
}

/// Output container format.
///
/// The default [`Container::Mp4Fragmented`] is the right choice for
/// write-while-read workflows (replay backends, live uploads) because
/// fragmented MP4 writes a minimal `moov` atom up front and flushes
/// self-contained fragments on keyframes, so a concurrent reader can
/// parse the file mid-write. Plain [`Container::Mp4`] writes the
/// `moov` at close, so partial files are unreadable and concurrent
/// readers see only the header. [`Container::Matroska`] is the
/// crash-safe alternative with similar streaming properties.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Container {
    /// Plain MP4 (`.mp4`). Final-file-only; index written at close.
    /// Default for the stitch output path so the existing behavior
    /// is preserved; opt in to a streamable container explicitly
    /// when needed.
    #[default]
    Mp4,
    /// Fragmented MP4 (`.mp4`) with `empty_moov` + `frag_keyframe`
    /// movflags. Readable while still being written. Default for
    /// the stacked-video replay encoder.
    Mp4Fragmented,
    /// Matroska (`.mkv`). Naturally streamable; recommended by OBS
    /// for crash-safe recording.
    Matroska,
    /// FLV container for RTMP streaming. Required by most RTMP
    /// ingest endpoints (YouTube, Twitch).
    Flv,
    /// Discard the local muxed output while retaining the optional stream output.
    Null,
}

impl Container {
    /// FFmpeg muxer name for this container.
    fn muxer_name(self) -> &'static str {
        match self {
            Self::Mp4 | Self::Mp4Fragmented => "mp4",
            Self::Matroska => "matroska",
            Self::Flv => "flv",
            Self::Null => "null",
        }
    }

    /// Parse from a string (case-insensitive). Accepts `"mp4"`,
    /// `"fmp4"`/`"mp4-fragmented"`, and `"mkv"`/`"matroska"`.
    pub fn from_str_loose(s: &str) -> Option<Self> {
        match s.to_lowercase().as_str() {
            "mp4" => Some(Self::Mp4),
            "fmp4" | "mp4-fragmented" | "mp4_fragmented" => Some(Self::Mp4Fragmented),
            "mkv" | "matroska" => Some(Self::Matroska),
            "flv" => Some(Self::Flv),
            _ => None,
        }
    }
}

impl From<crate::output::Codec> for VideoCodec {
    fn from(c: crate::output::Codec) -> Self {
        match c {
            crate::output::Codec::H264 => Self::H264,
            crate::output::Codec::HEVC => Self::Hevc,
            crate::output::Codec::AV1 => Self::Av1,
        }
    }
}

impl From<crate::output::Quality> for Quality {
    fn from(q: crate::output::Quality) -> Self {
        match q {
            crate::output::Quality::Fast => Self::Fast,
            crate::output::Quality::Balanced => Self::Balanced,
            crate::output::Quality::High => Self::High,
        }
    }
}

impl From<crate::output::Format> for Container {
    fn from(f: crate::output::Format) -> Self {
        match f {
            crate::output::Format::Mp4 | crate::output::Format::Mov => Self::Mp4,
            crate::output::Format::Mp4Fragmented => Self::Mp4Fragmented,
            crate::output::Format::Mkv => Self::Matroska,
            crate::output::Format::Flv => Self::Flv,
        }
    }
}

/// Configuration for the video encoder.
#[derive(Debug, Clone, Default)]
pub struct EncoderConfig {
    /// Force a specific encoder by name, or `None` for auto-detection.
    pub encoder_name: Option<String>,
    /// Output video codec (H.264, HEVC, AV1). Default: H.264.
    pub codec: VideoCodec,
    /// Quality preset (Fast / Balanced / High). Controls default encoder
    /// tuning when no explicit [`quality`](Self::quality) override is set.
    pub quality_preset: Quality,
    /// Normalized quality override (0-100, higher = better). When set,
    /// replaces the encoder's default quality parameter with a value
    /// derived from this scale. The conversion is per-encoder:
    /// CRF-style encoders use `crf = 40.0 - (quality / 100.0) * 28.0`,
    /// VideoToolbox compresses to `global_quality = 40 + quality * 0.35`
    /// (range 40-75) with a maxrate cap to tame exponential bitrate growth.
    pub quality: Option<u8>,
    /// Override the encoder preset string (passed through to the encoder).
    pub preset: Option<String>,
    /// Source files to copy audio from, in order (stream copy, no re-encoding).
    /// For a chained recording, list every segment so audio spans the whole
    /// output; passing a single file copies just that one. The first audio
    /// stream of each file is used.
    pub audio_source: Option<Vec<std::path::PathBuf>>,
    /// Start copying audio at this source timestamp, in seconds.
    ///
    /// Used when the video export starts from a later processing window
    /// (`--start-time`, GUI export start). Copied audio packets are
    /// rebased so the selected timestamp lands at output time zero.
    pub audio_start_time: f64,
    /// Output container format. Defaults to plain MP4 to match the
    /// existing stitch-output behavior; opt in to fragmented MP4 or
    /// Matroska for streamable / write-while-read workflows (e.g.,
    /// the M6.5 stacked-video replay backend).
    pub container: Container,
    /// Override the encoder's group-of-pictures size (frames
    /// between keyframes). `None` leaves ffmpeg / libx264 defaults
    /// (typically 250 frames). Set a small value (e.g. 30) when
    /// the output needs frequent keyframes for seekable replay or
    /// fragmented-MP4 fragment flush cadence.
    pub gop_size: Option<u32>,
    /// Optional RTMP stream URL for simultaneous file + stream output.
    ///
    /// When set, the encoder writes encoded packets to both the primary
    /// output file AND this RTMP endpoint. Single encode pass, zero
    /// extra CPU. Stream failures are non-fatal (recording continues).
    pub stream_url: Option<String>,
}

type OpenedVideoEncoder = (
    ffmpeg::encoder::video::Encoder,
    ScalingContext,
    usize,
    Rational,
    Rational,
    Option<HardwareUpload>,
);

/// Video encoder that writes RGBA frames to an MP4 file.
///
/// # Example
///
/// ```rust,no_run
/// use reco_io::ffmpeg::encoder::{VideoEncoder, EncoderConfig};
/// use std::path::Path;
/// use ffmpeg_next::Rational;
///
/// let config = EncoderConfig::default();
/// let mut enc = VideoEncoder::new(Path::new("out.mp4"), 1920, 1080, Rational(30, 1), &config).unwrap();
/// // enc.write_frame(&rgba_data).unwrap();
/// enc.finish().unwrap();
/// ```
pub struct VideoEncoder {
    octx: format::context::Output,
    encoder: ffmpeg::encoder::video::Encoder,
    scaler: ScalingContext,
    stream_index: usize,
    encoder_time_base: Rational,
    output_time_base: Rational,
    frame_count: i64,
    realtime: bool,
    first_frame_time: Option<std::time::Instant>,
    last_pts: i64,
    width: u32,
    height: u32,
    finished: bool,
    encoder_name: String,
    /// Reusable frame buffers to avoid per-frame allocation.
    rgba_frame: VideoFrame,
    yuv_frame: VideoFrame,
    hardware_upload: Option<HardwareUpload>,
    /// Audio passthrough state (if enabled).
    audio: Option<AudioPassthrough>,
    /// Secondary output context for RTMP streaming (same encoded packets).
    stream: Option<StreamOutput>,
}

/// State for a secondary RTMP stream output that receives cloned packets
/// from the primary encode pass.
struct StreamOutput {
    // The context closes its AVIO before the callback's boxed state is freed.
    octx: format::context::Output,
    deadline: Box<StreamDeadline>,
    video_index: usize,
    video_time_base: Rational,
    audio: Option<SilentAudio>,
}

const STREAM_STARTUP_TIMEOUT: Duration = Duration::from_secs(2);
const STREAM_IO_TIMEOUT: Duration = Duration::from_millis(250);

struct StreamDeadline {
    origin: Instant,
    deadline_ns: AtomicU64,
    aborted: AtomicBool,
}

impl StreamDeadline {
    fn new() -> Self {
        Self {
            origin: Instant::now(),
            deadline_ns: AtomicU64::new(STREAM_STARTUP_TIMEOUT.as_nanos() as u64),
            aborted: AtomicBool::new(false),
        }
    }

    fn arm(&self, timeout: Duration) {
        let deadline = self
            .origin
            .elapsed()
            .as_nanos()
            .saturating_add(timeout.as_nanos());
        self.deadline_ns
            .store(deadline.min(u64::MAX as u128) as u64, Ordering::Relaxed);
    }

    fn abort(&self) {
        self.aborted.store(true, Ordering::Relaxed);
    }

    fn interrupted(&self) -> bool {
        if self.aborted.load(Ordering::Relaxed) {
            return true;
        }
        if self.origin.elapsed().as_nanos() >= u128::from(self.deadline_ns.load(Ordering::Relaxed))
        {
            self.abort();
            return true;
        }
        false
    }
}

unsafe extern "C" fn stream_interrupt(opaque: *mut c_void) -> std::ffi::c_int {
    // FFmpeg retains this pointer only while StreamOutput owns the boxed state.
    let Some(deadline) = (unsafe { opaque.cast::<StreamDeadline>().as_ref() }) else {
        return 1;
    };
    i32::from(deadline.interrupted())
}

impl StreamOutput {
    fn check_io(&self) -> Result<(), ffmpeg::Error> {
        let error = unsafe {
            let pb = (*self.octx.as_ptr()).pb;
            if pb.is_null() { 0 } else { (*pb).error }
        };
        if error < 0 {
            return Err(ffmpeg::Error::from(error));
        }
        if self.deadline.interrupted() {
            return Err(ffmpeg::Error::Exit);
        }
        Ok(())
    }

    fn flush(&mut self) -> Result<(), ffmpeg::Error> {
        self.deadline.arm(STREAM_IO_TIMEOUT);
        unsafe {
            let pb = (*self.octx.as_mut_ptr()).pb;
            if !pb.is_null() {
                ffmpeg::sys::avio_flush(pb);
            }
        }
        self.check_io()
    }

    fn write_packet(
        &mut self,
        mut packet: ffmpeg::Packet,
        source_time_base: Rational,
    ) -> Result<(), EncodeError> {
        self.deadline.arm(STREAM_IO_TIMEOUT);
        packet.set_stream(self.video_index);
        packet.rescale_ts(source_time_base, self.video_time_base);
        let video_pts = packet.pts().unwrap_or(0);
        packet.write_interleaved(&mut self.octx)?;
        if let Some(audio) = &mut self.audio {
            audio.write_up_to(&mut self.octx, video_pts, self.video_time_base)?;
        }
        self.check_io()?;
        Ok(())
    }

    fn finish(&mut self) -> Result<(), ffmpeg::Error> {
        self.deadline.arm(STREAM_IO_TIMEOUT);
        self.octx.write_trailer()?;
        self.check_io()
    }
}

impl Drop for StreamOutput {
    fn drop(&mut self) {
        self.deadline.abort();
    }
}

/// Generates silent AAC frames to satisfy RTMP ingest audio requirements.
struct SilentAudio {
    encoder: ffmpeg::encoder::audio::Encoder,
    stream_index: usize,
    output_time_base: Rational,
    frame: ffmpeg::frame::Audio,
    next_pts: i64,
    samples_per_frame: usize,
    sample_rate: u32,
}

/// State for copying audio from one or more input files to the output.
///
/// For a chained recording the segments are copied back-to-back: when the
/// current segment's audio ends, the next opens and its timestamps are
/// shifted by `segment_offset` so the output audio is gapless.
struct AudioPassthrough {
    ictx: format::context::Input,
    /// Audio stream index in the current input file.
    input_stream_index: usize,
    /// Audio stream index in the output file.
    output_stream_index: usize,
    /// Current input audio stream time base (for rescaling).
    input_time_base: Rational,
    /// Output audio stream time base.
    output_time_base: Rational,
    /// Source timestamp where audio passthrough starts, in seconds. Applies to
    /// the first segment only (the start-time trim is within file 1).
    start_time_secs: f64,
    /// Whether all segments have been read.
    exhausted: bool,
    /// Remaining chained segments to copy after the current one ends.
    remaining_segments: std::collections::VecDeque<std::path::PathBuf>,
    /// Output-timebase shift added to the current segment's timestamps so
    /// audio is continuous across chained segments. Recomputed per segment
    /// from its first packet (segments may not start at timestamp 0 - AAC
    /// priming can even make the first DTS negative).
    segment_offset: i64,
    /// Highest packet end (pts + duration) written, in output ticks; the next
    /// segment is rebased so its first packet lands here.
    next_offset: i64,
    /// When set, the next packet read is the first of a freshly opened segment;
    /// its timestamp is rebased so the segment starts at this output position.
    pending_start: Option<i64>,
    /// True only while copying the first segment (start-time trim applies there).
    first_segment: bool,
}

// SAFETY: VideoEncoder is only used from a single thread. The raw pointers
// inside FFmpeg's SwsContext/Encoder are not shared across threads.
unsafe impl Send for VideoEncoder {}
// SAFETY: Same single-thread usage as VideoEncoder.
unsafe impl Send for StreamOutput {}
unsafe impl Send for SilentAudio {}

impl Drop for VideoEncoder {
    fn drop(&mut self) {
        if !self.finished {
            log::warn!(
                "VideoEncoder dropped without calling finish() — output file may be corrupt"
            );
            let _ = self.flush_and_finalize();
        }
    }
}

impl VideoEncoder {
    /// Create a new MP4 encoder.
    ///
    /// Auto-detects the best available encoder (hardware first, then software)
    /// unless `config.encoder_name` is set.
    pub fn new(
        path: &Path,
        width: u32,
        height: u32,
        fps: Rational,
        config: &EncoderConfig,
    ) -> Result<Self, EncodeError> {
        crate::init();

        let all_candidates = config.codec.candidates();

        let forced_encoder = config.encoder_name.is_some();

        // Build candidate list
        let candidates: Vec<(&str, bool, Pixel)> = if let Some(ref name) = config.encoder_name {
            // When a specific encoder is forced, look it up in all codec tables
            let candidate = all_candidates
                .iter()
                .find(|c| c.name == name.as_str())
                .or_else(|| {
                    // Also check other codec tables for cross-codec --encoder usage
                    [H264_ENCODERS, HEVC_ENCODERS, AV1_ENCODERS]
                        .iter()
                        .flat_map(|t| t.iter())
                        .find(|c| c.name == name.as_str())
                });
            let pixel_fmt = candidate.map_or(Pixel::YUV420P, |c| c.pixel_format);
            let is_hw = candidate.is_some_and(|c| c.is_hardware);
            vec![(name.as_str(), is_hw, pixel_fmt)]
        } else {
            all_candidates
                .iter()
                .filter(|c| encoder::find_by_name(c.name).is_some())
                .filter(|c| auto_candidate_allowed(c.name))
                .map(|c| (c.name, c.is_hardware, c.pixel_format))
                .collect()
        };

        if candidates.is_empty() {
            return Err(EncodeError::CodecNotFound(config.codec.label().to_string()));
        }

        // Try each candidate — create a fresh output context per attempt
        // because add_stream + write_header can leave the context in a
        // bad state if the encoder fails to open.
        let mut last_err = None;

        for (name, is_hw, pixel_fmt) in &candidates {
            let codec = match encoder::find_by_name(name) {
                Some(c) => c,
                None => continue,
            };

            // Fresh output context for each attempt. Use
            // `output_as` for non-default containers (fragmented
            // MP4 via movflags still goes through the mp4 muxer, so
            // the default path works via extension lookup; Matroska
            // needs the explicit muxer name when the extension
            // doesn't match).
            // `format::output(path)` infers muxer from extension.
            // `output_as(path, name)` forces by name. We use the
            // latter for Matroska so callers can point at `.mp4`
            // or any extension and still get MKV - consumers'
            // opt-in container choice wins over filename.
            let mut octx = match config.container {
                Container::Mp4 | Container::Mp4Fragmented => format::output(path)?,
                Container::Matroska | Container::Flv => {
                    format::output_as(path, config.container.muxer_name())?
                }
                Container::Null => unsafe {
                    // ffmpeg-next's output_as opens a file even for AVFMT_NOFILE muxers.
                    let mut context = std::ptr::null_mut();
                    let result = ffmpeg::sys::avformat_alloc_output_context2(
                        &mut context,
                        std::ptr::null_mut(),
                        c"null".as_ptr(),
                        std::ptr::null(),
                    );
                    if result < 0 {
                        return Err(ffmpeg::Error::from(result).into());
                    }
                    format::context::Output::wrap(context)
                },
            };

            match Self::try_open(
                &mut octx, codec, *pixel_fmt, *is_hw, width, height, fps, config, name,
            ) {
                Ok((enc_opened, scaler, stream_index, encoder_time_base, _, hardware_upload)) => {
                    let hw_tag = if *is_hw { " (hardware)" } else { " (software)" };
                    log::info!(
                        "Encoder: {}x{} {}{} @ {}/{} fps",
                        width,
                        height,
                        name,
                        hw_tag,
                        fps.0,
                        fps.1,
                    );
                    // Story-tell which frame-delivery path this encoder uses, so
                    // the upload cost is visible in the field.
                    if hardware_upload.is_some() {
                        log::info!(
                            "Encoder frame path: explicit VAAPI surface upload (NV12 -> GPU surface)"
                        );
                    } else if *is_hw {
                        log::info!(
                            "Encoder frame path: implicit driver upload (software NV12 -> GPU, handled by FFmpeg)"
                        );
                    } else {
                        log::info!("Encoder frame path: software encode (no GPU upload)");
                    }

                    // Set up audio passthrough before writing the header.
                    let audio = if let Some(ref audio_paths) = config.audio_source {
                        Self::setup_audio_stream(&mut octx, audio_paths, config.audio_start_time)?
                    } else {
                        None
                    };

                    // Fragmented MP4 needs `movflags` so the muxer
                    // writes an `empty_moov` up front and flushes
                    // self-contained fragments on every keyframe.
                    // A concurrent reader can then parse the file
                    // mid-write; plain MP4 would park the `moov`
                    // until `write_trailer` and break replay.
                    if config.container == Container::Mp4Fragmented {
                        let mut opts = ffmpeg::Dictionary::new();
                        opts.set("movflags", "empty_moov+frag_keyframe");
                        let _ = octx.write_header_with(opts)?;
                    } else {
                        octx.write_header()?;
                    }

                    let output_time_base = octx
                        .stream(stream_index)
                        .expect("video stream we just added")
                        .time_base();

                    // Update audio output time base after write_header
                    // (the muxer may adjust it).
                    let audio = audio.map(|mut a| {
                        if let Some(s) = octx.stream(a.output_stream_index) {
                            a.output_time_base = s.time_base();
                        }
                        a
                    });

                    let stream = if let Some(ref url) = config.stream_url {
                        match Self::open_stream_output(url, &octx, stream_index, fps) {
                            Ok(s) => {
                                log::info!("Stream output opened: {url}");
                                Some(s)
                            }
                            Err(e) => {
                                log::warn!(
                                    "Failed to open stream output ({e}), recording without stream"
                                );
                                None
                            }
                        }
                    } else {
                        None
                    };

                    return Ok(Self {
                        octx,
                        encoder: enc_opened,
                        scaler,
                        stream_index,
                        encoder_time_base,
                        output_time_base,
                        frame_count: 0,
                        realtime: false,
                        first_frame_time: None,
                        last_pts: -1,
                        width,
                        height,
                        finished: false,
                        encoder_name: name.to_string(),
                        rgba_frame: VideoFrame::new(Pixel::RGBA, width, height),
                        yuv_frame: VideoFrame::new(staging_pixel_format(*pixel_fmt), width, height),
                        hardware_upload,
                        audio,
                        stream,
                    });
                }
                Err(e) => {
                    if forced_encoder {
                        log::warn!("Encoder {name} failed to open: {e}");
                    } else {
                        log::debug!("Encoder {name} failed to open: {e}, trying next...");
                    }
                    last_err = Some(e);
                }
            }
        }

        Err(last_err.unwrap_or(EncodeError::CodecNotFound(config.codec.label().to_string())))
    }

    /// Attempt to open a specific encoder. Returns the opened encoder + scaler
    /// + stream metadata, or an error.
    #[allow(clippy::too_many_arguments)]
    fn try_open(
        octx: &mut format::context::Output,
        codec: ffmpeg::Codec,
        pixel_fmt: Pixel,
        is_hw: bool,
        width: u32,
        height: u32,
        fps: Rational,
        config: &EncoderConfig,
        name: &str,
    ) -> Result<OpenedVideoEncoder, EncodeError> {
        // The secondary FLV needs codec setup data even when the primary muxer discards packets.
        let needs_global_header = config.stream_url.is_some()
            || octx.format().flags().contains(format::Flags::GLOBAL_HEADER);

        let mut ost = octx.add_stream(codec)?;
        let stream_index = ost.index();

        let mut enc = codec::context::Context::new_with_codec(codec)
            .encoder()
            .video()?;

        enc.set_width(width);
        enc.set_height(height);
        enc.set_format(pixel_fmt);
        enc.set_colorspace(ffmpeg::color::Space::BT709);
        enc.set_color_range(ffmpeg::color::Range::MPEG);
        // These context fields are available in FFmpeg 4.4 and newer.
        unsafe {
            (*enc.as_mut_ptr()).color_primaries = ffmpeg::color::Primaries::BT709.into();
            (*enc.as_mut_ptr()).color_trc = ffmpeg::color::TransferCharacteristic::BT709.into();
        }
        enc.set_frame_rate(Some(fps));
        let encoder_time_base = Rational(fps.1, fps.0);
        enc.set_time_base(encoder_time_base);

        // Optional GOP override for callers that need short keyframe
        // intervals (replay-recording fMP4, live streaming). Applied
        // before `open_with` so libx264 / libx265 / etc. pick it up.
        if let Some(gop) = config.gop_size {
            enc.set_gop(gop);
        }

        if needs_global_header {
            enc.set_flags(codec::Flags::GLOBAL_HEADER);
        }

        if !is_hw {
            enc.set_threading(ffmpeg::threading::Config::count(0));
        }

        let hardware_upload = if pixel_fmt == VAAPI_PIXEL {
            let upload = HardwareUpload::new_vaapi(width, height)?;
            // SAFETY: `enc` is a live, not-yet-opened encoder context.
            let codec_ctx = unsafe { enc.as_mut_ptr() };
            upload.attach_to_encoder(codec_ctx)?;
            Some(upload)
        } else {
            None
        };

        // Seed the output stream with the encoder's unopened
        // parameters BEFORE `open_with` so the muxer has valid
        // codec parameters when it allocates its internal fragment
        // state (matters for the fMP4 muxer with `empty_moov`,
        // which writes the moov before any packet lands). The
        // canonical ffmpeg-next transcode example follows the same
        // pattern: set params, open, re-set params.
        ost.set_parameters(&enc);
        let mut opts = build_encoder_opts(
            name,
            config.quality_preset,
            config.quality,
            config.preset.as_deref(),
            width,
            height,
        );
        // B-frames cause negative initial PTS offsets and frame reordering
        // in the encoded output. For real-time stitching they add latency
        // with no visual benefit, so disable unconditionally.
        opts.set("bf", "0");
        let encoder = enc.open_with(opts)?;
        ost.set_parameters(&encoder);
        // Stamp both stream rates so our own outputs re-probe cleanly on
        // every consumer path (Matroska and fragmented MP4 don't derive
        // them from sample timing the way plain mov does).
        ost.set_rate(fps);
        ost.set_avg_frame_rate(fps);

        // Note: write_header is called by the caller (new()) after optional
        // audio stream setup. output_time_base is read after write_header.

        let output_time_base = Rational(0, 0); // placeholder, updated after write_header

        let staging_pixel_fmt = staging_pixel_format(pixel_fmt);
        let mut scaler = ScalingContext::get(
            Pixel::RGBA,
            width,
            height,
            staging_pixel_fmt,
            width,
            height,
            ScalingFlags::BILINEAR,
        )?;
        // Keep the CPU fallback consistent with rgba_to_nv12.wgsl.
        let color_result = unsafe {
            // Bindgen's macro integer type varies between FFmpeg header versions.
            #[allow(clippy::unnecessary_cast)]
            let coefficients = ffmpeg::sys::sws_getCoefficients(ffmpeg::sys::SWS_CS_ITU709 as i32);
            ffmpeg::sys::sws_setColorspaceDetails(
                scaler.as_mut_ptr(),
                coefficients,
                1,
                coefficients,
                0,
                0,
                1 << 16,
                1 << 16,
            )
        };
        if color_result < 0 {
            return Err(ffmpeg::Error::from(color_result).into());
        }

        Ok((
            encoder,
            scaler,
            stream_index,
            encoder_time_base,
            output_time_base,
            hardware_upload,
        ))
    }

    /// The name of the active encoder (e.g., `"h264_nvenc"`, `"libx264"`).
    pub fn encoder_name(&self) -> &str {
        &self.encoder_name
    }

    /// Pick the best encoder for a secondary stream (replay) that
    /// runs concurrently with a primary encode.
    ///
    /// NVENC has a dedicated ASIC and benefits from hardware encode
    /// even under concurrent stitch load. All other hardware encoders
    /// (VideoToolbox, AMF, QSV, VAAPI) share GPU resources with the
    /// stitch shader and benchmark slower than libx264 ultrafast
    /// when both run simultaneously.
    pub fn replay_encoder_name(primary_encoder: &str) -> Option<&'static str> {
        if primary_encoder.contains("nvenc") {
            None
        } else {
            Some("libx264")
        }
    }

    /// Set up audio passthrough by adding an audio stream to the output context.
    ///
    /// Called before `write_header`. Returns `None` if no audio stream found.
    fn setup_audio_stream(
        octx: &mut format::context::Output,
        sources: &[PathBuf],
        start_time_secs: f64,
    ) -> Result<Option<AudioPassthrough>, EncodeError> {
        let Some((source_path, rest)) = sources.split_first() else {
            return Ok(None);
        };
        let mut ictx = format::input(source_path)?;
        let start_time_secs = sanitize_audio_start_time(start_time_secs);

        let audio_stream = ictx.streams().best(ffmpeg::media::Type::Audio);

        let Some(audio_stream) = audio_stream else {
            log::info!("No audio stream found in {}", source_path.display());
            return Ok(None);
        };

        let input_stream_index = audio_stream.index();
        let input_time_base = audio_stream.time_base();
        let codec_params = audio_stream.parameters();

        // Add an audio stream to the output with copied codec parameters.
        let mut ost = octx.add_stream(ffmpeg::encoder::find(codec::Id::None))?;
        ost.set_parameters(codec_params);
        // SAFETY: set codec_tag to 0 so the muxer picks the right tag
        // for the container format. codec_par is valid for the stream's lifetime.
        unsafe {
            (*ost.parameters().as_mut_ptr()).codec_tag = 0;
        }

        let output_stream_index = ost.index();
        let output_time_base = ost.time_base();

        if start_time_secs > 0.0 {
            let seek_ts = (start_time_secs * f64::from(ffmpeg::ffi::AV_TIME_BASE)).round() as i64;
            match ictx.seek(seek_ts, ..seek_ts) {
                Ok(()) => {
                    log::info!(
                        "Audio passthrough: seeking {} to {start_time_secs:.3}s",
                        source_path.display()
                    );
                }
                Err(e) => {
                    log::warn!(
                        "Audio passthrough seek to {start_time_secs:.3}s failed for {} ({e}); \
                         falling back to packet filtering from the beginning",
                        source_path.display()
                    );
                }
            }
        }

        log::info!(
            "Audio passthrough from {} (stream {}, codec: {:?})",
            source_path.display(),
            input_stream_index,
            ictx.stream(input_stream_index)
                .map(|s| s.parameters().id())
                .unwrap_or(codec::Id::None),
        );

        Ok(Some(AudioPassthrough {
            ictx,
            input_stream_index,
            output_stream_index,
            input_time_base,
            output_time_base,
            start_time_secs,
            exhausted: false,
            remaining_segments: rest.iter().cloned().collect(),
            segment_offset: 0,
            next_offset: 0,
            pending_start: None,
            first_segment: true,
        }))
    }

    /// Open the next chained segment for audio copy, continuing the timeline.
    ///
    /// Skips segments with no audio stream. Returns `false` when none remain.
    fn advance_segment(audio: &mut AudioPassthrough) -> Result<bool, EncodeError> {
        loop {
            let Some(next) = audio.remaining_segments.pop_front() else {
                return Ok(false);
            };
            let ictx = format::input(&next)?;
            let Some(stream) = ictx.streams().best(ffmpeg::media::Type::Audio) else {
                log::warn!(
                    "Chained audio: {} has no audio stream, skipping",
                    next.display()
                );
                continue;
            };
            audio.input_stream_index = stream.index();
            audio.input_time_base = stream.time_base();
            audio.ictx = ictx;
            // Rebase from this segment's first packet (computed in the loop).
            audio.pending_start = Some(audio.next_offset);
            audio.first_segment = false;
            log::info!(
                "Chained audio: continuing from {} at +{:.2}s",
                next.display(),
                audio.next_offset as f64 * f64::from(audio.output_time_base),
            );
            return Ok(true);
        }
    }

    /// Open a secondary FLV output context for RTMP streaming.
    ///
    /// Copies codec parameters from the primary video stream and adds a
    /// silent AAC audio track (YouTube RTMP rejects video-only FLV).
    fn open_stream_output(
        url: &str,
        primary_octx: &format::context::Output,
        primary_video_index: usize,
        fps: Rational,
    ) -> Result<StreamOutput, EncodeError> {
        let url_c = CString::new(url).map_err(|_| ffmpeg::Error::InvalidData)?;
        let mut raw = std::ptr::null_mut();
        let result = unsafe {
            ffmpeg::sys::avformat_alloc_output_context2(
                &mut raw,
                std::ptr::null_mut(),
                c"flv".as_ptr(),
                url_c.as_ptr(),
            )
        };
        if result < 0 || raw.is_null() {
            if !raw.is_null() {
                unsafe {
                    ffmpeg::sys::avformat_free_context(raw);
                }
            }
            return Err(if result < 0 {
                ffmpeg::Error::from(result)
            } else {
                ffmpeg::Error::Unknown
            }
            .into());
        }
        let mut stream = StreamOutput {
            octx: unsafe { format::context::Output::wrap(raw) },
            deadline: Box::new(StreamDeadline::new()),
            video_index: 0,
            video_time_base: Rational(0, 1),
            audio: None,
        };
        unsafe {
            (*raw).interrupt_callback = ffmpeg::sys::AVIOInterruptCB {
                callback: Some(stream_interrupt),
                opaque: (&*stream.deadline as *const StreamDeadline)
                    .cast_mut()
                    .cast(),
            };
        }

        let primary_video = primary_octx
            .stream(primary_video_index)
            .ok_or_else(|| EncodeError::CodecNotFound("primary video stream missing".into()))?;
        let params = primary_video.parameters();
        let extradata_len = unsafe { (*params.as_ptr()).extradata_size };
        log::info!("Stream output: copying H.264 params (extradata={extradata_len} bytes)");
        if extradata_len == 0 {
            log::warn!("Stream output: H.264 extradata is empty; FLV header lacks SPS/PPS");
        }
        let mut ost = stream
            .octx
            .add_stream(ffmpeg::encoder::find(codec::Id::None))?;
        ost.set_parameters(params);
        unsafe {
            (*ost.parameters().as_mut_ptr()).codec_tag = 0;
        }
        stream.video_index = ost.index();
        stream.audio = Some(SilentAudio::new(&mut stream.octx)?);

        // output_as opens AVIO with no interrupt callback, so connect explicitly.
        stream.deadline.arm(STREAM_STARTUP_TIMEOUT);
        let result = unsafe {
            ffmpeg::sys::avio_open2(
                &mut (*raw).pb,
                url_c.as_ptr(),
                ffmpeg::sys::AVIO_FLAG_WRITE,
                &(*raw).interrupt_callback,
                std::ptr::null_mut(),
            )
        };
        if result < 0 {
            return Err(ffmpeg::Error::from(result).into());
        }
        stream.octx.write_header()?;
        stream.video_time_base = stream
            .octx
            .stream(stream.video_index)
            .expect("stream we just added")
            .time_base();
        if let Some(audio) = &mut stream.audio {
            audio.output_time_base = stream
                .octx
                .stream(audio.stream_index)
                .expect("audio stream we just added")
                .time_base();
            let preroll_pts = unsafe {
                ffmpeg::sys::av_rescale_q(
                    1,
                    Rational(fps.1, fps.0).into(),
                    stream.video_time_base.into(),
                )
            };
            audio.write_up_to(&mut stream.octx, preroll_pts, stream.video_time_base)?;
        }
        // Keep header and initial AAC within the same startup deadline.
        unsafe {
            ffmpeg::sys::avio_flush((*raw).pb);
        }
        stream.check_io()?;
        log::info!(
            "Stream output ready: {url} (video tb={}, fps={fps})",
            stream.video_time_base
        );
        Ok(stream)
    }

    /// Forward audio packets up to the given video duration limit.
    ///
    /// Reads audio packets from the input and writes those whose timestamp
    /// falls within `max_pts` (in the output time base). Stops when audio
    /// is exhausted or exceeds the limit.
    fn forward_audio_packets_until(&mut self, max_pts: i64) -> Result<(), EncodeError> {
        let Some(ref mut audio) = self.audio else {
            return Ok(());
        };
        if audio.exhausted {
            return Ok(());
        }
        // Start-time trim is expressed in the (fixed) output time base.
        let start_pts = seconds_to_pts(audio.start_time_secs, audio.output_time_base);

        loop {
            let mut packet = ffmpeg::Packet::empty();
            match packet.read(&mut audio.ictx) {
                Ok(()) => {
                    if packet.stream() != audio.input_stream_index {
                        continue; // skip video/subtitle packets
                    }
                    packet.set_stream(audio.output_stream_index);
                    packet.rescale_ts(audio.input_time_base, audio.output_time_base);

                    // The start-time trim applies to the first segment only.
                    if audio.first_segment && start_pts > 0 {
                        let packet_start = match (packet.pts(), packet.dts()) {
                            (Some(pts), Some(dts)) => Some(pts.min(dts)),
                            (Some(pts), None) => Some(pts),
                            (None, Some(dts)) => Some(dts),
                            (None, None) => None,
                        };
                        if packet_start.is_some_and(|ts| ts < start_pts) {
                            continue;
                        }
                        if let Some(pts) = packet.pts() {
                            packet.set_pts(Some(pts - start_pts));
                        }
                        if let Some(dts) = packet.dts() {
                            packet.set_dts(Some(dts - start_pts));
                        }
                    }

                    // First packet of a continuation segment: rebase it so the
                    // segment starts exactly where the previous one ended,
                    // regardless of the segment's own (possibly negative) start.
                    if let Some(target) = audio.pending_start {
                        let anchor = packet.dts().or_else(|| packet.pts()).unwrap_or(0);
                        audio.segment_offset = target - anchor;
                        audio.pending_start = None;
                    }

                    // Shift later segments so audio is gapless across the chain.
                    if audio.segment_offset != 0 {
                        if let Some(pts) = packet.pts() {
                            packet.set_pts(Some(pts + audio.segment_offset));
                        }
                        if let Some(dts) = packet.dts() {
                            packet.set_dts(Some(dts + audio.segment_offset));
                        }
                    }

                    // Track the running end so the next segment continues from here.
                    if let Some(pts) = packet.pts() {
                        let end = pts + packet.duration().max(0);
                        if end > audio.next_offset {
                            audio.next_offset = end;
                        }
                    }

                    // Stop if this audio packet is beyond the video duration.
                    if packet.pts().is_some_and(|pts| pts > max_pts) {
                        audio.exhausted = true;
                        break;
                    }

                    packet.write_interleaved(&mut self.octx)?;
                }
                Err(ffmpeg::Error::Eof) => {
                    // Current segment done; continue with the next chained one.
                    if !Self::advance_segment(audio)? {
                        audio.exhausted = true;
                        break;
                    }
                }
                Err(_) => break,
            }
        }
        Ok(())
    }

    /// Preserve elapsed capture time when live frames are dropped or delayed.
    pub fn use_realtime_timestamps(&mut self) {
        self.realtime = true;
    }

    fn send_current_yuv_frame(&mut self) -> Result<(), EncodeError> {
        let pts = if self.realtime {
            let start = self
                .first_frame_time
                .get_or_insert_with(std::time::Instant::now);
            seconds_to_pts(start.elapsed().as_secs_f64(), self.encoder_time_base)
                .max(self.last_pts + 1)
        } else {
            self.frame_count
        };
        self.yuv_frame.set_pts(Some(pts));
        self.yuv_frame.set_color_space(ffmpeg::color::Space::BT709);
        self.yuv_frame.set_color_range(ffmpeg::color::Range::MPEG);
        self.yuv_frame
            .set_color_primaries(ffmpeg::color::Primaries::BT709);
        self.yuv_frame
            .set_color_transfer_characteristic(ffmpeg::color::TransferCharacteristic::BT709);
        if let Some(ref mut upload) = self.hardware_upload {
            let frame = upload.upload(&self.yuv_frame)?;
            self.encoder.send_frame(frame)?;
        } else {
            self.encoder.send_frame(&self.yuv_frame)?;
        }
        self.receive_and_write_packets()?;

        self.last_pts = pts;
        self.frame_count += 1;
        Ok(())
    }

    /// Write an RGBA frame to the output file.
    ///
    /// `rgba_data` must be exactly `width * height * 4` bytes (tightly packed).
    #[cfg_attr(
        feature = "profiling",
        tracing::instrument(skip_all, name = "encode_frame")
    )]
    pub fn write_frame(&mut self, rgba_data: &[u8]) -> Result<(), EncodeError> {
        let expected = self.width as usize * self.height as usize * 4;
        if rgba_data.len() != expected {
            return Err(EncodeError::FrameSizeMismatch {
                expected,
                actual: rgba_data.len(),
            });
        }

        let stride = self.rgba_frame.stride(0);
        let row_bytes = self.width as usize * 4;
        if stride == row_bytes {
            self.rgba_frame.data_mut(0)[..rgba_data.len()].copy_from_slice(rgba_data);
        } else {
            for y in 0..self.height as usize {
                let src_start = y * row_bytes;
                let dst_start = y * stride;
                self.rgba_frame.data_mut(0)[dst_start..dst_start + row_bytes]
                    .copy_from_slice(&rgba_data[src_start..src_start + row_bytes]);
            }
        }

        self.scaler.run(&self.rgba_frame, &mut self.yuv_frame)?;
        self.send_current_yuv_frame()
    }

    /// Write a pre-converted NV12 frame to the output file.
    ///
    /// `nv12_data` must be exactly `width * height * 3 / 2` bytes:
    /// Y plane (`width * height`) followed by interleaved UV plane (`width * height / 2`).
    ///
    /// This bypasses the CPU swscale RGBA→NV12 conversion, which is the main
    /// performance benefit of GPU-side NV12 conversion.
    #[cfg_attr(
        feature = "profiling",
        tracing::instrument(skip_all, name = "encode_nv12_frame")
    )]
    pub fn write_nv12_frame(&mut self, nv12_data: &[u8]) -> Result<(), EncodeError> {
        let expected = self.width as usize * self.height as usize * 3 / 2;
        if nv12_data.len() != expected {
            return Err(EncodeError::FrameSizeMismatch {
                expected,
                actual: nv12_data.len(),
            });
        }

        let w = self.width as usize;
        let h = self.height as usize;
        let y_size = w * h;

        // Copy Y plane
        let y_stride = self.yuv_frame.stride(0);
        if y_stride == w {
            self.yuv_frame.data_mut(0)[..y_size].copy_from_slice(&nv12_data[..y_size]);
        } else {
            for row in 0..h {
                let src_start = row * w;
                let dst_start = row * y_stride;
                self.yuv_frame.data_mut(0)[dst_start..dst_start + w]
                    .copy_from_slice(&nv12_data[src_start..src_start + w]);
            }
        }

        // Copy UV/chroma data depending on encoder pixel format
        let uv_data = &nv12_data[y_size..];
        let chroma_h = h / 2;
        let chroma_w = w / 2;

        if self.yuv_frame.format() == Pixel::NV12 {
            // NV12: interleaved UV plane (same layout as input)
            let uv_stride = self.yuv_frame.stride(1);
            if uv_stride == w {
                self.yuv_frame.data_mut(1)[..uv_data.len()].copy_from_slice(uv_data);
            } else {
                for row in 0..chroma_h {
                    let src_start = row * w;
                    let dst_start = row * uv_stride;
                    self.yuv_frame.data_mut(1)[dst_start..dst_start + w]
                        .copy_from_slice(&uv_data[src_start..src_start + w]);
                }
            }
        } else {
            // YUV420P: de-interleave UV into separate U and V planes
            let u_stride = self.yuv_frame.stride(1);
            let v_stride = self.yuv_frame.stride(2);
            for row in 0..chroma_h {
                for col in 0..chroma_w {
                    let uv_idx = row * w + col * 2;
                    self.yuv_frame.data_mut(1)[row * u_stride + col] = uv_data[uv_idx];
                    self.yuv_frame.data_mut(2)[row * v_stride + col] = uv_data[uv_idx + 1];
                }
            }
        }

        self.send_current_yuv_frame()
    }

    /// Write a pre-converted YUV420P planar frame.
    ///
    /// Plane slices must be tightly packed (no padding between
    /// rows): Y is `width * height`, U and V are each
    /// `(width / 2) * (height / 2)`. Odd dimensions are rejected
    /// because 4:2:0 chroma subsampling requires even width/height.
    ///
    /// This path exists for the stacked-video replay encoder
    /// ([`crate::stacked_video::encoder::StackedEncoder`]), which
    /// produces YUV420P natively from its pack primitive. Feeding
    /// RGBA through [`Self::write_frame`] would trigger a
    /// YUV→RGBA→YUV roundtrip for every replay frame; skipping the
    /// scaler saves ~1-2ms per frame at 1080p and avoids colorspace
    /// drift from repeated range conversion.
    ///
    /// Adapts to the encoder's staging pixel format: writes separate
    /// U and V planes for YUV420P, or interleaves
    /// the chroma into a single UV plane for NV12 (including libx264 and hardware encoders,
    /// including the VAAPI path, whose NV12 staging frame is then
    /// uploaded to a GPU surface before encoding).
    #[cfg_attr(
        feature = "profiling",
        tracing::instrument(skip_all, name = "encode_yuv420p_frame")
    )]
    pub fn write_yuv420p_planes(
        &mut self,
        y: &[u8],
        u: &[u8],
        v: &[u8],
    ) -> Result<(), EncodeError> {
        if !self.width.is_multiple_of(2) || !self.height.is_multiple_of(2) {
            return Err(EncodeError::FrameSizeMismatch {
                expected: 0,
                actual: 0,
            });
        }

        let w = self.width as usize;
        let h = self.height as usize;
        let chroma_w = w / 2;
        let chroma_h = h / 2;
        let y_expected = w * h;
        let uv_expected = chroma_w * chroma_h;

        if y.len() != y_expected {
            return Err(EncodeError::FrameSizeMismatch {
                expected: y_expected,
                actual: y.len(),
            });
        }
        if u.len() != uv_expected || v.len() != uv_expected {
            return Err(EncodeError::FrameSizeMismatch {
                expected: uv_expected,
                actual: u.len().max(v.len()),
            });
        }

        // Y plane: identical for both YUV420P and NV12.
        let y_stride = self.yuv_frame.stride(0);
        if y_stride == w {
            self.yuv_frame.data_mut(0)[..y_expected].copy_from_slice(y);
        } else {
            for row in 0..h {
                let src_start = row * w;
                let dst_start = row * y_stride;
                self.yuv_frame.data_mut(0)[dst_start..dst_start + w]
                    .copy_from_slice(&y[src_start..src_start + w]);
            }
        }

        if self.yuv_frame.format() == Pixel::NV12 {
            // Planar callers interleave U+V for NV12 staging.
            let uv_stride = self.yuv_frame.stride(1);
            let dst = self.yuv_frame.data_mut(1);
            for row in 0..chroma_h {
                let u_start = row * chroma_w;
                let v_start = row * chroma_w;
                let dst_start = row * uv_stride;
                for col in 0..chroma_w {
                    dst[dst_start + col * 2] = u[u_start + col];
                    dst[dst_start + col * 2 + 1] = v[v_start + col];
                }
            }
        } else {
            // Planar staging keeps separate U and V planes.
            for (plane_idx, src) in [(1usize, u), (2, v)] {
                let stride = self.yuv_frame.stride(plane_idx);
                if stride == chroma_w {
                    self.yuv_frame.data_mut(plane_idx)[..uv_expected].copy_from_slice(src);
                } else {
                    for row in 0..chroma_h {
                        let src_start = row * chroma_w;
                        let dst_start = row * stride;
                        self.yuv_frame.data_mut(plane_idx)[dst_start..dst_start + chroma_w]
                            .copy_from_slice(&src[src_start..src_start + chroma_w]);
                    }
                }
            }
        }

        self.send_current_yuv_frame()
    }

    /// Flush muxer + AVIO buffers to disk without finalizing the
    /// container.
    ///
    /// Forces any fragments or packets currently buffered in ffmpeg
    /// (either the muxer's internal queue or the AVIO output layer)
    /// out to the file descriptor. A subsequent [`Self::finish`] is
    /// still required to write the final trailer.
    ///
    /// Needed for write-while-read workflows on fragmented MP4 /
    /// Matroska where a concurrent reader only sees bytes once
    /// they've actually hit disk. Call periodically (e.g. every
    /// keyframe) from the stacked-video replay path.
    ///
    /// `av_write_frame(ctx, NULL)` prompts the muxer to emit any
    /// queued packets; `avio_flush` then forces the AVIO layer to
    /// write its buffer to the OS. Both are safe to call multiple
    /// times and at any point after `write_header`.
    pub fn flush_to_disk(&mut self) -> Result<(), EncodeError> {
        // SAFETY: `octx` is a live output context (created in
        // `new`, never dropped until `Drop` runs). `avio_flush` is
        // safe on any live AVIO and doesn't alter muxer state -
        // just forces the output-layer buffer to the file
        // descriptor. We intentionally avoid
        // `av_write_frame(ctx, NULL)` because fMP4's
        // `frag_keyframe` mode treats that as "close current
        // fragment" which clashes with the subsequent
        // `write_trailer` on finish (observed as AVERROR -105).
        unsafe {
            let pb = (*self.octx.as_mut_ptr()).pb;
            if !pb.is_null() {
                ffmpeg::sys::avio_flush(pb);
                if (*pb).error < 0 {
                    return Err(ffmpeg::Error::from((*pb).error).into());
                }
            }
        }
        if let Some(stream) = &mut self.stream
            && let Err(error) = stream.flush()
        {
            log::warn!("Secondary stream flush failed ({error}), disabling stream");
            self.stream = None;
        }
        Ok(())
    }

    /// Flush the encoder and finalize the output file.
    ///
    /// Must be called after all frames have been written. Safe to call
    /// multiple times — subsequent calls are no-ops.
    pub fn finish(&mut self) -> Result<(), EncodeError> {
        if self.finished {
            return Ok(());
        }
        self.flush_and_finalize()?;
        self.finished = true;
        log::info!("Encoder finished: {} frames written", self.frame_count);
        Ok(())
    }

    fn flush_and_finalize(&mut self) -> Result<(), EncodeError> {
        self.encoder.send_eof()?;
        self.receive_and_write_packets()?;

        // Forward audio packets trimmed to the video duration.
        if self.audio.is_some() {
            let audio_tb = self.audio.as_ref().unwrap().output_time_base;
            let video_duration_pts = self.frame_count
                * i64::from(self.encoder_time_base.numerator())
                * i64::from(audio_tb.denominator())
                / (i64::from(self.encoder_time_base.denominator())
                    * i64::from(audio_tb.numerator()).max(1));
            self.forward_audio_packets_until(video_duration_pts)?;
        }

        self.octx.write_trailer()?;

        if let Some(ref mut stream) = self.stream
            && let Err(e) = stream.finish()
        {
            log::warn!("RTMP stream finalization failed: {e}");
        }
        self.stream = None;

        Ok(())
    }

    fn receive_and_write_packets(&mut self) -> Result<(), EncodeError> {
        let mut packet = ffmpeg::Packet::empty();
        while self.encoder.receive_packet(&mut packet).is_ok() {
            packet.set_stream(self.stream_index);
            packet.rescale_ts(self.encoder_time_base, self.output_time_base);
            // The fMP4 muxer refuses to finalize a fragment whose
            // last packet has no duration (raises a warning then
            // fails `write_trailer` with AVERROR(EINVAL)). libx264
            // doesn't always populate `duration` on output packets,
            // so fill in the one-frame default (1 unit in encoder
            // time base, rescaled to output time base) when it's
            // missing. Safe for the non-fragmented muxers too,
            // which happily accept explicit durations.
            if packet.duration() <= 0 {
                // SAFETY: `av_rescale_q` is a pure arithmetic helper
                // (a / b * c rounded). No FFI state, no pointers,
                // no lifetime concerns.
                let one_frame = unsafe {
                    ffmpeg::sys::av_rescale_q(
                        1,
                        self.encoder_time_base.into(),
                        self.output_time_base.into(),
                    )
                };
                packet.set_duration(one_frame.max(1));
            }

            // Primary write unrefs the packet; retain only a cheap secondary reference.
            let secondary = self.stream.as_ref().map(|_| packet.clone());
            packet.write_interleaved(&mut self.octx)?;
            if let Some(stream) = &mut self.stream
                && let Some(packet) = secondary
                && let Err(error) = stream.write_packet(packet, self.output_time_base)
            {
                log::warn!("Secondary stream write failed ({error}), disabling stream");
                self.stream = None;
            }
        }
        Ok(())
    }
}

fn sanitize_audio_start_time(secs: f64) -> f64 {
    if secs.is_finite() && secs > 0.0 {
        secs
    } else {
        0.0
    }
}

fn seconds_to_pts(secs: f64, time_base: Rational) -> i64 {
    let secs = sanitize_audio_start_time(secs);
    if secs == 0.0 {
        return 0;
    }

    let ts = (secs * f64::from(ffmpeg::ffi::AV_TIME_BASE)).round() as i64;
    unsafe {
        ffmpeg::sys::av_rescale_q(
            ts,
            Rational(1, ffmpeg::ffi::AV_TIME_BASE).into(),
            time_base.into(),
        )
    }
}

impl SilentAudio {
    /// Add a silent AAC audio stream to the output context and prepare the encoder.
    fn new(octx: &mut format::context::Output) -> Result<Self, EncodeError> {
        let aac = ffmpeg::encoder::find(codec::Id::AAC)
            .ok_or_else(|| EncodeError::CodecNotFound("AAC".into()))?;

        let needs_global_header = octx.format().flags().contains(format::Flags::GLOBAL_HEADER);

        let mut ost = octx.add_stream(aac)?;
        let stream_index = ost.index();

        let mut enc = codec::context::Context::new_with_codec(aac)
            .encoder()
            .audio()?;

        let sample_rate = 44100u32;
        enc.set_rate(sample_rate as i32);
        enc.set_format(ffmpeg::format::Sample::F32(
            ffmpeg::format::sample::Type::Planar,
        ));
        enc.set_channel_layout(ffmpeg::ChannelLayout::STEREO);
        enc.set_bit_rate(128_000);
        enc.set_time_base(Rational(1, sample_rate as i32));

        if needs_global_header {
            enc.set_flags(codec::Flags::GLOBAL_HEADER);
        }

        ost.set_parameters(&enc);
        let encoder = enc.open()?;
        ost.set_parameters(&encoder);

        let samples_per_frame = unsafe { (*encoder.as_ptr()).frame_size as usize };
        let samples_per_frame = if samples_per_frame == 0 {
            1024
        } else {
            samples_per_frame
        };

        let mut frame = ffmpeg::frame::Audio::new(
            ffmpeg::format::Sample::F32(ffmpeg::format::sample::Type::Planar),
            samples_per_frame,
            ffmpeg::ChannelLayout::STEREO,
        );
        frame.set_rate(sample_rate);

        Self::clear_frame(&mut frame)?;
        let output_time_base = Rational(0, 0); // updated after write_header

        Ok(Self {
            encoder,
            stream_index,
            output_time_base,
            frame,
            next_pts: 0,
            samples_per_frame,
            sample_rate,
        })
    }

    fn clear_frame(frame: &mut ffmpeg::frame::Audio) -> Result<(), EncodeError> {
        let result = unsafe { ffmpeg::sys::av_frame_make_writable(frame.as_mut_ptr()) };
        if result < 0 {
            return Err(ffmpeg::Error::from(result).into());
        }
        // Planar audio only defines linesize[0]; typed planes cover every sample.
        for plane in 0..frame.planes() {
            frame.plane_mut::<f32>(plane).fill(0.0);
        }
        Ok(())
    }

    /// Write enough silent audio frames to stay ahead of the given video PTS.
    fn write_up_to(
        &mut self,
        octx: &mut format::context::Output,
        video_pts: i64,
        video_time_base: Rational,
    ) -> Result<(), EncodeError> {
        // Convert video PTS to audio sample count.
        let video_samples = unsafe {
            ffmpeg::sys::av_rescale_q(
                video_pts,
                video_time_base.into(),
                Rational(1, self.sample_rate as i32).into(),
            )
        };

        while self.next_pts < video_samples {
            Self::clear_frame(&mut self.frame)?;
            self.frame.set_pts(Some(self.next_pts));
            self.encoder.send_frame(&self.frame)?;
            self.next_pts += self.samples_per_frame as i64;

            let mut packet = ffmpeg::Packet::empty();
            while self.encoder.receive_packet(&mut packet).is_ok() {
                packet.set_stream(self.stream_index);
                packet.rescale_ts(Rational(1, self.sample_rate as i32), self.output_time_base);
                packet.write_interleaved(octx)?;
            }
        }
        Ok(())
    }
}

/// Build encoder-specific FFmpeg options.
/// Scale a 1080p-tuned bitrate ceiling (Mbps) by output pixel count, so
/// higher resolutions get a proportionally higher cap. Clamped to
/// [0.5x, 8x] of the 1080p baseline; always >= 1 Mbps.
fn scale_bitrate_mbps(base_mbps: u32, width: u32, height: u32) -> u32 {
    let scale = (f64::from(width) * f64::from(height) / (1920.0 * 1080.0)).clamp(0.5, 8.0);
    ((f64::from(base_mbps) * scale).round() as u32).max(1)
}

fn build_encoder_opts(
    name: &str,
    quality_preset: Quality,
    quality_override: Option<u8>,
    preset_override: Option<&str>,
    width: u32,
    height: u32,
) -> ffmpeg::Dictionary<'static> {
    let mut opts = ffmpeg::Dictionary::new();

    // Bitrate ceilings below are tuned for 1080p. Scale them with pixel
    // count so higher resolutions (2K/4K) aren't capped far below the
    // quality the CQ setting actually wants. Returns an "NM" string.
    let mbps = |base: u32| format!("{}M", scale_bitrate_mbps(base, width, height));

    // When a quality override is set, derive the effective preset from
    // the quality value so NVENC preset/maxrate scale with the CQ.
    let effective_preset = match quality_override {
        Some(q) if q >= 75 => Quality::High,
        Some(q) if q >= 40 => Quality::Balanced,
        Some(_) => Quality::Fast,
        None => quality_preset,
    };

    match name {
        "h264_nvenc" => {
            // NVENC VBR with per-quality bitrate ceiling. Prior defaults
            // (10M / 15M across all quality presets) artificially capped
            // High output at ~15 Mbps even with cq=19. Scale the ceilings
            // with quality so "high" actually delivers the visual bump.
            let (preset, cq, bv, maxrate) = match effective_preset {
                Quality::Fast => ("p3", "28", 8, 12),
                Quality::Balanced => ("p4", "23", 12, 18),
                Quality::High => ("p5", "19", 20, 30),
            };
            opts.set("preset", preset);
            opts.set("tune", "hq");
            opts.set("rc", "vbr");
            opts.set("cq", cq);
            opts.set("b:v", &mbps(bv));
            opts.set("maxrate", &mbps(maxrate));
            opts.set("profile", "high");
            opts.set("spatial-aq", "1");
            opts.set("temporal-aq", "1");
        }
        "hevc_nvenc" => {
            // HEVC ~30% more efficient than H264, so ceilings scale down.
            let (preset, cq, bv, maxrate) = match effective_preset {
                Quality::Fast => ("p3", "28", 6, 10),
                Quality::Balanced => ("p4", "23", 9, 14),
                Quality::High => ("p5", "19", 15, 22),
            };
            opts.set("preset", preset);
            opts.set("tune", "hq");
            opts.set("rc", "vbr");
            opts.set("cq", cq);
            opts.set("b:v", &mbps(bv));
            opts.set("maxrate", &mbps(maxrate));
            opts.set("profile", "main");
            opts.set("spatial-aq", "1");
            opts.set("temporal-aq", "1");
        }
        "av1_nvenc" => {
            // AV1 another ~20% tighter than HEVC.
            let (preset, cq, bv, maxrate) = match effective_preset {
                Quality::Fast => ("p3", "32", 5, 8),
                Quality::Balanced => ("p4", "27", 7, 11),
                Quality::High => ("p5", "22", 12, 18),
            };
            opts.set("preset", preset);
            opts.set("tune", "hq");
            opts.set("rc", "vbr");
            opts.set("cq", cq);
            opts.set("b:v", &mbps(bv));
            opts.set("maxrate", &mbps(maxrate));
        }
        "h264_qsv" => {
            let gq = match effective_preset {
                Quality::Fast => "28",
                Quality::Balanced => "23",
                Quality::High => "19",
            };
            opts.set("preset", "medium");
            opts.set("global_quality", gq);
            opts.set("profile", "high");
        }
        "h264_videotoolbox" => {
            // VideoToolbox quality maps to kVTCompressionPropertyKey_Quality
            // (0.0-1.0 float, FFmpeg divides global_quality by 100). The
            // relationship between VT quality and bitrate is exponential:
            // 0.80 -> ~104 Mbps, 1.0 -> ~297 Mbps (near-lossless). To keep
            // bitrates comparable with NVENC / software CRF at the same
            // --quality-value, we cap values at 75 (Apple's "high" tier)
            // and enforce a maxrate ceiling via kVTCompressionPropertyKey_DataRateLimits.
            let (q, maxrate) = match effective_preset {
                Quality::Fast => ("50", "12000000"),
                Quality::Balanced => ("60", "18000000"),
                Quality::High => ("70", "30000000"),
            };
            opts.set("global_quality", q);
            opts.set("maxrate", maxrate);
            opts.set("profile", "high");
        }
        "hevc_videotoolbox" => {
            // Same VT quality model as H.264; HEVC is ~30% more efficient
            // so we use slightly lower ceilings.
            let (q, maxrate) = match effective_preset {
                Quality::Fast => ("50", "10000000"),
                Quality::Balanced => ("60", "14000000"),
                Quality::High => ("70", "22000000"),
            };
            opts.set("global_quality", q);
            opts.set("maxrate", maxrate);
            opts.set("profile", "main");
        }
        "h264_vaapi" | "hevc_vaapi" | "av1_vaapi" => {
            let qp = match effective_preset {
                Quality::Fast => "28",
                Quality::Balanced => "23",
                Quality::High => "19",
            };
            opts.set("qp", qp);
            if name == "h264_vaapi" {
                opts.set("profile", "high");
            }
        }
        "h264_v4l2m2m" | "hevc_v4l2m2m" => {
            let qp = match effective_preset {
                Quality::Fast => "28",
                Quality::Balanced => "23",
                Quality::High => "19",
            };
            opts.set("qp", qp);
            // Don't set profile - v4l2m2m drivers pick appropriate defaults.
        }
        "h264_amf" | "hevc_amf" | "av1_amf" => {
            let q = match effective_preset {
                Quality::Fast => "22",
                Quality::Balanced => "18",
                Quality::High => "14",
            };
            opts.set("quality", "balanced");
            opts.set("qp_i", q);
            opts.set("qp_p", q);
            opts.set("rc", "cqp");
        }
        "libx265" => {
            let (preset, crf_val) = match effective_preset {
                Quality::Fast => ("veryfast", "28"),
                Quality::Balanced => ("fast", "25"),
                Quality::High => ("medium", "21"),
            };
            opts.set("preset", preset);
            opts.set("crf", crf_val);
            opts.set("profile", "main");
        }
        "libsvtav1" => {
            let (preset, crf_val) = match effective_preset {
                Quality::Fast => ("10", "35"),
                Quality::Balanced => ("7", "30"),
                Quality::High => ("4", "25"),
            };
            opts.set("preset", preset);
            opts.set("crf", crf_val);
        }
        "libaom-av1" => {
            let (crf_val, cpu_used) = match effective_preset {
                Quality::Fast => ("35", "8"),
                Quality::Balanced => ("30", "6"),
                Quality::High => ("25", "4"),
            };
            opts.set("crf", crf_val);
            opts.set("cpu-used", cpu_used);
            opts.set("row-mt", "1");
        }
        "libx264" => {
            let (preset, crf_val) = match effective_preset {
                Quality::Fast => ("ultrafast", "32"),
                Quality::Balanced => ("veryfast", "28"),
                Quality::High => ("fast", "23"),
            };
            opts.set("preset", preset);
            opts.set("crf", crf_val);
            opts.set("profile", "high");
            // On Tegra (Jetson), limit threads to avoid starving other
            // pipeline stages (capture + pairing threads need CPU too).
            // x264 defaults to num_cpus which over-subscribes.
            if std::path::Path::new("/etc/nv_tegra_release").exists()
                || std::fs::read_to_string("/proc/device-tree/compatible")
                    .unwrap_or_default()
                    .contains("nvidia,tegra")
            {
                log::info!("Tegra platform detected, limiting libx264 to 4 threads");
                opts.set("threads", "4");
            }
        }
        _ => {
            log::info!("Encoder '{name}' has no quality presets configured, using FFmpeg defaults");
        }
    }

    // Apply normalized quality override (0-100, higher = better).
    // Converts from the consumer-facing scale to encoder-specific parameters.
    if let Some(q) = quality_override {
        let is_videotoolbox = name.contains("videotoolbox");
        if is_videotoolbox {
            // VideoToolbox quality is exponential: VT 0.80 -> ~104 Mbps,
            // VT 1.0 -> ~297 Mbps (near-lossless). To produce bitrates
            // comparable with NVENC at the same --quality-value, compress
            // the consumer 0-100 range into VT 40-75 and enforce a maxrate
            // ceiling via DataRateLimits. VT 0.75 is Apple's "high" tier;
            // anything above produces diminishing-returns bitrate inflation.
            let vt_q = 40.0 + (q as f64 / 100.0) * 35.0;
            let val = format!("{:.0}", vt_q);
            opts.set("global_quality", &val);
            // Tiered maxrate matches NVENC ceilings (in bits/sec).
            let maxrate = if q >= 75 {
                "30000000"
            } else if q >= 40 {
                "18000000"
            } else {
                "12000000"
            };
            opts.set("maxrate", maxrate);
            log::info!(
                "Encoder quality: {q} -> {name} global_quality={val} (VT {:.2}), maxrate={maxrate}",
                vt_q / 100.0
            );
        } else {
            // CRF-style encoders: crf = 40.0 - (quality / 100.0) * 28.0
            let crf = 40.0 - (q as f64 / 100.0) * 28.0;
            let crf_str = format!("{crf:.2}");
            // Detect which quality key the encoder uses and replace it.
            let quality_keys: &[&str] = &["crf", "cq", "global_quality", "qp", "qp_i"];
            let mut found = false;
            for &key in quality_keys {
                if opts.get(key).is_some() {
                    opts.set(key, &crf_str);
                    // Also override qp_p when qp_i is present (AMF uses paired keys).
                    if key == "qp_i" && opts.get("qp_p").is_some() {
                        opts.set("qp_p", &crf_str);
                    }
                    log::info!("Encoder quality: {q} -> {name} {key}={crf_str}");
                    found = true;
                    break;
                }
            }
            if !found {
                // Fallback: set "crf" for unknown encoders.
                opts.set("crf", &crf_str);
                log::info!("Encoder quality: {q} -> {name} crf={crf_str}");
            }
        }
    }

    // Apply preset override.
    if let Some(preset) = preset_override {
        opts.set("preset", preset);
    }

    opts
}

#[cfg(test)]
mod tests {
    use super::*;

    struct StalledTcpSink {
        address: std::net::SocketAddr,
        stop: std::sync::mpsc::Sender<()>,
        thread: Option<std::thread::JoinHandle<()>>,
    }

    impl StalledTcpSink {
        fn new() -> Self {
            let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            listener.set_nonblocking(true).unwrap();
            let address = listener.local_addr().unwrap();
            let (stop, receiver) = std::sync::mpsc::channel();
            let thread = std::thread::spawn(move || {
                let end = Instant::now() + Duration::from_secs(15);
                loop {
                    match listener.accept() {
                        Ok((socket, _)) => {
                            // A watchdog closes the peer even if the writer regresses and hangs.
                            let _ = receiver
                                .recv_timeout(end.saturating_duration_since(Instant::now()));
                            let _ = socket.shutdown(std::net::Shutdown::Both);
                            break;
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            if receiver.try_recv().is_ok() || Instant::now() >= end {
                                break;
                            }
                            std::thread::sleep(Duration::from_millis(5));
                        }
                        Err(_) => break,
                    }
                }
            });
            Self {
                address,
                stop,
                thread: Some(thread),
            }
        }

        fn close(&mut self) {
            let _ = self.stop.send(());
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    impl Drop for StalledTcpSink {
        fn drop(&mut self) {
            self.close();
        }
    }

    fn stream_test_encoder(path: &Path, url: String, width: u32, height: u32) -> VideoEncoder {
        VideoEncoder::new(
            path,
            width,
            height,
            Rational(30, 1),
            &EncoderConfig {
                encoder_name: Some("libx264".into()),
                preset: Some("ultrafast".into()),
                container: Container::Matroska,
                stream_url: Some(url),
                ..Default::default()
            },
        )
        .unwrap()
    }

    fn video_packet_count(path: &Path) -> usize {
        let mut input = format::input(path).unwrap();
        let index = input
            .streams()
            .best(ffmpeg::media::Type::Video)
            .unwrap()
            .index();
        input
            .packets()
            .filter(|(stream, _)| stream.index() == index)
            .count()
    }

    #[test]
    fn secondary_deadline_interrupt_is_sticky_and_accepts_null_safely() {
        let deadline = StreamDeadline::new();
        let opaque = (&deadline as *const StreamDeadline).cast_mut().cast();
        assert_eq!(unsafe { stream_interrupt(std::ptr::null_mut()) }, 1);
        assert_eq!(unsafe { stream_interrupt(opaque) }, 0);
        deadline.arm(Duration::ZERO);
        assert_eq!(unsafe { stream_interrupt(opaque) }, 1);
        deadline.arm(STREAM_STARTUP_TIMEOUT);
        assert_eq!(unsafe { stream_interrupt(opaque) }, 1);
    }

    #[test]
    fn secondary_rtmp_startup_timeout_preserves_primary_recording() {
        let sink = StalledTcpSink::new();
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("no-rtmp-handshake.mkv");
        let started = Instant::now();
        let mut encoder =
            stream_test_encoder(&path, format!("rtmp://{}/live/test", sink.address), 64, 64);
        assert!(
            started.elapsed() >= Duration::from_secs(1),
            "test must reach the stalled RTMP handshake"
        );
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "startup took {:?}",
            started.elapsed()
        );
        assert!(encoder.stream.is_none());
        for _ in 0..3 {
            encoder
                .write_nv12_frame(&vec![128; 64 * 64 * 3 / 2])
                .unwrap();
        }
        encoder.finish().unwrap();
        assert_eq!(video_packet_count(&path), 3);
    }

    #[test]
    fn secondary_tcp_write_stall_disables_stream_and_preserves_all_primary_frames() {
        let sink = StalledTcpSink::new();
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("stalled-secondary.mkv");
        let mut encoder = stream_test_encoder(&path, format!("tcp://{}", sink.address), 1280, 720);
        assert!(encoder.stream.is_some());
        let mut pixels = vec![0; 1280 * 720 * 3 / 2];
        let mut random = 0x12345678_u32;
        let mut longest_write = Duration::ZERO;
        let write_start = Instant::now();
        let mut stream_failed_at = None;
        for _ in 0..40 {
            for pixel in &mut pixels {
                random ^= random << 13;
                random ^= random >> 17;
                random ^= random << 5;
                *pixel = random as u8;
            }
            let started = Instant::now();
            encoder.write_nv12_frame(&pixels).unwrap();
            longest_write = longest_write.max(started.elapsed());
            if encoder.stream.is_none() && stream_failed_at.is_none() {
                stream_failed_at = Some(write_start.elapsed());
            }
        }
        assert!(
            encoder.stream.is_none(),
            "test must fill the stalled peer's socket buffers"
        );
        assert!(
            stream_failed_at.unwrap() < Duration::from_secs(5),
            "deadline must interrupt before the socket watchdog"
        );
        assert!(
            longest_write < Duration::from_secs(3),
            "one write took {longest_write:?}"
        );
        let started = Instant::now();
        encoder.flush_to_disk().unwrap();
        encoder.finish().unwrap();
        assert!(started.elapsed() < Duration::from_secs(3));
        assert_eq!(video_packet_count(&path), 40);
    }

    #[test]
    fn secondary_disconnect_flush_and_drop_leave_primary_valid() {
        let mut sink = StalledTcpSink::new();
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("disconnected-secondary.mkv");
        let mut encoder = stream_test_encoder(&path, format!("tcp://{}", sink.address), 64, 64);
        assert!(encoder.stream.is_some());
        encoder
            .write_nv12_frame(&vec![128; 64 * 64 * 3 / 2])
            .unwrap();
        sink.close();
        let stream = encoder.stream.as_mut().unwrap();
        stream.deadline.arm(STREAM_IO_TIMEOUT);
        let payload = vec![0; 1024 * 1024];
        let started = Instant::now();
        unsafe {
            let pb = (*stream.octx.as_mut_ptr()).pb;
            ffmpeg::sys::avio_write(pb, payload.as_ptr(), payload.len() as i32);
        }
        encoder.flush_to_disk().unwrap();
        assert!(encoder.stream.is_none());
        assert!(
            started.elapsed() < Duration::from_secs(3),
            "flush/drop took {:?}",
            started.elapsed()
        );
        encoder
            .write_nv12_frame(&vec![128; 64 * 64 * 3 / 2])
            .unwrap();
        encoder.finish().unwrap();
        assert_eq!(video_packet_count(&path), 2);
    }

    #[test]
    fn null_muxer_encodes_without_creating_or_truncating_a_file() {
        let temporary = tempfile::tempdir().unwrap();
        for existing in [false, true] {
            let path = temporary
                .path()
                .join(if existing { "keep.mkv" } else { "absent.mkv" });
            if existing {
                std::fs::write(&path, b"preserve existing recording").unwrap();
            }
            let mut encoder = VideoEncoder::new(
                &path,
                64,
                64,
                Rational(30, 1),
                &EncoderConfig {
                    encoder_name: Some("libx264".into()),
                    preset: Some("ultrafast".into()),
                    container: Container::Null,
                    ..Default::default()
                },
            )
            .unwrap();
            for _ in 0..3 {
                encoder
                    .write_nv12_frame(&vec![128; 64 * 64 * 3 / 2])
                    .unwrap();
            }
            encoder.finish().unwrap();
            if existing {
                assert_eq!(
                    std::fs::read(&path).unwrap(),
                    b"preserve existing recording"
                );
            } else {
                assert!(!path.exists());
            }
        }
    }

    #[test]
    fn null_primary_secondary_flv_has_decodable_video_and_audio() {
        let temporary = tempfile::tempdir().unwrap();
        let absent = temporary.path().join("must-not-exist.mkv");
        let flv = temporary.path().join("secondary.flv");
        let mut encoder = VideoEncoder::new(
            &absent,
            64,
            64,
            Rational(30, 1),
            &EncoderConfig {
                encoder_name: Some("libx264".into()),
                preset: Some("ultrafast".into()),
                container: Container::Null,
                stream_url: Some(flv.to_str().unwrap().into()),
                ..Default::default()
            },
        )
        .unwrap();
        let secondary = encoder.stream.as_ref().expect("secondary must open");
        let parameters = secondary
            .octx
            .stream(secondary.video_index)
            .unwrap()
            .parameters();
        assert!(unsafe { (*parameters.as_ptr()).extradata_size } > 0);
        assert!(secondary.audio.is_some());
        for _ in 0..12 {
            encoder
                .write_nv12_frame(&vec![128; 64 * 64 * 3 / 2])
                .unwrap();
        }
        assert!(encoder.stream.is_some(), "secondary must remain healthy");
        encoder.finish().unwrap();
        drop(encoder);
        assert!(!absent.exists());

        let mut input = format::input(&flv).unwrap();
        let video = input.streams().best(ffmpeg::media::Type::Video).unwrap();
        let video_index = video.index();
        assert_eq!(video.parameters().id(), codec::Id::H264);
        let mut video_decoder = codec::context::Context::from_parameters(video.parameters())
            .unwrap()
            .decoder()
            .video()
            .unwrap();
        let audio = input.streams().best(ffmpeg::media::Type::Audio).unwrap();
        let audio_index = audio.index();
        assert_eq!(audio.parameters().id(), codec::Id::AAC);
        let mut audio_decoder = codec::context::Context::from_parameters(audio.parameters())
            .unwrap()
            .decoder()
            .audio()
            .unwrap();
        let mut video_frame = VideoFrame::empty();
        let mut audio_frame = ffmpeg::frame::Audio::empty();
        let mut video_frames = 0;
        let mut audio_frames = 0;
        for (stream, packet) in input.packets() {
            if stream.index() == video_index {
                video_decoder.send_packet(&packet).unwrap();
                while video_decoder.receive_frame(&mut video_frame).is_ok() {
                    video_frames += 1;
                }
            } else if stream.index() == audio_index {
                audio_decoder.send_packet(&packet).unwrap();
                while audio_decoder.receive_frame(&mut audio_frame).is_ok() {
                    audio_frames += 1;
                }
            }
        }
        video_decoder.send_eof().unwrap();
        while video_decoder.receive_frame(&mut video_frame).is_ok() {
            video_frames += 1;
        }
        audio_decoder.send_eof().unwrap();
        while audio_decoder.receive_frame(&mut audio_frame).is_ok() {
            audio_frames += 1;
        }
        assert_eq!(video_frames, 12);
        assert!(audio_frames > 0);
    }

    #[test]
    fn high_quality_keeps_crf_when_ultrafast_is_explicit() {
        let opts = build_encoder_opts(
            "libx264",
            Quality::High,
            None,
            Some("ultrafast"),
            1920,
            1080,
        );
        assert_eq!(opts.get("preset"), Some("ultrafast"));
        assert_eq!(opts.get("crf"), Some("23"));
    }

    #[test]
    fn encoded_nv12_has_bt709_limited_color_metadata() {
        let temporary = tempfile::tempdir().unwrap();
        let path = temporary.path().join("color.mkv");
        let mut encoder = VideoEncoder::new(
            &path,
            64,
            64,
            Rational(30, 1),
            &EncoderConfig {
                encoder_name: Some("libx264".into()),
                container: Container::Matroska,
                quality_preset: Quality::High,
                preset: Some("ultrafast".into()),
                ..Default::default()
            },
        )
        .unwrap();
        let mut nv12 = vec![128; 64 * 64 * 3 / 2];
        nv12[..64 * 64].fill(16);
        encoder.write_nv12_frame(&nv12).unwrap();
        encoder.finish().unwrap();

        let mut input = format::input(&path).unwrap();
        let stream = input.streams().best(ffmpeg::media::Type::Video).unwrap();
        let index = stream.index();
        let mut decoder = codec::context::Context::from_parameters(stream.parameters())
            .unwrap()
            .decoder()
            .video()
            .unwrap();
        for (stream, packet) in input.packets() {
            if stream.index() == index {
                decoder.send_packet(&packet).unwrap();
            }
        }
        decoder.send_eof().unwrap();
        let mut frame = ffmpeg::frame::Video::empty();
        decoder.receive_frame(&mut frame).unwrap();
        assert_eq!(frame.color_space(), ffmpeg::color::Space::BT709);
        assert_eq!(frame.color_range(), ffmpeg::color::Range::MPEG);
        assert_eq!(frame.color_primaries(), ffmpeg::color::Primaries::BT709);
        assert_eq!(
            frame.color_transfer_characteristic(),
            ffmpeg::color::TransferCharacteristic::BT709
        );
    }

    #[test]
    fn libx264_nv12_and_planar_inputs_roundtrip_chroma_and_frame_order() {
        let temporary = tempfile::tempdir().unwrap();
        let colors = [[63_u8, 102, 240], [173, 42, 26], [32, 240, 118]];
        for planar in [false, true] {
            let path = temporary.path().join(format!("chroma-{planar}.mkv"));
            let mut encoder = VideoEncoder::new(
                &path,
                68,
                32,
                Rational(30, 1),
                &EncoderConfig {
                    encoder_name: Some("libx264".into()),
                    container: Container::Matroska,
                    quality_preset: Quality::High,
                    preset: Some("ultrafast".into()),
                    ..Default::default()
                },
            )
            .unwrap();
            assert_eq!(encoder.yuv_frame.format(), Pixel::NV12);
            for [y, u, v] in colors {
                let y = vec![y; 68 * 32];
                let u = vec![u; 34 * 16];
                let v = vec![v; 34 * 16];
                if planar {
                    encoder.write_yuv420p_planes(&y, &u, &v).unwrap();
                } else {
                    let mut nv12 = y;
                    nv12.extend(u.iter().zip(&v).flat_map(|(u, v)| [*u, *v]));
                    encoder.write_nv12_frame(&nv12).unwrap();
                }
            }
            encoder.finish().unwrap();
            let mut input = format::input(&path).unwrap();
            let stream = input.streams().best(ffmpeg::media::Type::Video).unwrap();
            let index = stream.index();
            let mut decoder = codec::context::Context::from_parameters(stream.parameters())
                .unwrap()
                .decoder()
                .video()
                .unwrap();
            let mut decoded = Vec::new();
            let mut drain = |decoder: &mut ffmpeg::decoder::Video| {
                let mut frame = ffmpeg::frame::Video::empty();
                while decoder.receive_frame(&mut frame).is_ok() {
                    decoded.push([frame.data(0)[0], frame.data(1)[0], frame.data(2)[0]]);
                }
            };
            for (stream, packet) in input.packets() {
                if stream.index() == index {
                    decoder.send_packet(&packet).unwrap();
                    drain(&mut decoder);
                }
            }
            decoder.send_eof().unwrap();
            drain(&mut decoder);
            assert_eq!(decoded.len(), colors.len());
            for (actual, expected) in decoded.iter().zip(colors) {
                for channel in 0..3 {
                    assert!(
                        actual[channel].abs_diff(expected[channel]) <= 4,
                        "{actual:?} != {expected:?}"
                    );
                }
            }
        }
    }

    #[test]
    fn rgba_fallback_uses_bt709_limited_conversion() {
        let temporary = tempfile::tempdir().unwrap();
        let mut encoder = VideoEncoder::new(
            &temporary.path().join("red.mkv"),
            64,
            64,
            Rational(30, 1),
            &EncoderConfig {
                encoder_name: Some("libx264".into()),
                container: Container::Matroska,
                ..Default::default()
            },
        )
        .unwrap();
        encoder
            .write_frame(&[255, 0, 0, 255].repeat(64 * 64))
            .unwrap();
        // BT.709 limited red is Y=63, Cb=102, Cr=240 (within rounding).
        assert_eq!(encoder.yuv_frame.format(), Pixel::NV12);
        assert!(encoder.yuv_frame.data(0)[0].abs_diff(63) <= 1);
        assert!(encoder.yuv_frame.data(1)[0].abs_diff(102) <= 1);
        assert!(encoder.yuv_frame.data(1)[1].abs_diff(240) <= 1);
        encoder.finish().unwrap();
    }

    #[test]
    fn live_timing_preserves_a_capture_stall_but_file_encoding_does_not() {
        let temporary = tempfile::tempdir().unwrap();
        for realtime in [false, true] {
            let mut encoder = VideoEncoder::new(
                &temporary.path().join(format!("timing-{realtime}.mkv")),
                64,
                64,
                Rational(30, 1),
                &EncoderConfig {
                    encoder_name: Some("libx264".into()),
                    container: Container::Matroska,
                    ..Default::default()
                },
            )
            .unwrap();
            if realtime {
                encoder.use_realtime_timestamps();
            }
            let pixels = vec![0; 64 * 64 * 4];
            encoder.write_frame(&pixels).unwrap();
            encoder.first_frame_time =
                Some(std::time::Instant::now() - std::time::Duration::from_secs(2));
            encoder.write_frame(&pixels).unwrap();
            assert_eq!(encoder.frame_count, 2);
            if realtime {
                assert!(encoder.last_pts >= 60);
            } else {
                assert_eq!(encoder.last_pts, 1);
            }
            encoder.finish().unwrap();
        }
    }

    #[test]
    fn silent_audio_encodes_zeroed_samples() {
        fn assert_silent(frame: &ffmpeg::frame::Audio) {
            assert_eq!(frame.planes(), 2);
            for plane in 0..2 {
                let samples = frame.plane::<f32>(plane);
                assert_eq!(samples.len(), frame.samples());
                assert!(samples.iter().all(|sample| *sample == 0.0));
            }
        }

        crate::init();
        let temporary = tempfile::tempdir().unwrap();
        let mut output = format::output(&temporary.path().join("silence.flv")).unwrap();
        let mut audio = SilentAudio::new(&mut output).unwrap();
        assert_silent(&audio.frame);
        output.write_header().unwrap();
        audio.output_time_base = output.stream(audio.stream_index).unwrap().time_base();

        for reuse in 1..=4 {
            SilentAudio::clear_frame(&mut audio.frame).unwrap();
            audio.frame.plane_mut::<f32>(0).fill(f32::NAN);
            audio.frame.plane_mut::<f32>(1).fill(f32::INFINITY);
            let retained = unsafe {
                let reference = ffmpeg::sys::av_frame_clone(audio.frame.as_ptr());
                assert!(!reference.is_null());
                ffmpeg::frame::Audio::wrap(reference)
            };
            audio
                .write_up_to(
                    &mut output,
                    reuse * audio.samples_per_frame as i64,
                    Rational(1, audio.sample_rate as i32),
                )
                .unwrap();
            assert_silent(&audio.frame);
            assert!(
                retained
                    .plane::<f32>(0)
                    .iter()
                    .all(|sample| sample.is_nan())
            );
            assert!(
                retained
                    .plane::<f32>(1)
                    .iter()
                    .all(|sample| sample.is_infinite())
            );
        }
        audio
            .write_up_to(&mut output, 1000, Rational(1, 1000))
            .unwrap();
        assert_silent(&audio.frame);
        assert!(audio.next_pts >= 44_100);
        output.write_trailer().unwrap();
    }

    /// Only a positive, finite start time should shift audio; anything else
    /// (default exports, garbage values) must leave the soundtrack untouched.
    #[test]
    fn sanitize_audio_start_time_keeps_only_valid_offsets() {
        assert_eq!(sanitize_audio_start_time(12.5), 12.5);
        assert_eq!(sanitize_audio_start_time(0.0), 0.0);
        assert_eq!(sanitize_audio_start_time(-4.0), 0.0);
        assert_eq!(sanitize_audio_start_time(f64::NAN), 0.0);
        assert_eq!(sanitize_audio_start_time(f64::INFINITY), 0.0);
    }

    /// The audio rebase offset must equal the requested start time expressed
    /// in the output stream's time base, so trimmed audio lands at time zero
    /// in sync with the video. This is the core of the `--start-time` fix.
    #[test]
    fn seconds_to_pts_converts_into_the_stream_time_base() {
        // One second in a 44.1 kHz audio time base is exactly 44_100 ticks.
        assert_eq!(seconds_to_pts(1.0, Rational(1, 44_100)), 44_100);
        // Half a second in a millisecond time base is 500 ticks.
        assert_eq!(seconds_to_pts(0.5, Rational(1, 1_000)), 500);
    }

    /// A zero or invalid start time produces no shift, so exports without
    /// `--start-time` mux audio exactly as before this fix.
    #[test]
    fn seconds_to_pts_is_zero_for_no_offset() {
        assert_eq!(seconds_to_pts(0.0, Rational(1, 44_100)), 0);
        assert_eq!(seconds_to_pts(-2.0, Rational(1, 44_100)), 0);
        assert_eq!(seconds_to_pts(f64::NAN, Rational(1, 44_100)), 0);
    }

    /// Bitrate ceilings scale with output resolution vs the 1080p baseline.
    #[test]
    fn bitrate_scales_with_resolution() {
        // 1080p: unchanged baseline.
        assert_eq!(scale_bitrate_mbps(30, 1920, 1080), 30);
        // 2560x1440 (~1.78x pixels).
        assert_eq!(scale_bitrate_mbps(30, 2560, 1440), 53);
        // 4K (4x pixels).
        assert_eq!(scale_bitrate_mbps(30, 3840, 2160), 120);
        // Clamped: very high res caps at 8x.
        assert_eq!(scale_bitrate_mbps(30, 7680, 4320), 240);
        // Clamped: low res floors at 0.5x, never below 1 Mbps.
        assert_eq!(scale_bitrate_mbps(30, 640, 360), 15);
        assert_eq!(scale_bitrate_mbps(1, 320, 240), 1);
    }
}
