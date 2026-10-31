//! Timestamp-aligned replay of finalized separate-camera recordings.

use std::io::Read;
use std::path::{Path, PathBuf};

use reco_core::calibration::MatchCalibration;
use reco_core::source::{FramePair, FrameSource, SourceError, SourceInfo, StereoFrame, YuvData};
use serde::{Deserialize, Serialize};

use crate::ffmpeg::decoder::VideoDecoder;

const MAX_GAP_NS: i64 = 500_000_000;
const MUX_TOLERANCE_NS: u64 = 2_000_000;

fn invalid(reason: impl Into<String>) -> SourceError {
    SourceError::Read {
        reason: reason.into(),
    }
}

#[derive(Clone, Deserialize)]
struct Timing {
    packets: u64,
    first_pts_ns: u64,
    last_pts_ns: u64,
    first_clock_time_ns: u64,
    last_clock_time_ns: u64,
}

#[derive(Clone, Deserialize)]
struct Camera {
    file: String,
    timing: Timing,
}

#[derive(Deserialize)]
struct Capture {
    width: u32,
    height: u32,
    fps: u32,
    left_device: String,
    right_device: String,
}

#[derive(Deserialize)]
struct SavedFormat {
    width: u32,
    height: u32,
    codec: String,
}

/// Validated capture provenance; filenames are confined to the manifest directory.
#[derive(Deserialize)]
pub struct SourceBundle {
    version: u32,
    state: String,
    clock_comparable: bool,
    capture: Capture,
    source: SavedFormat,
    calibration: String,
    left: Camera,
    right: Camera,
    #[serde(skip)]
    directory: PathBuf,
}

impl SourceBundle {
    /// Load a completed bundle and verify that CLI inputs refer to its exact files.
    pub fn load(
        path: &Path,
        left: &Path,
        right: &Path,
        calibration: &Path,
    ) -> Result<Self, SourceError> {
        let mut bytes = Vec::new();
        std::fs::File::open(path)
            .map_err(|e| invalid(format!("source manifest: {e}")))?
            .take(1_048_577)
            .read_to_end(&mut bytes)
            .map_err(|e| invalid(format!("source manifest: {e}")))?;
        if bytes.len() > 1_048_576 {
            return Err(invalid("source manifest exceeds 1 MiB"));
        }
        let mut bundle: Self = serde_json::from_slice(&bytes)
            .map_err(|e| invalid(format!("invalid source manifest: {e}")))?;
        bundle.validate()?;
        bundle.directory = path
            .parent()
            .unwrap_or(Path::new("."))
            .canonicalize()
            .map_err(|e| invalid(format!("source directory: {e}")))?;
        for (actual, name) in [
            (left, "left.mkv"),
            (right, "right.mkv"),
            (calibration, "calibration.json"),
        ] {
            let expected = bundle.directory.join(name);
            let metadata = std::fs::symlink_metadata(&expected)
                .map_err(|e| invalid(format!("bundle {name}: {e}")))?;
            if !metadata.is_file()
                || metadata.file_type().is_symlink()
                || actual.canonicalize().ok().as_ref() != Some(&expected)
            {
                return Err(invalid(format!(
                    "input must be the bundle's regular {name} file"
                )));
            }
        }
        Ok(bundle)
    }

