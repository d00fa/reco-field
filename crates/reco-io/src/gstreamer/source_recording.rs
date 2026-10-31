//! Separate-camera files and their capture-time provenance.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;

use super::camera::{CameraConfig, CameraControls};

/// Settings for the independent H.264 camera recording branches.
#[derive(Clone, Debug, Serialize)]
pub struct SourceRecordingConfig {
    /// A new session directory; existing directories are never reused.
    pub directory: PathBuf,
    /// Saved camera width, independent of the stitched output.
    pub width: u32,
    /// Saved camera height, preserving the capture aspect ratio.
    pub height: u32,
    /// x264 constant-rate-factor quality, from 0 to 51.
    pub crf: u32,
}

impl SourceRecordingConfig {
    /// Validate recording dimensions and destination before opening cameras.
    pub fn validate(&self, capture: &CameraConfig) -> Result<(), String> {
        if self.width < 16
            || self.height < 16
            || self.width > capture.width
            || self.height > capture.height
            || !self.width.is_multiple_of(2)
            || !self.height.is_multiple_of(2)
            || self.width > 8192
            || self.height > 8192
            || self.crf > 51
        {
            return Err("source dimensions must be even, at least 16, and no larger than capture; CRF must be 0..=51".into());
        }
        if u64::from(self.width) * u64::from(capture.height)
            != u64::from(self.height) * u64::from(capture.width)
        {
            return Err("source dimensions must preserve the full camera aspect ratio".into());
        }
        if self.directory.as_os_str().is_empty()
            || self.directory.to_str().is_none()
            || self.directory.to_string_lossy().contains("://")
            || self.directory.exists()
        {
            return Err("source directory must be a new local UTF-8 path".into());
        }
        Ok(())
    }
}

/// Timing measured at an encoder input and its parsed output, before muxing.
#[derive(Clone, Debug, Default, Serialize)]
pub struct SourceTiming {
    /// Number of encoded access units, excluding any untimed codec header.
    pub packets: u64,
    /// First input timestamp before x264's timestamp offset.
    pub first_input_pts_ns: Option<u64>,
    /// First input timestamp mapped through its segment to the shared clock.
    pub first_input_clock_time_ns: Option<u64>,
    /// First parsed encoded timestamp, before Matroska rebases it.
    pub first_pts_ns: Option<u64>,
    /// Last parsed encoded timestamp, before Matroska rebases it.
    pub last_pts_ns: Option<u64>,
    /// First encoded timestamp mapped through its segment to the shared clock.
    pub first_clock_time_ns: Option<u64>,
    /// Last encoded timestamp mapped through its segment to the shared clock.
    pub last_clock_time_ns: Option<u64>,
    /// Negotiated encoded caps, including colorimetry when available.
    pub caps: Option<String>,
}

/// Writes a durable manifest only after both camera files have finalized.
pub struct SourceRecording {
    config: SourceRecordingConfig,
    manifest: serde_json::Value,
    finalized: bool,
}

impl SourceRecording {
    /// Reserve a fresh directory and snapshot the exact calibration in use.
    pub fn create(
        config: SourceRecordingConfig,
        capture: &CameraConfig,
        controls: &CameraControls,
        calibration: &reco_core::calibration::MatchCalibration,
    ) -> Result<Self, String> {
        config.validate(capture)?;
        std::fs::create_dir(&config.directory)
            .map_err(|e| format!("create source directory: {e}"))?;
        let mut recording = Self {
            manifest: serde_json::json!({
                "version": 1, "state": "recording", "error": null,
                "started_unix_ms": unix_ms(), "finished_unix_ms": null,
                "capture": capture, "controls": controls,
                "source": {"width": config.width, "height": config.height, "codec": "h264", "colorimetry": "bt601-limited",
                    "encoder": "x264enc", "preset": "ultrafast", "crf": config.crf},
                "calibration": "calibration.json",
                "lens_profiles": {"left": "left-lens.json", "right": "right-lens.json",
                    "origin": "effective intrinsics embedded in calibration"},
                "clock": "shared GstSystemClock", "clock_comparable": false,
                "hardware_synchronized": false,
                "left": {"file": "left.mkv", "timing": null},
                "right": {"file": "right.mkv", "timing": null}
            }),
            config,
            finalized: false,
        };
        let snapshot = (|| {
            write_new_json(
                &recording.config.directory.join("calibration.json"),
                calibration,
            )?;
            write_new_json(
                &recording.config.directory.join("left-lens.json"),
                &calibration.left,
            )?;
            write_new_json(
                &recording.config.directory.join("right-lens.json"),
                &calibration.right,
            )?;
            recording.write_manifest()
        })();
        if let Err(error) = snapshot {
            let _ = recording.finish(
                &[SourceTiming::default(), SourceTiming::default()],
                Some(&error),
            );
            return Err(error);
        }
        Ok(recording)
    }

