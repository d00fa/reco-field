//! Coverage-fitted cylindrical output in the calibrated rig coordinate frame.

use nalgebra::{Matrix4, Point3, Vector3};

use super::{renderer::view_matrix, scene::SceneGeometry, viewport::ViewportConfig};
use crate::calibration::{CameraParams, MatchCalibration};

/// A cylindrical image rectangle. Vertical coordinates are cylinder heights,
/// not pitch angles, so equal horizontal and vertical pixel scales are retained.
#[derive(Debug, Clone, Copy)]
pub struct PanoramaBounds {
    /// Leftmost angle in radians relative to the corrected rig centre.
    pub yaw_min: f32,
    /// Horizontal angular span in radians, at most pi.
    pub yaw_span: f32,
    /// Upper cylinder height (the tangent of pitch).
    pub height_top: f32,
    /// Vertical cylinder height span.
    pub height_span: f32,
}

impl PanoramaBounds {
    /// Actual horizontal coverage in degrees.
    pub fn horizontal_degrees(self) -> f32 {
        self.yaw_span.to_degrees()
    }

    /// Actual vertical coverage in degrees.
    pub fn vertical_degrees(self) -> f32 {
        (self.height_top.atan() - (self.height_top - self.height_span).atan()).to_degrees()
    }

    /// Direction in the corrected rig frame for normalized output coordinates.
    pub fn ray(self, u: f32, v: f32) -> Vector3<f32> {
        let yaw = self.yaw_min + u * self.yaw_span;
        Vector3::new(
            yaw.sin(),
            self.height_top - v * self.height_span,
            -yaw.cos(),
        )
    }
}

pub(crate) struct PanoramaGeometry {
    pub bounds: PanoramaBounds,
    pub view_to_plane: [Matrix4<f32>; 2],
    pub plane_aspect: f32,
    pub plane_width: f32,
}

struct Coverage<'a> {
    transforms: [Matrix4<f32>; 2],
    cameras: [&'a CameraParams; 2],
    aspect: f32,
    width: f32,
    correction: f64,
}

impl Coverage<'_> {
    fn covered(&self, yaw: f32, height: f32) -> bool {
        let ray = Vector3::new(yaw.sin(), height, -yaw.cos());
        (0..2).any(|camera| self.source_uv(camera, ray).is_some())
    }

    fn source_uv(&self, camera: usize, ray: Vector3<f32>) -> Option<(f64, f64)> {
        let transform = &self.transforms[camera];
        let origin = transform.transform_point(&Point3::origin());
        let direction = transform.transform_vector(&ray);
        if direction.z.abs() < 1e-6 {
            return None;
        }
        let t = -origin.z / direction.z;
        if t <= 0.0 {
            return None;
        }
        let hit = origin + direction * t;
        let u = hit.x / self.width + 0.5;
        let v = 0.5 - hit.y * self.aspect / self.width;
        if !(0.0..=1.0).contains(&u) || !(0.0..=1.0).contains(&v) {
            return None;
        }
        let p = self.cameras[camera];
        let fx = p.fx / p.width as f64;
        let fy = p.fy / p.height as f64;
        let cx = p.cx / p.width as f64;
        let cy = p.cy / p.height as f64;
        let x = (f64::from(u * 2.0 - 0.5) - cx) / fx;
        let y = (f64::from(v * 2.0 - 0.5) - cy) / fy;
        let r = x.hypot(y);
        let theta = r.atan();
        let theta_d = theta + self.correction * (crate::lens::kb4::theta_d(theta, &p.d) - theta);
        let scale = if r > 0.0 { theta_d / r } else { 1.0 };
        let source = (fx * x * scale + cx, fy * y * scale + cy);
        ((0.0..=1.0).contains(&source.0) && (0.0..=1.0).contains(&source.1)).then_some(source)
    }
}