    fn validate(&self) -> Result<(), SourceError> {
        if self.version != 1
            || self.state != "complete"
            || !self.clock_comparable
            || self.left.file != "left.mkv"
            || self.right.file != "right.mkv"
            || self.calibration != "calibration.json"
            || self.source.codec != "h264"
            || self.capture.left_device.parse::<u32>().ok()
                == self.capture.right_device.parse::<u32>().ok()
            || self.capture.left_device.parse::<u32>().is_err()
            || self.capture.right_device.parse::<u32>().is_err()
            || !(1..=60).contains(&self.capture.fps)
        {
            return Err(invalid(
                "source manifest is incomplete, incompatible, or has incomparable camera clocks",
            ));
        }
        for value in [
            self.capture.width,
            self.capture.height,
            self.source.width,
            self.source.height,
        ] {
            if !(16..=8192).contains(&value) || !value.is_multiple_of(2) {
                return Err(invalid("invalid source dimensions"));
            }
        }
        if self.source.width > self.capture.width
            || self.source.height > self.capture.height
            || u64::from(self.source.width) * u64::from(self.capture.height)
                != u64::from(self.source.height) * u64::from(self.capture.width)
        {
            return Err(invalid(
                "source dimensions do not preserve the capture aspect ratio",
            ));
        }
        for camera in [&self.left, &self.right] {
            let t = &camera.timing;
            if t.packets < 2
                || t.packets > 100_000_000
                || t.last_pts_ns <= t.first_pts_ns
                || t.last_clock_time_ns <= t.first_clock_time_ns
                || t.last_clock_time_ns > i64::MAX as u64 - MUX_TOLERANCE_NS
                || (t.last_pts_ns - t.first_pts_ns)
                    .abs_diff(t.last_clock_time_ns - t.first_clock_time_ns)
                    > MUX_TOLERANCE_NS
            {
                return Err(invalid("invalid source timing anchors"));
            }
        }
        if self.end_ns() <= self.start_ns() {
            return Err(invalid("source camera timelines do not overlap"));
        }
        Ok(())
    }

    fn start_ns(&self) -> i64 {
        self.left
            .timing
            .first_clock_time_ns
            .max(self.right.timing.first_clock_time_ns) as i64
    }
    fn end_ns(&self) -> i64 {
        self.left
            .timing
            .last_clock_time_ns
            .min(self.right.timing.last_clock_time_ns) as i64
    }
    fn frames(&self) -> u64 {
        ((self.end_ns() - self.start_ns()) as u128 * self.capture.fps as u128 / 1_000_000_000)
            as u64
            + 1
    }

    /// Scale only pixel intrinsics; distortion and camera arrangement are unchanged.
    pub fn scale_calibration(&self, calibration: &mut MatchCalibration) -> Result<(), SourceError> {
        for camera in [&mut calibration.left, &mut calibration.right] {
            if (camera.width, camera.height) != (self.capture.width, self.capture.height) {
                return Err(invalid(
                    "calibration dimensions differ from the recorded capture",
                ));
            }
            let sx = self.source.width as f64 / self.capture.width as f64;
            let sy = self.source.height as f64 / self.capture.height as f64;
            camera.fx *= sx;
            camera.fy *= sy;
            camera.cx *= sx;
            camera.cy *= sy;
            camera.width = self.source.width;
            camera.height = self.source.height;
        }
        calibration.sync_offset = 0;
        Ok(())
    }
}

/// Counts from timestamp resampling, independent of hardware exposure synchronization.
#[derive(Debug, Clone, Default, Serialize)]
pub struct SourceSyncStats {
    /// Frames emitted on the common recording timeline.
    pub output_frames: u64,
    /// Output frames reusing the preceding left image across a short capture gap.
    pub left_duplicates: u64,
    /// Output frames reusing the preceding right image across a short capture gap.
    pub right_duplicates: u64,
    /// Decoded left images skipped to select the closest capture timestamp.
    pub left_skipped: u64,
    /// Decoded right images skipped to select the closest capture timestamp.
    pub right_skipped: u64,
}

struct TimedFrame {
    time_ns: i64,
    sequence: u64,
    pixels: YuvData,
}

struct DecodedFrames {
    decoder: VideoDecoder,
    timing: Timing,
    first_pts_us: Option<i64>,
    previous_pts_us: Option<i64>,
    count: u64,
    done: bool,
}

impl Iterator for DecodedFrames {
    type Item = Result<TimedFrame, SourceError>;
    fn next(&mut self) -> Option<Self::Item> {
        if self.done {
            return None;
        }
        let result = self.read();
        if !matches!(&result, Ok(Some(_))) {
            self.done = true;
        }
        result.transpose()
    }
}

