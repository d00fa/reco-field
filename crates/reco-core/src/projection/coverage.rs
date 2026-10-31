//! Precomputed coverage boundary for "no-black" viewport constraining.

use crate::calibration::MatchCalibration;
use crate::detect::detector::CameraId;
use crate::detect::director::ViewportPosition;
use crate::render::scene::SceneGeometry;
use crate::render::viewport::ViewportConfig;

use super::camera_to_panorama;

// -- Coverage Boundary --
//
// A precomputed, pitch-indexed lookup table of valid yaw ranges for
// "no-black" viewport constraining. Replaces the per-frame frontier
// sampling approach in `viewport_bounds` with O(1) runtime lookups.

/// Precomputed coverage boundary for the stitched panorama.
///
/// Maps each pitch angle to the valid yaw range where both camera planes
/// provide pixel data. Built once from calibration (~20k `camera_to_panorama`
/// calls, <1ms on any modern CPU). Runtime lookups are O(1) via pitch-indexed
/// linear interpolation.
///
/// Use [`safe_clamp`](Self::safe_clamp) to constrain a viewport position
/// so no black edges appear.
#[derive(Debug, Clone)]
pub struct CoverageBoundary {
    n_slices: usize,
    /// Global minimum pitch with any coverage (radians).
    pub pitch_min: f32,
    /// Global maximum pitch with any coverage (radians).
    pub pitch_max: f32,
    /// Per-slice combined coverage: `(yaw_min, yaw_max)`.
    slices: Vec<(f32, f32)>,
    /// Per-slice left-plane coverage: `(yaw_min, yaw_max)`.
    left_slices: Vec<(f32, f32)>,
    /// Per-slice right-plane coverage: `(yaw_min, yaw_max)`.
    right_slices: Vec<(f32, f32)>,
    /// Minimum pitch range across all yaw positions.
    /// Determines the maximum safe FOV.
    min_pitch_range: f32,
    /// Camera position from the scene geometry. Stored so
    /// `safe_clamp` can construct a `VirtualCamera` for the exact
    /// user-to-world rig correction mapping.
    camera_position: [f32; 3],
}

/// Result of clamping a viewport position to the safe panning region.
#[derive(Debug, Clone, Copy)]
pub struct ClampedPosition {
    /// Clamped yaw in radians.
    pub yaw: f32,
    /// Clamped pitch in radians.
    pub pitch: f32,
}

/// Full angular extent of the stitched panorama (radians).
///
/// Returned by [`StitchSession::panorama_extent`](crate::session::StitchSession::panorama_extent).
/// Analytics consumers (heatmaps, zone stats) use this to size grids that
/// span the full coverage rather than hardcoding `+-45 deg yaw, +-20 deg pitch`.
#[derive(Debug, Clone, Copy)]
pub struct PanoramaExtent {
    /// Minimum yaw with coverage from either camera (radians).
    pub yaw_min: f32,
    /// Maximum yaw with coverage from either camera (radians).
    pub yaw_max: f32,
    /// Minimum pitch with coverage from either camera (radians).
    pub pitch_min: f32,
    /// Maximum pitch with coverage from either camera (radians).
    pub pitch_max: f32,
}

impl PanoramaExtent {
    /// Width of the yaw range in radians.
    pub fn yaw_span(&self) -> f32 {
        self.yaw_max - self.yaw_min
    }

    /// Width of the pitch range in radians.
    pub fn pitch_span(&self) -> f32 {
        self.pitch_max - self.pitch_min
    }

    /// Map an angular position in radians to normalized `[0, 1]`
    /// coordinates within this extent.
    ///
    /// Returns `None` if the extent is degenerate (zero span on either
    /// axis). Values outside the extent are returned as-is (not clamped),
    /// so callers can detect out-of-bounds detections.
    pub fn normalize(&self, yaw: f32, pitch: f32) -> Option<(f32, f32)> {
        let yaw_span = self.yaw_span();
        let pitch_span = self.pitch_span();
        if yaw_span <= 0.0 || pitch_span <= 0.0 {
            return None;
        }
        Some((
            (yaw - self.yaw_min) / yaw_span,
            (pitch - self.pitch_min) / pitch_span,
        ))
    }
}