impl PanoramaGeometry {
    pub fn fit(
        scene: &SceneGeometry,
        calibration: &MatchCalibration,
        viewport: &ViewportConfig,
    ) -> Result<Self, String> {
        let inverse_view = view_matrix(
            &scene.camera_position,
            0.0,
            0.0,
            viewport.rig_tilt,
            viewport.rig_roll,
        )
        .try_inverse()
        .ok_or("singular panorama view")?;
        let transforms = [
            scene
                .model_matrix_left()
                .try_inverse()
                .ok_or("singular left plane")?
                * inverse_view,
            scene
                .model_matrix_right()
                .try_inverse()
                .ok_or("singular right plane")?
                * inverse_view,
        ];
        let coverage = Coverage {
            transforms,
            cameras: [&calibration.left, &calibration.right],
            aspect: scene.plane_aspect,
            width: scene.plane_width,
            correction: f64::from(viewport.lens_correction_amount),
        };
        // An invalid-cell integral image permits a bounded rectangle search,
        // including holes between camera footprints instead of just envelopes.
        const COLS: usize = 480;
        const ROWS: usize = 320;
        const HEIGHT: f32 = 3.2;
        let dx = std::f32::consts::PI / COLS as f32;
        let dy = HEIGHT / ROWS as f32;
        let mut invalid = vec![0_u32; (COLS + 1) * (ROWS + 1)];
        for row in 0..ROWS {
            for col in 0..COLS {
                let valid = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.5, 0.5)]
                    .into_iter()
                    .all(|(x, y)| {
                        coverage.covered(
                            -std::f32::consts::FRAC_PI_2 + (col as f32 + x) * dx,
                            HEIGHT * 0.5 - (row as f32 + y) * dy,
                        )
                    });
                let i = (row + 1) * (COLS + 1) + col + 1;
                invalid[i] = u32::from(!valid) + invalid[i - 1] + invalid[i - COLS - 1]
                    - invalid[i - COLS - 2];
            }
        }
        let find = |width: usize| -> Option<(usize, usize, usize)> {
            let height = (width as f32 * dx / viewport.aspect_ratio() / dy).ceil() as usize;
            if height == 0 || height > ROWS {
                return None;
            }
            let mut best = None;
            let mut score = f32::INFINITY;
            for row in 0..=ROWS - height {
                for col in 0..=COLS - width {
                    let a = row * (COLS + 1) + col;
                    let b = a + width;
                    let c = (row + height) * (COLS + 1) + col;
                    let d = c + width;
                    if invalid[d] + invalid[a] != invalid[b] + invalid[c] {
                        continue;
                    }
                    let x = (col as f32 + width as f32 * 0.5) * dx - std::f32::consts::FRAC_PI_2;
                    let y = HEIGHT * 0.5 - (row as f32 + height as f32 * 0.5) * dy;
                    let candidate = x * x + y * y;
                    if candidate < score {
                        score = candidate;
                        best = Some((col, row, height));
                    }
                }
            }
            best
        };
        let mut low = 1;
        let mut high = COLS;
        let mut chosen = None;
        while low <= high {
            let width = (low + high) / 2;
            if let Some((col, row, height)) = find(width) {
                chosen = Some((width, col, row, height));
                low = width + 1;
            } else {
                high = width - 1;
            }
        }
        let (width, col, row, height) =
            chosen.ok_or("no covered cylindrical panorama rectangle")?;
        let span = (width as f32 - 1.0) * dx;
        if span < 5.0_f32.to_radians() {
            return Err(
                "calibration has less than five degrees of usable panorama coverage".into(),
            );
        }
        let centre_x = -std::f32::consts::FRAC_PI_2 + (col as f32 + width as f32 * 0.5) * dx;
        let centre_y = HEIGHT * 0.5 - (row as f32 + height as f32 * 0.5) * dy;
        let mut bounds = PanoramaBounds {
            yaw_min: centre_x - span * 0.5,
            yaw_span: span,
            height_top: centre_y + span / viewport.aspect_ratio() * 0.5,
            height_span: span / viewport.aspect_ratio(),
        };
        // Check every final output pixel, once per geometry change. This catches
        // small curved-boundary gaps that the coarse fitting grid could miss.
        for attempt in 0..8 {
            let valid = (0..=viewport.height).all(|y| {
                (0..=viewport.width).all(|x| {
                    let u = x as f32 / viewport.width as f32;
                    let v = y as f32 / viewport.height as f32;
                    coverage.covered(
                        bounds.yaw_min + u * bounds.yaw_span,
                        bounds.height_top - v * bounds.height_span,
                    ) && (x == viewport.width
                        || y == viewport.height
                        || coverage.covered(
                            bounds.yaw_min
                                + (x as f32 + 0.5) / viewport.width as f32 * bounds.yaw_span,
                            bounds.height_top
                                - (y as f32 + 0.5) / viewport.height as f32 * bounds.height_span,
                        ))
                })
            });
            if valid {
                return Ok(Self {
                    bounds,
                    view_to_plane: transforms,
                    plane_aspect: scene.plane_aspect,
                    plane_width: scene.plane_width,
                });
            }
            if attempt < 7 {
                bounds.yaw_span *= 0.99;
                bounds.height_span *= 0.99;
                bounds.yaw_min = centre_x - bounds.yaw_span * 0.5;
                bounds.height_top = centre_y + bounds.height_span * 0.5;
            }
        }
        Err("panorama coverage contains an uncovered gap; recalibrate the camera pair".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::calibration::PlaneLayout;
    use crate::detect::detector::CameraId;
    use crate::projection::{VirtualCamera, panorama_to_camera};
    use crate::render::viewport::OutputProjection;

    fn garage() -> MatchCalibration {
        let camera = CameraParams {
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
            left: camera.clone(),
            right: camera,
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
            blend_width: 0.05,
        }
    }

    fn scene(calibration: &MatchCalibration) -> SceneGeometry {
        SceneGeometry::from_layout_with_aspect(
            &calibration.layout,
            calibration.left.width as f32 / calibration.left.height as f32,
        )
    }

    fn viewport() -> ViewportConfig {
        ViewportConfig {
            projection: OutputProjection::Panorama,
            width: 320,
            height: 100,
            ..Default::default()
        }
    }

    fn reference(
        u: f32,
        v: f32,
        geometry: &PanoramaGeometry,
        viewport: &ViewportConfig,
        scene: &SceneGeometry,
        calibration: &MatchCalibration,
    ) -> [Option<(f64, f64)>; 2] {
        let inverse = view_matrix(
            &scene.camera_position,
            0.0,
            0.0,
            viewport.rig_tilt,
            viewport.rig_roll,
        )
        .try_inverse()
        .unwrap();
        let direction = inverse
            .transform_vector(&geometry.bounds.ray(u, v))
            .normalize();
        let position =
            VirtualCamera::new(&scene.camera_position).direction_to_yaw_pitch(&direction);
        [CameraId::Left, CameraId::Right].map(|camera| {
            panorama_to_camera(position.yaw, position.pitch, camera, calibration, scene)
                .map(|(u, v)| (f64::from(u), f64::from(v)))
        })
    }

    #[test]
    fn garage_panorama_covers_every_pixel_and_matches_reference_with_tilt_roll() {
        let calibration = garage();
        let scene = scene(&calibration);
        for (tilt, roll) in [(0.0, 0.0), (0.25, 0.12), (-0.2, -0.15)] {
            let viewport = ViewportConfig {
                rig_tilt: tilt,
                rig_roll: roll,
                ..viewport()
            };
            let geometry = PanoramaGeometry::fit(&scene, &calibration, &viewport).unwrap();
            let coverage = Coverage {
                transforms: geometry.view_to_plane,
                cameras: [&calibration.left, &calibration.right],
                aspect: scene.plane_aspect,
                width: scene.plane_width,
                correction: 1.0,
            };
            eprintln!(
                "garage tilt={tilt} roll={roll}: {} x {} degrees",
                geometry.bounds.horizontal_degrees(),
                geometry.bounds.vertical_degrees()
            );
            assert!(geometry.bounds.horizontal_degrees() > 120.0);
            assert!(geometry.bounds.horizontal_degrees() <= 180.0);
            for y in 0..viewport.height {
                for x in 0..viewport.width {
                    let u = (x as f32 + 0.5) / viewport.width as f32;
                    let v = (y as f32 + 0.5) / viewport.height as f32;
                    let expected = reference(u, v, &geometry, &viewport, &scene, &calibration);
                    assert!(
                        expected.iter().any(Option::is_some),
                        "uncovered pixel {x},{y}"
                    );
                    for (index, expected) in expected.into_iter().enumerate() {
                        let actual = coverage.source_uv(index, geometry.bounds.ray(u, v));
                        assert_eq!(actual.is_some(), expected.is_some());
                        if let (Some(a), Some(b)) = (actual, expected) {
                            assert!((a.0 - b.0).abs() < 2e-5 && (a.1 - b.1).abs() < 2e-5);
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn wide_rig_preserves_near_180_degrees_without_stretching() {
        let mut calibration = garage();
        calibration.layout = PlaneLayout {
            camera_axis_offset: 0.2,
            intersect: 0.0,
            x_ty: 0.0,
            x_rz: 0.0,
            z_rx: 0.0,
            x_rx: 0.0,
            z_rz: 0.0,
        };
        for camera in [&mut calibration.left, &mut calibration.right] {
            camera.fx = 1200.0;
            camera.fy = 1200.0;
            camera.cx = 2016.0;
            camera.cy = 1520.0;
            camera.d = [0.0; 4];
        }
        let viewport = viewport();
        let geometry =
            PanoramaGeometry::fit(&scene(&calibration), &calibration, &viewport).unwrap();
        eprintln!("wide rig: {} degrees", geometry.bounds.horizontal_degrees());
        assert!(geometry.bounds.horizontal_degrees() > 175.0);
        assert!(geometry.bounds.horizontal_degrees() <= 180.0);
        assert!(
            (geometry.bounds.yaw_span / geometry.bounds.height_span - viewport.aspect_ratio())
                .abs()
                < 1e-5
        );
    }

    #[test]
    fn output_aspect_fits_real_coverage_and_exceeds_the_old_crop() {
        let calibration = garage();
        let scene = scene(&calibration);
        for (width, height) in [(320, 180), (320, 135), (320, 100)] {
            let viewport = ViewportConfig {
                width,
                height,
                ..viewport()
            };
            let geometry = PanoramaGeometry::fit(&scene, &calibration, &viewport).unwrap();
            let old_horizontal =
                2.0 * (viewport.aspect_ratio() * (75.0_f32.to_radians() * 0.5).tan()).atan();
            eprintln!(
                "aspect {width}:{height}: panorama={} old-perspective={}",
                geometry.bounds.horizontal_degrees(),
                old_horizontal.to_degrees()
            );
            assert!(geometry.bounds.yaw_span > old_horizontal + 10.0_f32.to_radians());
        }
    }

    fn render(
        calibration: MatchCalibration,
        viewport: ViewportConfig,
        left: &[u8],
        right: &[u8],
    ) -> Vec<u8> {
        use crate::gpu::{GpuContext, rgba_readback::RgbaReadback};
        use crate::render::{
            pipeline::{BgraPlanes, StitchPipeline},
            renderer::InputFormat,
        };
        let gpu = GpuContext::new_blocking().unwrap();
        let mut readback = RgbaReadback::new(&gpu, viewport.width, viewport.height).unwrap();
        let pipeline = StitchPipeline::with_gpu(
            gpu,
            calibration,
            viewport,
            128,
            96,
            wgpu::TextureFormat::Rgba8Unorm,
            InputFormat::Bgra,
        )
        .unwrap();
        let cmd = pipeline
            .render_to_target_bgra(
                &BgraPlanes::from_rgba(left),
                &BgraPlanes::from_rgba(right),
                0.0,
                0.0,
            )
            .unwrap();
        readback
            .readback(pipeline.gpu(), pipeline.render_target(), cmd)
            .unwrap();
        readback
            .flush_pending(pipeline.gpu())
            .unwrap()
            .unwrap()
            .to_vec()
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_cylindrical_coordinates_match_cpu_and_keep_every_output_pixel() {
        let calibration = garage();
        let viewport = ViewportConfig {
            width: 192,
            height: 64,
            blend_width: 0.0,
            rig_tilt: 0.16,
            rig_roll: -0.1,
            ..viewport()
        };
        let scene = scene(&calibration);
        let geometry = PanoramaGeometry::fit(&scene, &calibration, &viewport).unwrap();
        let image: Vec<u8> = (0..96)
            .flat_map(|y| {
                (0..128).flat_map(move |x| {
                    [
                        32 + (x * 192 / 127) as u8,
                        32 + (y * 192 / 95) as u8,
                        128,
                        255,
                    ]
                })
            })
            .collect();
        let pixels = render(calibration.clone(), viewport.clone(), &image, &image);
        let mut checked = 0;
        for y in 0..viewport.height {
            for x in 0..viewport.width {
                let pixel = &pixels[((y * viewport.width + x) * 4) as usize..][..4];
                assert!(
                    pixel[2] >= 126 && pixel[2] <= 129 && pixel[3] == 255,
                    "missing pixel {x},{y}: {pixel:?}"
                );
                let refs = reference(
                    (x as f32 + 0.5) / viewport.width as f32,
                    (y as f32 + 0.5) / viewport.height as f32,
                    &geometry,
                    &viewport,
                    &scene,
                    &calibration,
                );
                if let [Some(uv), None] | [None, Some(uv)] = refs {
                    for (channel, coordinate, size) in [(0, uv.0, 128.0), (1, uv.1, 96.0)] {
                        let expected = 32.0
                            + ((coordinate * size - 0.5) / (size - 1.0)).clamp(0.0, 1.0) * 192.0;
                        assert!(
                            (f64::from(pixel[channel]) - expected).abs() < 3.0,
                            "coordinate mismatch {x},{y} channel={channel}: {} != {expected}",
                            pixel[channel]
                        );
                    }
                    checked += 1;
                }
            }
        }
        assert!(
            checked > 4000,
            "expected broad sole-camera coverage: {checked}"
        );
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_perspective_keeps_the_previous_camera_mapping() {
        let calibration = garage();
        let viewport = ViewportConfig {
            projection: OutputProjection::Perspective,
            width: 192,
            height: 108,
            blend_width: 0.0,
            rig_tilt: 0.1,
            rig_roll: 0.04,
            ..Default::default()
        };
        let scene = scene(&calibration);
        let image: Vec<u8> = (0..96)
            .flat_map(|y| {
                (0..128).flat_map(move |x| {
                    [
                        32 + (x * 192 / 127) as u8,
                        32 + (y * 192 / 95) as u8,
                        128,
                        255,
                    ]
                })
            })
            .collect();
        let pixels = render(calibration.clone(), viewport.clone(), &image, &image);
        let inverse = view_matrix(
            &scene.camera_position,
            0.0,
            0.0,
            viewport.rig_tilt,
            viewport.rig_roll,
        )
        .try_inverse()
        .unwrap();
        let camera = VirtualCamera::new(&scene.camera_position);
        let half_height = (viewport.fov_degrees.to_radians() * 0.5).tan();
        let mut checked = 0;
        for y in 2..viewport.height - 2 {
            for x in 2..viewport.width - 2 {
                let ray = Vector3::new(
                    (2.0 * (x as f32 + 0.5) / viewport.width as f32 - 1.0)
                        * half_height
                        * viewport.aspect_ratio(),
                    (1.0 - 2.0 * (y as f32 + 0.5) / viewport.height as f32) * half_height,
                    -1.0,
                );
                let pose =
                    camera.direction_to_yaw_pitch(&inverse.transform_vector(&ray).normalize());
                let refs = [CameraId::Left, CameraId::Right]
                    .map(|c| panorama_to_camera(pose.yaw, pose.pitch, c, &calibration, &scene));
                if let [Some(uv), None] | [None, Some(uv)] = refs {
                    let pixel = &pixels[((y * viewport.width + x) * 4) as usize..][..4];
                    for (channel, coordinate, size) in [(0, uv.0, 128.0), (1, uv.1, 96.0)] {
                        let expected = 32.0
                            + ((coordinate * size - 0.5) / (size - 1.0)).clamp(0.0, 1.0) * 192.0;
                        assert!(
                            (f32::from(pixel[channel]) - expected).abs() < 3.0,
                            "perspective mismatch {x},{y}: {pixel:?}"
                        );
                    }
                    checked += 1;
                }
            }
        }
        assert!(checked > 2000);
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_nv12_uses_each_camera_matrix_and_range_in_both_projections() {
        use crate::gpu::{GpuContext, rgba_readback::RgbaReadback};
        use crate::render::{
            pipeline::{Nv12Planes, StitchPipeline},
            renderer::{InputColorimetry, InputFormat, YuvMatrix},
        };
        let encode = |rgb: [u8; 3], matrix: YuvMatrix, full_range: bool| {
            let (kr, kb) = match matrix {
                YuvMatrix::Bt601 => (0.299, 0.114),
                YuvMatrix::Bt709 => (0.2126, 0.0722),
            };
            let [r, g, b] = rgb.map(|value| f64::from(value) / 255.0);
            let y = kr * r + (1.0 - kr - kb) * g + kb * b;
            let cb = (b - y) / (2.0 * (1.0 - kb));
            let cr = (r - y) / (2.0 * (1.0 - kr));
            let samples = if full_range {
                [y * 255.0, 128.0 + cb * 255.0, 128.0 + cr * 255.0]
            } else {
                [16.0 + y * 219.0, 128.0 + cb * 224.0, 128.0 + cr * 224.0]
            }
            .map(|v| v.round().clamp(0.0, 255.0) as u8);
            (
                vec![samples[0]; 128 * 96],
                [samples[1], samples[2]].repeat(128 * 96 / 4),
            )
        };
        for projection in [OutputProjection::Perspective, OutputProjection::Panorama] {
            let viewport = ViewportConfig {
                projection,
                width: 192,
                height: 64,
                ..Default::default()
            };
            let gpu = GpuContext::new_blocking().unwrap();
            let mut readback = RgbaReadback::new(&gpu, viewport.width, viewport.height).unwrap();
            let mut pipeline = StitchPipeline::with_gpu(
                gpu,
                garage(),
                viewport.clone(),
                128,
                96,
                wgpu::TextureFormat::Rgba8Unorm,
                InputFormat::Nv12,
            )
            .unwrap();
            let render = |pipeline: &StitchPipeline,
                          readback: &mut RgbaReadback,
                          left: &(Vec<u8>, Vec<u8>),
                          right: &(Vec<u8>, Vec<u8>)| {
                let cmd = pipeline
                    .render_to_target_nv12(
                        &Nv12Planes {
                            y: &left.0,
                            uv: &left.1,
                        },
                        &Nv12Planes {
                            y: &right.0,
                            uv: &right.1,
                        },
                        0.0,
                        0.0,
                    )
                    .unwrap();
                readback
                    .readback(pipeline.gpu(), pipeline.render_target(), cmd)
                    .unwrap();
                readback
                    .flush_pending(pipeline.gpu())
                    .unwrap()
                    .unwrap()
                    .to_vec()
            };
            // Existing callers still get BT.709: decoding BT.601 red with that
            // default produces a measurable green error, which metadata removes.
            let red_601 = encode([255, 0, 0], YuvMatrix::Bt601, false);
            let default_pixels = render(&pipeline, &mut readback, &red_601, &red_601);
            let centre = ((viewport.height / 2 * viewport.width + viewport.width / 2) * 4) as usize;
            assert!((21..=27).contains(&default_pixels[centre + 1]));
            assert!(default_pixels[centre] >= 252 && default_pixels[centre + 2] <= 3);
            for (left_full, right_full) in [(false, true), (true, false)] {
                pipeline.set_input_colorimetry(
                    InputColorimetry {
                        matrix: YuvMatrix::Bt601,
                        full_range: left_full,
                    },
                    InputColorimetry {
                        matrix: YuvMatrix::Bt709,
                        full_range: right_full,
                    },
                );
                for expected in [[255, 0, 0], [40, 170, 70], [20, 60, 200], [64, 128, 192]] {
                    let left = encode(expected, YuvMatrix::Bt601, left_full);
                    let right = encode(expected, YuvMatrix::Bt709, right_full);
                    let pixels = render(&pipeline, &mut readback, &left, &right);
                    let mut checked = 0;
                    for pixel in pixels.chunks_exact(4) {
                        if pixel[..3] == [0, 0, 0] {
                            continue;
                        }
                        for channel in 0..3 {
                            assert!(
                                pixel[channel].abs_diff(expected[channel]) <= 4,
                                "{projection:?}, ranges={left_full}/{right_full}: {pixel:?} != {expected:?}"
                            );
                        }
                        checked += 1;
                    }
                    assert!(checked > (viewport.width * viewport.height / 2) as usize);
                    if projection == OutputProjection::Panorama {
                        assert_eq!(checked, (viewport.width * viewport.height) as usize);
                    }
                }
            }
        }
    }
}