impl DecodedFrames {
    fn read(&mut self) -> Result<Option<TimedFrame>, SourceError> {
        let Some(frame) = self
            .decoder
            .next_frame()
            .map_err(|e| invalid(format!("source decode failed: {e}")))?
        else {
            let duration_ns = self
                .previous_pts_us
                .zip(self.first_pts_us)
                .and_then(|(last, first)| last.checked_sub(first))
                .and_then(|v| u64::try_from(v).ok())
                .and_then(|v| v.checked_mul(1000));
            if self.count != self.timing.packets
                || duration_ns.is_none_or(|v| {
                    v.abs_diff(self.timing.last_pts_ns - self.timing.first_pts_ns)
                        > MUX_TOLERANCE_NS
                })
            {
                return Err(invalid(
                    "source file is truncated or does not match its timing manifest",
                ));
            }
            return Ok(None);
        };
        // Matroska millisecond timestamps can tie; Cursor selects the last frame in a tie.
        if self
            .previous_pts_us
            .is_some_and(|previous| frame.timestamp_us < previous)
        {
            return Err(invalid("source timestamps move backwards"));
        }
        let first = *self.first_pts_us.get_or_insert(frame.timestamp_us);
        self.previous_pts_us = Some(frame.timestamp_us);
        self.count += 1;
        let time_ns =
            timestamp_on_clock(first, frame.timestamp_us, self.timing.first_clock_time_ns)?;
        if self.count > self.timing.packets
            || time_ns > self.timing.last_clock_time_ns as i64 + MUX_TOLERANCE_NS as i64
        {
            return Err(invalid("source file timestamps differ from the manifest"));
        }
        Ok(Some(TimedFrame {
            time_ns,
            sequence: self.count,
            pixels: YuvData {
                y: frame.y,
                u: frame.u,
                v: frame.v,
            },
        }))
    }
}

fn timestamp_on_clock(
    first_pts_us: i64,
    pts_us: i64,
    first_clock_ns: u64,
) -> Result<i64, SourceError> {
    pts_us
        .checked_sub(first_pts_us)
        .and_then(|v| v.checked_mul(1000))
        .and_then(|v| i64::try_from(first_clock_ns).ok()?.checked_add(v))
        .ok_or_else(|| invalid("source timestamp overflow"))
}

struct Cursor {
    frames: Box<dyn Iterator<Item = Result<TimedFrame, SourceError>> + Send>,
    before: Option<TimedFrame>,
    after: Option<TimedFrame>,
    last_selected: Option<u64>,
    duplicates: u64,
    skipped: u64,
}

impl Cursor {
    fn new(
        frames: impl Iterator<Item = Result<TimedFrame, SourceError>> + Send + 'static,
    ) -> Result<Self, SourceError> {
        let mut frames = Box::new(frames);
        let after = frames.next().transpose()?;
        if after.is_none() {
            return Err(invalid("source contains no video frames"));
        }
        Ok(Self {
            frames,
            before: None,
            after,
            last_selected: None,
            duplicates: 0,
            skipped: 0,
        })
    }

    fn sample(&mut self, target_ns: i64) -> Result<YuvData, SourceError> {
        while self.after.as_ref().is_some_and(|f| f.time_ns <= target_ns) {
            self.before = self.after.take();
            self.after = self.frames.next().transpose()?;
        }
        if let (Some(a), Some(b)) = (&self.before, &self.after)
            && b.time_ns - a.time_ns > MAX_GAP_NS
        {
            return Err(invalid("source capture gap exceeds 0.5 seconds"));
        }
        let selected = match (&self.before, &self.after) {
            (Some(a), Some(b)) if target_ns - a.time_ns <= b.time_ns - target_ns => a,
            (_, Some(b)) => b,
            (Some(a), None) if target_ns <= a.time_ns + MUX_TOLERANCE_NS as i64 => a,
            _ => return Err(invalid("source ended before the common capture interval")),
        };
        if selected.time_ns.abs_diff(target_ns) > MAX_GAP_NS as u64 {
            return Err(invalid(
                "source has no frame near the requested capture time",
            ));
        }
        if let Some(previous) = self.last_selected {
            self.duplicates += u64::from(selected.sequence == previous);
            self.skipped += selected.sequence.saturating_sub(previous + 1);
        } else {
            self.skipped += selected.sequence - 1;
        }
        self.last_selected = Some(selected.sequence);
        Ok(selected.pixels.clone())
    }