    /// Settings already validated while reserving the recording directory.
    pub fn config(&self) -> &SourceRecordingConfig {
        &self.config
    }

    /// Complete the manifest, or retain an explicit incomplete reason.
    pub fn finish(
        &mut self,
        timing: &[SourceTiming; 2],
        error: Option<&str>,
    ) -> Result<(), String> {
        let valid_times = timing.iter().all(|t| {
            t.packets > 0
                && t.first_pts_ns
                    .zip(t.last_pts_ns)
                    .is_some_and(|(first, last)| first <= last)
                && t.first_clock_time_ns
                    .zip(t.last_clock_time_ns)
                    .is_some_and(|(first, last)| first <= last)
                && t.first_clock_time_ns
                    .zip(t.first_input_clock_time_ns)
                    .is_some_and(|(encoded, input)| encoded.abs_diff(input) <= 1_000_000)
        });
        let files_present = ["left.mkv", "right.mkv"].iter().all(|name| {
            self.config
                .directory
                .join(name)
                .metadata()
                .is_ok_and(|meta| meta.is_file() && meta.len() > 0)
        });
        let failure = error.or(if !valid_times {
            Some("source recording has no complete timestamped video")
        } else if !files_present {
            Some("source recording is missing a nonempty camera file")
        } else {
            None
        });
        self.manifest["left"]["timing"] =
            serde_json::to_value(&timing[0]).map_err(|e| e.to_string())?;
        self.manifest["right"]["timing"] =
            serde_json::to_value(&timing[1]).map_err(|e| e.to_string())?;
        self.manifest["clock_comparable"] = valid_times.into();
        self.manifest["finished_unix_ms"] = unix_ms().into();
        self.manifest["state"] = if failure.is_some() {
            "incomplete"
        } else {
            "complete"
        }
        .into();
        self.manifest["error"] = failure.map(serde_json::Value::from).unwrap_or_default();
        self.write_manifest()?;
        self.finalized = true;
        failure.map_or(Ok(()), |error| Err(error.to_owned()))
    }

    fn write_manifest(&self) -> Result<(), String> {
        let mut file =
            tempfile::NamedTempFile::new_in(&self.config.directory).map_err(|e| e.to_string())?;
        serde_json::to_writer_pretty(file.as_file_mut(), &self.manifest)
            .map_err(|e| e.to_string())?;
        file.as_file_mut()
            .write_all(b"\n")
            .map_err(|e| e.to_string())?;
        file.as_file().sync_all().map_err(|e| e.to_string())?;
        file.persist(self.config.directory.join("manifest.json"))
            .map_err(|e| e.to_string())?;
        Ok(())
    }
}