/// Angular offsets of viewport boundary points from center.
///
impl CoverageBoundary {
    /// Fit the rendered frustum to actual source coverage after the coarse pose clamp.
    pub(crate) fn fit_render_fov(
        &self,
        pose: ViewportPosition,
        viewport: &ViewportConfig,
        calibration: &MatchCalibration,
        scene: &SceneGeometry,
    ) -> f32 {
        let requested = pose
            .fov_degrees
            .unwrap_or(viewport.fov_degrees)
            .clamp(1.0, 179.0);
        let view = crate::render::renderer::view_matrix(
            &self.camera_position,
            pose.yaw,
            pose.pitch,
            viewport.rig_tilt,
            viewport.rig_roll,
        );
        let Some(inverse_view) = view.try_inverse() else {
            return requested;
        };
        let camera = super::VirtualCamera::new(&self.camera_position);
        let covered = |x: f32, y: f32, tangent: f32| {
            let ray = inverse_view
                .transform_vector(&nalgebra::Vector3::new(
                    x * tangent * viewport.aspect_ratio(),
                    y * tangent,
                    -1.0,
                ))
                .normalize();
            let point = camera.direction_to_yaw_pitch(&ray);
            [CameraId::Left, CameraId::Right].into_iter().any(|side| {
                super::panorama_to_camera(point.yaw, point.pitch, side, calibration, scene)
                    .is_some()
            })
        };
        let fits = |fov: f32| {
            let tangent = (fov.to_radians() * 0.5).tan();
            (0..=32).all(|i| {
                let p = i as f32 / 16.0 - 1.0;
                covered(p, -1.0, tangent)
                    && covered(p, 1.0, tangent)
                    && covered(-1.0, p, tangent)
                    && covered(1.0, p, tangent)
                    && covered(0.0, p, tangent)
            })
        };
        if fits(requested) {
            return requested;
        }
        // A missing center cannot be recovered by zooming; keep its direction.
        if !fits(1.0) {
            return requested;
        }
        let (mut low, mut high) = (1.0, requested);
        for _ in 0..10 {
            let middle = (low + high) * 0.5;
            if fits(middle) {
                low = middle;
            } else {
                high = middle;
            }
        }
        // Leave a small margin between probes for the curved lens/quad boundary.
        (low - 0.5).max(1.0)
    }