    fn validate_end(&mut self) -> Result<(), SourceError> {
        for frame in self.frames.by_ref() {
            frame?;
        }
        Ok(())
    }
}

pub(crate) struct BundleSource {
    left: Cursor,
    right: Cursor,
    info: SourceInfo,
    start_ns: i64,
    count: u64,
    pub(crate) colors: [reco_core::render::renderer::InputColorimetry; 2],
}

impl BundleSource {
    pub(crate) fn open(bundle: SourceBundle) -> Result<Self, SourceError> {
        let open = |camera: &Camera| -> Result<
            (Cursor, reco_core::render::renderer::InputColorimetry),
            SourceError,
        > {
            let decoder = VideoDecoder::open(&bundle.directory.join(&camera.file))
                .map_err(|e| invalid(format!("source camera file: {e}")))?;
            if decoder.width() != bundle.source.width
                || decoder.height() != bundle.source.height
                || decoder.rotation() != 0
                || decoder.is_full_range()
                || (decoder.fps() - bundle.capture.fps as f64).abs() > 0.5
            {
                return Err(invalid(
                    "source video format differs from its capture manifest",
                ));
            }
            let color = decoder
                .input_colorimetry()
                .ok_or_else(|| invalid("source video has unknown or unsupported colorimetry"))?;
            let cursor = Cursor::new(DecodedFrames {
                decoder,
                timing: camera.timing.clone(),
                first_pts_us: None,
                previous_pts_us: None,
                count: 0,
                done: false,
            })?;
            Ok((cursor, color))
        };
        let (left, left_color) = open(&bundle.left)?;
        let (right, right_color) = open(&bundle.right)?;
        Ok(Self {
            left,
            right,
            colors: [left_color, right_color],
            info: SourceInfo {
                width: bundle.source.width,
                height: bundle.source.height,
                fps: bundle.capture.fps as f64,
                fps_rational: Some((bundle.capture.fps as i32, 1)),
                total_frames: Some(bundle.frames()),
            },
            start_ns: bundle.start_ns(),
            count: 0,
        })
    }

    pub(crate) fn validate_completion(&mut self) -> Result<(), SourceError> {
        if self.count == self.info.total_frames.unwrap_or(0) {
            self.left.validate_end()?;
            self.right.validate_end()?;
        }
        Ok(())
    }

    pub(crate) fn stats(&self) -> SourceSyncStats {
        SourceSyncStats {
            output_frames: self.count,
            left_duplicates: self.left.duplicates,
            right_duplicates: self.right.duplicates,
            left_skipped: self.left.skipped,
            right_skipped: self.right.skipped,
        }
    }
}