impl Drop for SourceRecording {
    fn drop(&mut self) {
        if !self.finalized {
            self.manifest["state"] = "incomplete".into();
            self.manifest["error"] = "capture ended without confirmed file finalization".into();
            self.manifest["finished_unix_ms"] = unix_ms().into();
            if let Err(error) = self.write_manifest() {
                log::error!("source manifest: {error}");
            }
        }
    }
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn write_new_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(|e| e.to_string())?;
    serde_json::to_writer_pretty(&mut file, value).map_err(|e| e.to_string())?;
    file.sync_all().map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_stretch_upscale_odd_dimensions_and_existing_destinations() {
        let temp = tempfile::tempdir().unwrap();
        let capture = CameraConfig {
            width: 4032,
            height: 3040,
            fps: 30,
            left_device: "0".into(),
            right_device: "1".into(),
        };
        let mut config = SourceRecordingConfig {
            directory: temp.path().join("new"),
            width: 2016,
            height: 1520,
            crf: 18,
        };
        assert!(config.validate(&capture).is_ok());
        for (w, h) in [(1920, 1080), (4034, 3040), (2015, 1520), (0, 0)] {
            config.width = w;
            config.height = h;
            assert!(config.validate(&capture).is_err());
        }
        config.width = 2016;
        config.height = 1520;
        config.directory = temp.path().to_owned();
        assert!(config.validate(&capture).is_err());
    }

    fn recording(directory: PathBuf) -> SourceRecording {
        let lens = serde_json::json!({"width":4032,"height":3040,"fx":1000.0,"fy":1000.0,"cx":2016.0,"cy":1520.0,"d":[0.0,0.0,0.0,0.0]});
        let calibration = serde_json::from_value(serde_json::json!({
            "left_uniforms":lens,"right_uniforms":lens,
            "params":{"cameraAxisOffset":0.2,"intersect":0.4,"xTy":0.0,"xRz":0.0,"zRx":0.0}
        }))
        .unwrap();
        SourceRecording::create(
            SourceRecordingConfig {
                directory,
                width: 2016,
                height: 1520,
                crf: 18,
            },
            &CameraConfig {
                width: 4032,
                height: 3040,
                fps: 30,
                left_device: "0".into(),
                right_device: "1".into(),
            },
            &CameraControls::default(),
            &calibration,
        )
        .unwrap()
    }

    #[test]
    fn snapshots_survive_failed_capture_and_unconfirmed_shutdown() {
        let temp = tempfile::tempdir().unwrap();
        let dir = temp.path().join("session");
        let capture = recording(dir.clone());
        let original = std::fs::read(dir.join("calibration.json")).unwrap();
        drop(capture);
        let manifest: serde_json::Value =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json")).unwrap()).unwrap();
        assert_eq!(manifest["state"], "incomplete");
        assert_eq!(manifest["hardware_synchronized"], false);
        assert_eq!(
            std::fs::read(dir.join("calibration.json")).unwrap(),
            original
        );
        assert!(dir.join("left-lens.json").is_file());
        assert!(dir.join("right-lens.json").is_file());
    }

    #[test]
    fn completion_requires_both_timestamped_files_and_no_capture_failure() {
        let temp = tempfile::tempdir().unwrap();
        let valid = SourceTiming {
            packets: 2,
            first_input_pts_ns: Some(1),
            first_input_clock_time_ns: Some(10),
            first_pts_ns: Some(3_600_000_000_000_001),
            last_pts_ns: Some(3_600_000_033_333_334),
            first_clock_time_ns: Some(10),
            last_clock_time_ns: Some(33_333_343),
            caps: Some("video/x-h264".into()),
        };
        for (name, timing, error, complete) in [
            ("complete", [valid.clone(), valid.clone()], None, true),
            (
                "missing",
                [valid.clone(), SourceTiming::default()],
                None,
                false,
            ),
            ("eos", [valid.clone(), valid], Some("EOS timed out"), false),
        ] {
            let dir = temp.path().join(name);
            let mut capture = recording(dir.clone());
            std::fs::write(dir.join("left.mkv"), b"test encoded data").unwrap();
            std::fs::write(dir.join("right.mkv"), b"test encoded data").unwrap();
            assert_eq!(capture.finish(&timing, error).is_ok(), complete);
            drop(capture);
            let manifest: serde_json::Value =
                serde_json::from_slice(&std::fs::read(dir.join("manifest.json")).unwrap()).unwrap();
            assert_eq!(
                manifest["state"],
                if complete { "complete" } else { "incomplete" }
            );
            assert_eq!(
                manifest["left"]["timing"]["first_pts_ns"],
                3_600_000_000_000_001_u64
            );
        }
    }
}