    /// Build the coverage boundary from calibration data.
    ///
    /// Densely samples both planes' edge loops and a sparse interior grid,
    /// projecting into (yaw, pitch) space and grouping into pitch slices.
    pub fn from_calibration(calibration: &MatchCalibration, scene: &SceneGeometry) -> Self {
        let n_slices: usize = 400;
        let margin = 0.02_f32;

        let mut left_points: Vec<(f32, f32)> = Vec::new();
        let mut right_points: Vec<(f32, f32)> = Vec::new();

        for &camera in &[CameraId::Left, CameraId::Right] {
            let points = if camera == CameraId::Left {
                &mut left_points
            } else {
                &mut right_points
            };

            // Dense edge sampling: 400 points per edge (4 edges = 1600 per plane)
            let edge_steps = 400_u32;
            for i in 0..=edge_steps {
                let t = margin + (1.0 - 2.0 * margin) * (i as f32 / edge_steps as f32);
                for &(nx, ny) in &[
                    (t, margin),
                    (t, 1.0 - margin),
                    (margin, t),
                    (1.0 - margin, t),
                ] {
                    if let Some(pos) = camera_to_panorama(camera, nx, ny, calibration, scene) {
                        points.push((pos.yaw, pos.pitch));
                    }
                }
            }

            // Sparse interior grid (20x20) for coverage at intermediate pitch levels
            let grid_steps = 20_u32;
            for ix in 0..=grid_steps {
                let nx = margin + (1.0 - 2.0 * margin) * (ix as f32 / grid_steps as f32);
                for iy in 0..=grid_steps {
                    let ny = margin + (1.0 - 2.0 * margin) * (iy as f32 / grid_steps as f32);
                    if let Some(pos) = camera_to_panorama(camera, nx, ny, calibration, scene) {
                        points.push((pos.yaw, pos.pitch));
                    }
                }
            }
        }

        // Find global pitch range
        let all_points = left_points.iter().chain(right_points.iter());
        let mut global_pitch_min = f32::MAX;
        let mut global_pitch_max = f32::MIN;
        for &(_, pitch) in all_points {
            global_pitch_min = global_pitch_min.min(pitch);
            global_pitch_max = global_pitch_max.max(pitch);
        }

        if global_pitch_min >= global_pitch_max {
            return Self {
                n_slices,
                pitch_min: 0.0,
                pitch_max: 0.0,
                slices: vec![(0.0, 0.0); n_slices],
                left_slices: vec![(0.0, 0.0); n_slices],
                right_slices: vec![(0.0, 0.0); n_slices],
                min_pitch_range: 0.0,
                camera_position: scene.camera_position,
            };
        }

        let pitch_range = global_pitch_max - global_pitch_min;
        let slice_size = pitch_range / n_slices as f32;

        // Bucket points into pitch slices
        let mut slices = vec![(f32::MAX, f32::MIN); n_slices];
        let mut left_slices = vec![(f32::MAX, f32::MIN); n_slices];
        let mut right_slices = vec![(f32::MAX, f32::MIN); n_slices];

        let pitch_to_slice = |pitch: f32| -> usize {
            let idx = ((pitch - global_pitch_min) / slice_size) as usize;
            idx.min(n_slices - 1)
        };

        for &(yaw, pitch) in &left_points {
            let s = pitch_to_slice(pitch);
            left_slices[s].0 = left_slices[s].0.min(yaw);
            left_slices[s].1 = left_slices[s].1.max(yaw);
            slices[s].0 = slices[s].0.min(yaw);
            slices[s].1 = slices[s].1.max(yaw);
        }
        for &(yaw, pitch) in &right_points {
            let s = pitch_to_slice(pitch);
            right_slices[s].0 = right_slices[s].0.min(yaw);
            right_slices[s].1 = right_slices[s].1.max(yaw);
            slices[s].0 = slices[s].0.min(yaw);
            slices[s].1 = slices[s].1.max(yaw);
        }

        // Compute min pitch range (determines max FOV)
        let min_pitch_range = {
            let quarter = n_slices / 4;
            let three_quarter = 3 * n_slices / 4;
            let mut yaw_lo = f32::MAX;
            let mut yaw_hi = f32::MIN;
            for s in &slices[quarter..three_quarter] {
                if s.0 <= s.1 {
                    yaw_lo = yaw_lo.min(s.0);
                    yaw_hi = yaw_hi.max(s.1);
                }
            }

            if yaw_lo >= yaw_hi {
                pitch_range
            } else {
                // Sample pitch range at multiple yaw positions and use the
                // 10th percentile instead of absolute minimum. The absolute
                // minimum is dominated by narrow seam edges which the director
                // rarely visits. The 10th percentile gives a practical FOV
                // that works across most of the useful yaw range.
                let n_samples = 50;
                let mut ranges = Vec::with_capacity(n_samples + 1);
                for j in 0..=n_samples {
                    let t = j as f32 / n_samples as f32;
                    let test_yaw = yaw_lo + t * (yaw_hi - yaw_lo);
                    let mut p_lo = f32::MAX;
                    let mut p_hi = f32::MIN;
                    for (s, (left, right)) in
                        left_slices.iter().zip(right_slices.iter()).enumerate()
                    {
                        let in_left = left.0 <= left.1 && test_yaw >= left.0 && test_yaw <= left.1;
                        let in_right =
                            right.0 <= right.1 && test_yaw >= right.0 && test_yaw <= right.1;
                        if in_left || in_right {
                            let p = global_pitch_min + (s as f32 + 0.5) * slice_size;
                            p_lo = p_lo.min(p);
                            p_hi = p_hi.max(p);
                        }
                    }
                    if p_hi > p_lo {
                        ranges.push(p_hi - p_lo);
                    }
                }
                if ranges.is_empty() {
                    pitch_range
                } else {
                    ranges.sort_by(|a, b| a.partial_cmp(b).unwrap());
                    // 10th percentile: skip the narrowest 10%
                    let idx = (ranges.len() / 10).min(ranges.len() - 1);
                    ranges[idx]
                }
            }
        };

        // Fill gaps: slices with no samples inherit from neighbors
        for i in 1..n_slices {
            if slices[i].0 > slices[i].1 {
                slices[i] = slices[i - 1];
                left_slices[i] = left_slices[i - 1];
                right_slices[i] = right_slices[i - 1];
            }
        }
        for i in (0..n_slices - 1).rev() {
            if slices[i].0 > slices[i].1 {
                slices[i] = slices[i + 1];
                left_slices[i] = left_slices[i + 1];
                right_slices[i] = right_slices[i + 1];
            }
        }

        log::debug!(
            "CoverageBoundary: pitch [{:.3}, {:.3}] ({:.1} deg), min pitch range {:.1} deg",
            global_pitch_min,
            global_pitch_max,
            pitch_range.to_degrees(),
            min_pitch_range.to_degrees(),
        );

        Self {
            n_slices,
            pitch_min: global_pitch_min,
            pitch_max: global_pitch_max,
            slices,
            left_slices,
            right_slices,
            min_pitch_range,
            camera_position: scene.camera_position,
        }
    }