impl FrameSource for BundleSource {
    fn info(&self) -> SourceInfo {
        self.info.clone()
    }
    fn next_frame(&mut self) -> Result<Option<StereoFrame>, SourceError> {
        if self.is_exhausted() {
            self.validate_completion()?;
            return Ok(None);
        }
        let target_ns =
            self.start_ns + (self.count as u128 * 1_000_000_000 / self.info.fps as u128) as i64;
        let pair = FramePair {
            left: self.left.sample(target_ns)?,
            right: self.right.sample(target_ns)?,
        };
        self.count += 1;
        Ok(Some(StereoFrame::Yuv420p(pair)))
    }
    fn total_frames(&self) -> Option<u64> {
        self.info.total_frames
    }
    fn is_exhausted(&self) -> bool {
        self.count >= self.info.total_frames.unwrap_or(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest() -> serde_json::Value {
        let timing = serde_json::json!({"packets": 31, "first_pts_ns": 3_600_000_000_000_000u64,
            "last_pts_ns": 3_600_001_000_000_000u64, "first_clock_time_ns": 5_000_000_000u64,
            "last_clock_time_ns": 6_000_000_000u64});
        serde_json::json!({"version":1,"state":"complete","clock_comparable":true,
            "capture":{"width":4032,"height":3040,"fps":30,"left_device":"0","right_device":"1"},
            "source":{"width":2016,"height":1520,"codec":"h264"},"calibration":"calibration.json",
            "left":{"file":"left.mkv","timing":timing},"right":{"file":"right.mkv","timing":timing}})
    }

    #[test]
    fn validates_completed_bundle_and_exact_regular_input_paths() {
        let dir = tempfile::tempdir().unwrap();
        for name in ["left.mkv", "right.mkv", "calibration.json", "different.mkv"] {
            std::fs::write(dir.path().join(name), b"fixture").unwrap();
        }
        let path = dir.path().join("manifest.json");
        std::fs::write(&path, serde_json::to_vec(&manifest()).unwrap()).unwrap();
        let load = |left: &str| {
            SourceBundle::load(
                &path,
                &dir.path().join(left),
                &dir.path().join("right.mkv"),
                &dir.path().join("calibration.json"),
            )
        };
        assert!(load("left.mkv").is_ok());
        assert!(load("different.mkv").is_err());
        for (key, value) in [
            ("state", serde_json::json!("recording")),
            ("clock_comparable", serde_json::json!(false)),
        ] {
            let mut changed = manifest();
            changed[key] = value;
            std::fs::write(&path, serde_json::to_vec(&changed).unwrap()).unwrap();
            assert!(load("left.mkv").is_err());
        }
    }

    #[test]
    fn rejects_wrong_sensor_geometry_and_clock_spans() {
        for (pointer, value) in [
            ("/left/file", serde_json::json!("../left.mkv")),
            ("/capture/right_device", serde_json::json!("0")),
            ("/source/height", serde_json::json!(1080)),
            (
                "/left/timing/last_clock_time_ns",
                serde_json::json!(6_100_000_000u64),
            ),
            ("/left/timing/packets", serde_json::json!(0)),
        ] {
            let mut changed = manifest();
            *changed.pointer_mut(pointer).unwrap() = value;
            assert!(
                serde_json::from_value::<SourceBundle>(changed)
                    .unwrap()
                    .validate()
                    .is_err(),
                "{pointer}"
            );
        }
    }

    fn pixels(mark: u8) -> YuvData {
        YuvData {
            y: vec![mark; 4],
            u: vec![128],
            v: vec![128],
        }
    }

    fn cursor(first_pts_us: i64, clock_ns: u64, markers: &[u8]) -> Cursor {
        let frames: Vec<_> = markers
            .iter()
            .enumerate()
            .map(|(index, &mark)| {
                Ok(TimedFrame {
                    time_ns: timestamp_on_clock(
                        first_pts_us,
                        first_pts_us + i64::from(mark) * 100_000,
                        clock_ns,
                    )
                    .unwrap(),
                    sequence: index as u64 + 1,
                    pixels: pixels(mark),
                })
            })
            .collect();
        Cursor::new(frames.into_iter()).unwrap()
    }

    #[test]
    fn resamples_pixel_markers_after_mux_rebase_and_drops_without_permanent_drift() {
        let left = cursor(4_000_000, 5_000_000_000, &[0, 1, 2, 4, 5]);
        let right = cursor(3_600_000_000_000, 5_020_000_000, &[0, 1, 3, 4, 5]);
        let mut source = BundleSource {
            colors: Default::default(),
            left,
            right,
            info: SourceInfo {
                width: 2,
                height: 2,
                fps: 10.,
                fps_rational: Some((10, 1)),
                total_frames: Some(5),
            },
            start_ns: 5_020_000_000,
            count: 0,
        };
        let mut pairs = Vec::new();
        while let Some(StereoFrame::Yuv420p(pair)) = source.next_frame().unwrap() {
            pairs.push((pair.left.y[0], pair.right.y[0]));
        }
        assert_eq!(pairs, [(0, 0), (1, 1), (2, 1), (4, 3), (4, 4)]);
        assert_eq!(source.stats().left_duplicates, 1);
        assert_eq!(source.stats().right_duplicates, 1);
        assert_eq!(source.stats().left_skipped, 0);
        assert_eq!(source.stats().output_frames, 5);
        assert!(source.next_frame().unwrap().is_none());
    }

    #[test]
    fn counts_decoded_images_skipped_by_resampling_separately_from_capture_gaps() {
        let mut frames = cursor(1_000_000, 5_000_000_000, &[0, 1, 2, 3, 4]);
        assert_eq!(frames.sample(5_000_000_000).unwrap().y[0], 0);
        assert_eq!(frames.sample(5_200_000_000).unwrap().y[0], 2);
        assert_eq!(frames.sample(5_400_000_000).unwrap().y[0], 4);
        assert_eq!(frames.skipped, 2);
        assert_eq!(frames.duplicates, 0);
    }

    #[test]
    fn rejects_long_gaps_instead_of_hiding_missing_video() {
        let mut frames = cursor(123_456, 1_000_000_000, &[0, 1, 8, 9]);
        assert!(frames.sample(1_000_000_000).is_ok());
        assert!(frames.sample(1_100_000_000).is_err());
    }

    #[test]
    fn does_not_extend_a_camera_after_its_last_frame() {
        let mut frames = cursor(123_456, 1_000_000_000, &[0, 1, 2]);
        assert!(frames.sample(1_200_000_000).is_ok());
        assert!(frames.sample(1_300_000_000).is_err());
    }

    #[test]
    fn common_interval_excludes_nonoverlapping_camera_tails() {
        let mut data = manifest();
        data["right"]["timing"]["first_clock_time_ns"] = serde_json::json!(5_100_000_000u64);
        data["right"]["timing"]["last_clock_time_ns"] = serde_json::json!(6_100_000_000u64);
        let bundle: SourceBundle = serde_json::from_value(data).unwrap();
        bundle.validate().unwrap();
        assert_eq!(bundle.start_ns(), 5_100_000_000);
        assert_eq!(bundle.end_ns(), 6_000_000_000);
        assert_eq!(bundle.frames(), 28);
    }

    #[test]
    fn calibration_scaling_preserves_rig_and_distortion() {
        let lens = serde_json::json!({"width":4032,"height":3040,"fx":1000.0,"fy":1100.0,
            "cx":2016.0,"cy":1520.0,"d":[0.1,0.2,0.3,0.4]});
        let mut calibration: MatchCalibration = serde_json::from_value(serde_json::json!({
            "left_uniforms":lens,"right_uniforms":lens,"sync_offset":7,
            "params":{"cameraAxisOffset":0.2,"intersect":0.4,"xTy":0.01,"xRz":0.02,"zRx":0.03}
        }))
        .unwrap();
        let bundle: SourceBundle = serde_json::from_value(manifest()).unwrap();
        bundle.scale_calibration(&mut calibration).unwrap();
        assert_eq!(
            (calibration.left.width, calibration.left.height),
            (2016, 1520)
        );
        assert_eq!(
            (
                calibration.left.fx,
                calibration.left.fy,
                calibration.left.cx,
                calibration.left.cy
            ),
            (500., 550., 1008., 760.)
        );
        assert_eq!(calibration.left.d, [0.1, 0.2, 0.3, 0.4]);
        assert_eq!(calibration.layout.intersect, 0.4);
        assert_eq!(calibration.sync_offset, 0);
        assert!(bundle.scale_calibration(&mut calibration).is_err());
    }

    fn write_camera(path: &Path, start_ms: i64, marks: &[u8], packet_pts: Option<&[i64]>) {
        use ffmpeg_next as ffmpeg;
        ffmpeg::init().unwrap();
        let codec =
            ffmpeg::encoder::find_by_name("libx264").expect("test requires installed libx264");
        let mut output = ffmpeg::format::output(path).unwrap();
        let mut stream = output.add_stream(codec).unwrap();
        let mut encoder = ffmpeg::codec::context::Context::new_with_codec(codec)
            .encoder()
            .video()
            .unwrap();
        encoder.set_width(64);
        encoder.set_height(64);
        encoder.set_format(ffmpeg::format::Pixel::YUV420P);
        encoder.set_flags(ffmpeg::codec::Flags::GLOBAL_HEADER);
        encoder.set_time_base(ffmpeg::Rational(1, 1000));
        encoder.set_frame_rate(Some(ffmpeg::Rational(10, 1)));
        encoder.set_max_b_frames(0);
        unsafe {
            (*encoder.as_mut_ptr()).colorspace = ffmpeg::ffi::AVColorSpace::AVCOL_SPC_BT709;
            (*encoder.as_mut_ptr()).color_range = ffmpeg::ffi::AVColorRange::AVCOL_RANGE_MPEG;
        }
        let mut options = ffmpeg::Dictionary::new();
        options.set("preset", "ultrafast");
        options.set("tune", "zerolatency");
        options.set("crf", "0");
        let mut encoder = encoder.open_with(options).unwrap();
        stream.set_parameters(&encoder);
        stream.set_time_base(ffmpeg::Rational(1, 1000));
        stream.set_rate(ffmpeg::Rational(10, 1));
        stream.set_avg_frame_rate(ffmpeg::Rational(10, 1));
        output.write_header().unwrap();
        let stream_time_base = output.stream(0).unwrap().time_base();
        let mut packet_index = 0;
        let mut drain = |encoder: &mut ffmpeg::encoder::video::Encoder,
                         output: &mut ffmpeg::format::context::Output| {
            let mut packet = ffmpeg::Packet::empty();
            while encoder.receive_packet(&mut packet).is_ok() {
                packet.set_stream(0);
                packet.set_duration(100);
                if let Some(timestamps) = packet_pts {
                    packet.set_pts(Some(timestamps[packet_index]));
                    // Keep legal increasing DTS while exercising tied or backwards presentation times.
                    packet.set_dts(packet.dts().map(|time| time - 1000));
                }
                packet_index += 1;
                packet.rescale_ts(ffmpeg::Rational(1, 1000), stream_time_base);
                packet.write_interleaved(output).unwrap();
            }
        };
        let mut frame = ffmpeg::util::frame::Video::new(ffmpeg::format::Pixel::YUV420P, 64, 64);
        frame.data_mut(1).fill(128);
        frame.data_mut(2).fill(128);
        for &mark in marks {
            frame.data_mut(0).fill(16 + mark * 40);
            frame.set_pts(Some(start_ms + i64::from(mark) * 100));
            encoder.send_frame(&frame).unwrap();
            drain(&mut encoder, &mut output);
        }
        encoder.send_eof().unwrap();
        drain(&mut encoder, &mut output);
        output.write_trailer().unwrap();
    }

    #[test]
    fn decoded_files_use_capture_anchors_and_validate_final_packet_counts() {
        let dir = tempfile::tempdir().unwrap();
        let left = dir.path().join("left.mkv");
        let right = dir.path().join("right.mkv");
        write_camera(&left, 7000, &[0, 1, 3, 4], None);
        write_camera(&right, 9000, &[0, 1, 2, 3, 4], None);
        let mut data = manifest();
        data["capture"]["width"] = serde_json::json!(64);
        data["capture"]["height"] = serde_json::json!(64);
        data["capture"]["fps"] = serde_json::json!(10);
        data["source"]["width"] = serde_json::json!(64);
        data["source"]["height"] = serde_json::json!(64);
        for (name, count, clock) in [
            ("left", 4, 10_000_000_000u64),
            ("right", 5, 10_020_000_000u64),
        ] {
            data[name]["timing"] = serde_json::json!({"packets":count,"first_pts_ns":3_600_000_000_000_000u64,
                "last_pts_ns":3_600_000_400_000_000u64,"first_clock_time_ns":clock,"last_clock_time_ns":clock+400_000_000});
        }
        let path = dir.path().join("manifest.json");
        let calibration = dir.path().join("calibration.json");
        std::fs::write(&calibration, b"{}").unwrap();
        std::fs::write(&path, serde_json::to_vec(&data).unwrap()).unwrap();
        let mut source =
            BundleSource::open(SourceBundle::load(&path, &left, &right, &calibration).unwrap())
                .unwrap();
        let mut pixels = Vec::new();
        while let Some(StereoFrame::Yuv420p(pair)) = source.next_frame().unwrap() {
            pixels.push(((pair.left.y[0] - 16) / 40, (pair.right.y[0] - 16) / 40));
        }
        assert_eq!(pixels, [(0, 0), (1, 1), (3, 2), (3, 3)]);
        assert_eq!(source.stats().output_frames, 4);
        assert_eq!(
            source.colors[0].matrix,
            reco_core::render::renderer::YuvMatrix::Bt709
        );
        data["left"]["timing"]["packets"] = serde_json::json!(5);
        std::fs::write(&path, serde_json::to_vec(&data).unwrap()).unwrap();
        let mut source =
            BundleSource::open(SourceBundle::load(&path, &left, &right, &calibration).unwrap())
                .unwrap();
        assert!((0..6).any(|_| source.next_frame().is_err()));
    }

    fn decoded_timestamp_fixture(path: &Path, timestamps: &[i64]) -> DecodedFrames {
        write_camera(path, 7000, &[0, 1, 2, 3], Some(timestamps));
        DecodedFrames {
            decoder: VideoDecoder::open(path).unwrap(),
            timing: Timing {
                packets: 4,
                first_pts_ns: 7_000_000_000,
                last_pts_ns: 7_300_000_000,
                first_clock_time_ns: 10_000_000_000,
                last_clock_time_ns: 10_300_000_000,
            },
            first_pts_us: None,
            previous_pts_us: None,
            count: 0,
            done: false,
        }
    }

    #[test]
    fn equal_decoded_pts_preserve_counts_and_select_last_image_at_that_time() {
        let dir = tempfile::tempdir().unwrap();
        let frames =
            decoded_timestamp_fixture(&dir.path().join("equal.mkv"), &[7000, 7100, 7100, 7300])
                .collect::<Result<Vec<_>, _>>()
                .unwrap();
        assert_eq!(frames.len(), 4);
        assert_eq!(
            frames.iter().map(|frame| frame.time_ns).collect::<Vec<_>>(),
            [
                10_000_000_000,
                10_100_000_000,
                10_100_000_000,
                10_300_000_000
            ]
        );
        let mut cursor = Cursor::new(frames.into_iter().map(Ok)).unwrap();
        for (time, mark) in [
            (10_000_000_000, 0),
            (10_100_000_000, 2),
            (10_300_000_000, 3),
        ] {
            assert_eq!((cursor.sample(time).unwrap().y[0] - 16) / 40, mark);
        }
        assert_eq!(cursor.skipped, 1);
        assert_eq!(cursor.duplicates, 0);
        cursor.validate_end().unwrap();
    }

    #[test]
    fn backwards_decoded_pts_are_still_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let mut frames =
            decoded_timestamp_fixture(&dir.path().join("backwards.mkv"), &[7000, 7200, 7100, 7300]);
        assert!(frames.next().unwrap().is_ok());
        assert!(frames.next().unwrap().is_ok());
        assert!(
            matches!(frames.next(), Some(Err(error)) if error.to_string().contains("move backwards"))
        );
        assert!(frames.next().is_none());
    }
}