    /// Global yaw coverage range across the full panorama (radians).
    ///
    /// Returns `(yaw_min, yaw_max)`, the widest-point extremes of the
    /// stitched panorama. Useful for heatmap consumers that need to
    /// bucket detections by yaw across the full coverage.
    ///
    /// This is the global extent, not the per-pitch range. For a
    /// pitch-aware range, sample with [`yaw_range_at`](Self::yaw_range_at).
    pub fn yaw_range(&self) -> (f32, f32) {
        let mut lo = f32::INFINITY;
        let mut hi = f32::NEG_INFINITY;
        for &(a, b) in &self.slices {
            if a <= b {
                lo = lo.min(a);
                hi = hi.max(b);
            }
        }
        if lo > hi { (0.0, 0.0) } else { (lo, hi) }
    }

    /// Global pitch coverage range across the full panorama (radians).
    ///
    /// Returns `(pitch_min, pitch_max)`. Used alongside
    /// [`yaw_range`](Self::yaw_range) by heatmap and analytics consumers
    /// that need panorama bounds without reaching into private state.
    pub fn pitch_range(&self) -> (f32, f32) {
        (self.pitch_min, self.pitch_max)
    }

    /// Look up the combined yaw coverage range at a given pitch.
    ///
    /// Returns the interpolated `(yaw_min, yaw_max)` where at least one
    /// camera plane provides coverage. Used by the director for
    /// perspective-aware clamping; analytics consumers typically want
    /// [`yaw_range`](Self::yaw_range) for the global extent instead.
    pub fn yaw_range_at(&self, pitch: f32) -> (f32, f32) {
        self.interpolate_slice(&self.slices, pitch)
    }

    /// Interpolate a slice table at the given pitch.
    fn interpolate_slice(&self, table: &[(f32, f32)], pitch: f32) -> (f32, f32) {
        if self.n_slices == 0 || self.pitch_max <= self.pitch_min {
            return (0.0, 0.0);
        }
        let pitch_range = self.pitch_max - self.pitch_min;
        let t = (pitch - self.pitch_min) / pitch_range;
        let idx_f = t * (self.n_slices - 1) as f32;
        let idx_lo = (idx_f.floor() as usize).min(self.n_slices - 1);
        let idx_hi = (idx_lo + 1).min(self.n_slices - 1);
        let frac = idx_f - idx_lo as f32;

        let lo = table[idx_lo];
        let hi = table[idx_hi];
        if lo.0 > lo.1 {
            return hi;
        }
        if hi.0 > hi.1 {
            return lo;
        }
        (lo.0 + frac * (hi.0 - lo.0), lo.1 + frac * (hi.1 - lo.1))
    }

    /// Clamp a viewport position to the safe panning region for a given FOV.
    ///
    /// `rig_tilt` (radians) accounts for the renderer's rig tilt rotation.
    /// The caller passes user-space (yaw, pitch); this method transforms
    /// to world space (+rig_tilt), clamps against coverage, then transforms
    /// back. Pass 0.0 when there is no rig tilt.
    ///
    /// `self` must be the **world-space** coverage boundary.
    pub fn safe_clamp(
        &self,
        yaw: f32,
        pitch: f32,
        fov_v_deg: f32,
        aspect: f32,
        rig_tilt: f32,
    ) -> ClampedPosition {
        let world_pitch = crate::lens::rig_correction::human_to_world_pitch(yaw, pitch, rig_tilt);
        let clamped = self.safe_clamp_world(yaw, world_pitch, fov_v_deg, aspect);
        ClampedPosition {
            yaw: clamped.yaw,
            pitch: crate::lens::rig_correction::world_to_human_pitch(
                clamped.yaw,
                clamped.pitch,
                rig_tilt,
            ),
        }
    }

    /// Clamp viewport center to coverage with perspective-correct margins.
    fn safe_clamp_world(
        &self,
        yaw: f32,
        pitch: f32,
        fov_v_deg: f32,
        aspect: f32,
    ) -> ClampedPosition {
        // B-30 defense: non-finite inputs would propagate through the
        // clamp / comparisons and emit NaN, which then flows into the
        // MVP matrix and produces a black or garbage frame. Upstream
        // guards (B-28 detector boundary, B-29 director EMA) stop most
        // NaN at the source, but user overrides and external clients
        // can still hand us non-finite values. Fall back to the
        // coverage center.
        if !yaw.is_finite() || !pitch.is_finite() || !fov_v_deg.is_finite() || !aspect.is_finite() {
            let safe_pitch = (self.pitch_min + self.pitch_max) * 0.5;
            let (yaw_lo, yaw_hi) = self.yaw_range_at(safe_pitch);
            let safe_yaw = (yaw_lo + yaw_hi) * 0.5;
            return ClampedPosition {
                yaw: safe_yaw,
                pitch: safe_pitch,
            };
        }

        let half_vfov = (fov_v_deg * 0.5).to_radians();
        let half_hfov = (aspect * half_vfov.tan()).atan();

        // Pitch: global bounds with vertical FOV margin
        let clamped_pitch = if self.pitch_min + half_vfov <= self.pitch_max - half_vfov {
            pitch.clamp(self.pitch_min + half_vfov, self.pitch_max - half_vfov)
        } else {
            (self.pitch_min + self.pitch_max) * 0.5
        };

        // Yaw: coverage range at clamped pitch with horizontal FOV margin
        let (yaw_lo, yaw_hi) = self.yaw_range_at(clamped_pitch);
        let clamped_yaw = if yaw_lo + half_hfov <= yaw_hi - half_hfov {
            yaw.clamp(yaw_lo + half_hfov, yaw_hi - half_hfov)
        } else {
            (yaw_lo + yaw_hi) * 0.5
        };

        ClampedPosition {
            yaw: clamped_yaw,
            pitch: clamped_pitch,
        }
    }

    /// Maximum vertical FOV (degrees) that fits within the coverage.
    ///
    /// This is the widest zoom-out where at least one valid viewport
    /// position exists. Determined by the narrowest pitch range across
    /// all yaw positions (typically at the seam between planes).
    pub fn max_fov_degrees(&self) -> f32 {
        if self.min_pitch_range <= 0.0 {
            return 20.0;
        }
        self.min_pitch_range.to_degrees()
    }

    /// Create a copy with all pitch values shifted by an offset.
    ///
    /// Used to create a tilt-adjusted boundary for the director, which
    /// operates in pre-tilt space while the boundary is in world space.
    /// The director calls `safe_clamp` on the shifted boundary without
    /// needing to know about rig tilt.
    pub fn with_pitch_offset(&self, offset: f32) -> Self {
        Self {
            n_slices: self.n_slices,
            pitch_min: self.pitch_min + offset,
            pitch_max: self.pitch_max + offset,
            slices: self.slices.clone(),
            left_slices: self.left_slices.clone(),
            right_slices: self.right_slices.clone(),
            min_pitch_range: self.min_pitch_range,
            camera_position: self.camera_position,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::calibration::{CameraParams, PlaneLayout};

    fn garage_calibration() -> MatchCalibration {
        let lens = CameraParams {
            width: 4032,
            height: 3040,
            fx: 2092.640480434009,
            fy: 2083.712938662459,
            cx: 2055.922945560364,
            cy: 1479.3015421724676,
            d: [
                -0.0008852475107180186,
                0.04858733977083213,
                -0.060099529576326534,
                0.02735409281151152,
            ],
        };
        MatchCalibration {
            left: lens.clone(),
            right: lens,
            layout: PlaneLayout {
                camera_axis_offset: 0.24960595819567188,
                intersect: 0.46521607427309203,
                x_ty: -0.043550260140647765,
                x_rz: 0.004191979829482283,
                z_rx: -0.12882016960454748,
                x_rx: 0.0,
                z_rz: 0.0,
            },
            rig_tilt: 0.0,
            rig_roll: 0.0,
            sync_offset: 0,
            field_roi: None,
            lens_correction_amount: 1.0,
            blend_width: 0.15,
        }
    }

    fn uncovered_samples(
        calibration: &MatchCalibration,
        scene: &SceneGeometry,
        viewport: &ViewportConfig,
        pose: ViewportPosition,
        fov: f32,
    ) -> usize {
        let projection =
            nalgebra::Perspective3::new(viewport.aspect_ratio(), fov.to_radians(), 0.01, 100.0);
        let view = crate::render::renderer::view_matrix(
            &scene.camera_position,
            pose.yaw,
            pose.pitch,
            viewport.rig_tilt,
            viewport.rig_roll,
        );
        let inverse = (projection.to_homogeneous() * view).try_inverse().unwrap();
        let camera = super::super::VirtualCamera::new(&scene.camera_position);
        let covered = |x, y| {
            let world = inverse.transform_point(&nalgebra::Point3::new(x, y, 0.0));
            let ray = (world.coords - camera.eye).normalize();
            let point = camera.direction_to_yaw_pitch(&ray);
            [CameraId::Left, CameraId::Right].into_iter().any(|side| {
                super::super::panorama_to_camera(point.yaw, point.pitch, side, calibration, scene)
                    .is_some()
            })
        };
        let mut uncovered = 0;
        for i in 0..=512 {
            let p = i as f32 / 256.0 - 1.0;
            for (x, y) in [(p, -1.0), (p, 1.0), (-1.0, p), (1.0, p), (0.0, p)] {
                uncovered += usize::from(!covered(x, y));
            }
        }
        uncovered
    }

    #[test]
    fn render_fov_removes_actual_rig_top_notch() {
        let calibration = garage_calibration();
        let scene = SceneGeometry::from_layout_with_aspect(&calibration.layout, 4032.0 / 3040.0);
        let coverage = CoverageBoundary::from_calibration(&calibration, &scene);
        let viewport = ViewportConfig::default();
        let requested = viewport.fov_degrees.min(coverage.max_fov_degrees());
        let clamped = coverage.safe_clamp(0.0, 0.0, requested, viewport.aspect_ratio(), 0.0);
        let pose = ViewportPosition {
            yaw: clamped.yaw,
            pitch: clamped.pitch,
            fov_degrees: Some(requested),
        };
        assert!(uncovered_samples(&calibration, &scene, &viewport, pose, requested) > 0);
        let fitted = coverage.fit_render_fov(pose, &viewport, &calibration, &scene);
        assert!(
            fitted > requested * 0.85,
            "excessive zoom: {requested} -> {fitted}"
        );
        assert!(fitted < requested);
        assert_eq!(
            uncovered_samples(&calibration, &scene, &viewport, pose, fitted),
            0
        );
        let fitted_pose = ViewportPosition {
            fov_degrees: Some(fitted),
            ..pose
        };
        assert_eq!(
            coverage.fit_render_fov(fitted_pose, &viewport, &calibration, &scene),
            fitted
        );
        let next_center = coverage.safe_clamp(0.0, 0.0, fitted, viewport.aspect_ratio(), 0.0);
        let next_pose = ViewportPosition {
            yaw: next_center.yaw,
            pitch: next_center.pitch,
            fov_degrees: Some(fitted),
        };
        assert_eq!(
            coverage.fit_render_fov(next_pose, &viewport, &calibration, &scene),
            fitted
        );
    }

    #[test]
    fn render_fov_checks_actual_tilted_and_rolled_view() {
        let calibration = garage_calibration();
        let scene = SceneGeometry::from_layout_with_aspect(&calibration.layout, 4032.0 / 3040.0);
        let coverage = CoverageBoundary::from_calibration(&calibration, &scene);
        let viewport = ViewportConfig {
            rig_tilt: 0.25,
            rig_roll: 0.12,
            ..ViewportConfig::default()
        };
        let camera = super::super::VirtualCamera::new(&scene.camera_position);
        let (yaw, pitch) = crate::lens::rig_correction::world_to_render_pose(
            &camera,
            0.0,
            0.0,
            viewport.rig_tilt,
            viewport.rig_roll,
        );
        let pose = ViewportPosition {
            yaw,
            pitch,
            fov_degrees: Some(75.0),
        };
        let fitted = coverage.fit_render_fov(pose, &viewport, &calibration, &scene);
        assert!(
            fitted > 45.0 && fitted <= 75.0,
            "unexpected fitted FOV: {fitted}"
        );
        assert_eq!(
            uncovered_samples(&calibration, &scene, &viewport, pose, fitted),
            0
        );
    }

    #[test]
    fn render_fov_does_not_move_an_uncovered_center() {
        let calibration = garage_calibration();
        let scene = SceneGeometry::from_layout_with_aspect(&calibration.layout, 4032.0 / 3040.0);
        let coverage = CoverageBoundary::from_calibration(&calibration, &scene);
        let pose = ViewportPosition {
            yaw: std::f32::consts::PI,
            pitch: 0.0,
            fov_degrees: Some(75.0),
        };
        assert_eq!(
            coverage.fit_render_fov(pose, &ViewportConfig::default(), &calibration, &scene),
            75.0
        );
    }
}
