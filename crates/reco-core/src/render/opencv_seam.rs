//! Bounded OpenCV seam analysis; full-resolution composition remains on the GPU.
use super::blend::{compute, group, view};
use crate::gpu::GpuContext;
use std::ffi::{CStr, c_char};
use std::path::Path;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::thread::JoinHandle;
use wgpu::util::DeviceExt;

unsafe extern "C" {
    fn reco_opencv_version() -> *const c_char;
    fn reco_opencv_graphcut(
        left: *const u8,
        right: *const u8,
        width: u32,
        height: u32,
        mask: *mut u8,
        error: *mut c_char,
        error_length: usize,
    ) -> i32;
}

pub(super) fn analysis_size(width: u32, height: u32) -> (u32, u32) {
    let scale = (768.0 / width as f64)
        .min((250_000.0 / (width as f64 * height as f64)).sqrt())
        .min(1.0);
    (
        (width as f64 * scale).floor().max(1.0) as u32,
        (height as f64 * scale).floor().max(1.0) as u32,
    )
}

struct Job {
    generation: u64,
    width: u32,
    height: u32,
    packed: Vec<u8>,
}
struct Outcome {
    generation: u64,
    result: Result<Vec<u8>, String>,
}
struct Mapping {
    generation: u64,
    result: Receiver<Result<(), wgpu::BufferAsyncError>>,
}

pub(super) struct Estimator {
    width: u32,
    height: u32,
    key: Option<u64>,
    generation: u64,
    frames: u32,
    attempts: u32,
    active: bool,
    processing: bool,
    mapping: Option<Mapping>,
    pipeline: wgpu::ComputePipeline,
    group: wgpu::BindGroup,
    packed: wgpu::Buffer,
    staging: wgpu::Buffer,
    jobs: Option<SyncSender<Job>>,
    results: Receiver<Outcome>,
    thread: Option<JoinHandle<()>>,
}

impl Estimator {
    pub(super) fn new(
        gpu: &GpuContext,
        left: &wgpu::Texture,
        right: &wgpu::Texture,
        right_warp: &wgpu::Texture,
        width: u32,
        height: u32,
    ) -> Self {
        let bytes = u64::from(width) * u64::from(height) * 12;
        let packed = gpu.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("opencv_seam_sample"),
            size: bytes,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
            mapped_at_creation: false,
        });
        let staging = gpu.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("opencv_seam_readback"),
            size: bytes,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let size = gpu
            .device
            .create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("opencv_seam_size"),
                contents: bytemuck::cast_slice(&[width, height, 0, 0]),
                usage: wgpu::BufferUsages::UNIFORM,
            });
        let pipeline = compute(
            &gpu.device,
            "opencv_seam_sample",
            include_str!("../shaders/seam_sample.wgsl"),
        );
        let group = group(
            &gpu.device,
            &pipeline.get_bind_group_layout(0),
            &[
                wgpu::BindingResource::TextureView(&view(left)),
                wgpu::BindingResource::TextureView(&view(right)),
                wgpu::BindingResource::TextureView(&view(right_warp)),
                packed.as_entire_binding(),
                size.as_entire_binding(),
            ],
        );
        let (job_tx, job_rx) = mpsc::sync_channel::<Job>(1);
        let (result_tx, result_rx) = mpsc::sync_channel(1);
        let thread = std::thread::Builder::new()
            .name("opencv-seam".into())
            .spawn(move || {
                while let Ok(job) = job_rx.recv() {
                    let generation = job.generation;
                    let result = solve(&job);
                    if result_tx.send(Outcome { generation, result }).is_err() {
                        break;
                    }
                }
            })
            .expect("spawn OpenCV seam worker");
        let version = unsafe { CStr::from_ptr(reco_opencv_version()) }.to_string_lossy();
        log::info!(
            "OpenCV {version} graph-cut seam enabled: bounded {width}x{height} analysis, COST_COLOR, stable seam per geometry"
        );
        Self {
            width,
            height,
            key: None,
            generation: 0,
            frames: 0,
            attempts: 0,
            active: false,
            processing: false,
            mapping: None,
            pipeline,
            group,
            packed,
            staging,
            jobs: Some(job_tx),
            results: result_rx,
            thread: Some(thread),
        }
    }

    #[cfg(test)]
    pub(super) fn activate_for_test(&mut self) {
        self.active = true;
    }

    pub(super) fn set_geometry(&mut self, key: u64) {
        if self.key != Some(key) {
            self.key = Some(key);
            self.generation = self.generation.wrapping_add(1);
            self.frames = 0;
            self.attempts = 0;
            self.active = false;
        }
    }

    pub(super) fn begin_frame(&mut self, gpu: &GpuContext, mask: &wgpu::Texture) -> bool {
        let _ = gpu.device.poll(wgpu::PollType::Poll);
        if let Some(mapping) = self.mapping.take() {
            match mapping.result.try_recv() {
                Ok(Ok(())) => {
                    let packed = self.staging.slice(..).get_mapped_range().to_vec();
                    self.staging.unmap();
                    if mapping.generation == self.generation {
                        let job = Job {
                            generation: mapping.generation,
                            width: self.width,
                            height: self.height,
                            packed,
                        };
                        if self
                            .jobs
                            .as_ref()
                            .is_some_and(|sender| sender.try_send(job).is_ok())
                        {
                            self.processing = true;
                        }
                    }
                }
                Ok(Err(error)) => {
                    self.staging.unmap();
                    log::warn!("OpenCV seam readback failed: {error}; retaining geometric seam");
                }
                Err(mpsc::TryRecvError::Empty) => self.mapping = Some(mapping),
                Err(mpsc::TryRecvError::Disconnected) => {
                    self.staging.unmap();
                    log::warn!("OpenCV seam readback canceled");
                }
            }
        }
        while let Ok(outcome) = self.results.try_recv() {
            self.processing = false;
            if outcome.generation != self.generation {
                continue;
            }
            match outcome.result {
                Ok(ownership) => {
                    gpu.queue.write_texture(
                        wgpu::TexelCopyTextureInfo {
                            texture: mask,
                            mip_level: 0,
                            origin: wgpu::Origin3d::ZERO,
                            aspect: wgpu::TextureAspect::All,
                        },
                        &ownership,
                        wgpu::TexelCopyBufferLayout {
                            offset: 0,
                            bytes_per_row: Some(self.width),
                            rows_per_image: Some(self.height),
                        },
                        wgpu::Extent3d {
                            width: self.width,
                            height: self.height,
                            depth_or_array_layers: 1,
                        },
                    );
                    self.active = true;
                    log::info!(
                        "OpenCV graph-cut seam applied: generation={} {}x{}; stable until geometry changes",
                        self.generation,
                        self.width,
                        self.height
                    );
                }
                Err(reason) => log::warn!(
                    "OpenCV graph-cut seam unavailable: {reason}; retaining geometric seam"
                ),
            }
        }
        self.frames = self.frames.saturating_add(1);
        self.active
    }

    pub(super) fn encode_sample(&mut self, encoder: &mut wgpu::CommandEncoder) {
        if self.active
            || self.processing
            || self.mapping.is_some()
            || self.attempts >= 3
            || self.frames < 30 * (self.attempts + 1)
        {
            return;
        }
        {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("opencv_seam_sample"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, &self.group, &[]);
            pass.dispatch_workgroups(self.width.div_ceil(8), self.height.div_ceil(8), 1);
        }
        encoder.copy_buffer_to_buffer(&self.packed, 0, &self.staging, 0, self.staging.size());
        let (tx, rx) = mpsc::channel();
        encoder.map_buffer_on_submit(&self.staging, wgpu::MapMode::Read, .., move |result| {
            let _ = tx.send(result);
        });
        self.mapping = Some(Mapping {
            generation: self.generation,
            result: rx,
        });
        self.attempts += 1;
    }
}

impl Drop for Estimator {
    fn drop(&mut self) {
        self.jobs.take();
        // Dropping the receiver lets a finishing worker leave without blocking on its result.
        let (_, empty) = mpsc::channel();
        let results = std::mem::replace(&mut self.results, empty);
        drop(results);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

fn solve(job: &Job) -> Result<Vec<u8>, String> {
    if job.width == 0
        || job.height == 0
        || job.width > 768
        || u64::from(job.width) * u64::from(job.height) > 250_000
    {
        return Err("invalid seam image dimensions".into());
    }
    let count = job.width as usize * job.height as usize;
    if job.packed.len() != count * 12 {
        return Err("invalid packed sample length".into());
    }
    let mut left = Vec::with_capacity(count * 4);
    let mut right = Vec::with_capacity(count * 4);
    let mut baseline = Vec::with_capacity(count);
    for pixel in job.packed.chunks_exact(12) {
        left.extend_from_slice(&pixel[..4]);
        right.extend_from_slice(&pixel[4..8]);
        baseline.push(pixel[8]);
    }
    let mut ownership = vec![0; count];
    let mut error = [0 as c_char; 512];
    let result = unsafe {
        reco_opencv_graphcut(
            left.as_ptr(),
            right.as_ptr(),
            job.width,
            job.height,
            ownership.as_mut_ptr(),
            error.as_mut_ptr(),
            error.len(),
        )
    };
    if result != 0 {
        return Err(unsafe { CStr::from_ptr(error.as_ptr()) }
            .to_string_lossy()
            .into_owned());
    }
    let before = seam_cost(&left, &right, &baseline, job.width, job.height);
    let after = seam_cost(&left, &right, &ownership, job.width, job.height);
    log::info!(
        "OpenCV graph-cut seam candidate: mismatch baseline={before:.3} selected={after:.3}"
    );
    if let Some(path) = std::env::var_os("RECO_OPENCV_SEAM_DIAGNOSTICS")
        && let Err(error) = diagnostics(Path::new(&path), job, &left, &right, &baseline, &ownership)
    {
        log::warn!("OpenCV seam diagnostics: {error}");
    }
    if !after.is_finite() || after > before * 1.05 + 1.0 {
        return Err(format!(
            "candidate mismatch {after:.3} exceeds baseline {before:.3}"
        ));
    }
    Ok(ownership)
}

fn seam_cost(left: &[u8], right: &[u8], mask: &[u8], width: u32, height: u32) -> f64 {
    let mut total = 0.0;
    let mut edges = 0;
    for y in 0..height as usize {
        for x in 0..width as usize {
            let p = y * width as usize + x;
            for q in [
                if x + 1 < width as usize {
                    Some(p + 1)
                } else {
                    None
                },
                if y + 1 < height as usize {
                    Some(p + width as usize)
                } else {
                    None
                },
            ]
            .into_iter()
            .flatten()
            {
                if (mask[p] >= 128) == (mask[q] >= 128) {
                    continue;
                }
                for i in [p, q] {
                    if left[i * 4 + 3] == 0 || right[i * 4 + 3] == 0 {
                        continue;
                    }
                    total += (0..3)
                        .map(|c| (left[i * 4 + c] as f64 - right[i * 4 + c] as f64).abs())
                        .sum::<f64>()
                        / 3.0;
                    edges += 1;
                }
            }
        }
    }
    if edges == 0 {
        f64::INFINITY
    } else {
        total / edges as f64
    }
}

fn diagnostics(
    path: &Path,
    job: &Job,
    left: &[u8],
    right: &[u8],
    baseline: &[u8],
    ownership: &[u8],
) -> std::io::Result<()> {
    if !path.is_absolute() || !path.is_dir() {
        return Err(std::io::Error::other(
            "diagnostic directory must already exist and be absolute",
        ));
    }
    let prefix = format!("seam-{}", job.generation);
    for (label, image) in [("left", left), ("right", right)] {
        let mut ppm = format!("P6\n{} {}\n255\n", job.width, job.height).into_bytes();
        for pixel in image.chunks_exact(4) {
            ppm.extend_from_slice(&pixel[..3]);
        }
        std::fs::write(path.join(format!("{prefix}-{label}.ppm")), ppm)?;
        let mut pgm = format!("P5\n{} {}\n255\n", job.width, job.height).into_bytes();
        pgm.extend(image.chunks_exact(4).map(|pixel| pixel[3]));
        std::fs::write(path.join(format!("{prefix}-{label}-valid.pgm")), pgm)?;
    }
    for (label, image) in [("baseline", baseline), ("selected", ownership)] {
        let mut pgm = format!("P5\n{} {}\n255\n", job.width, job.height).into_bytes();
        pgm.extend_from_slice(image);
        std::fs::write(path.join(format!("{prefix}-{label}.pgm")), pgm)?;
    }
    Ok(())
}

#[cfg(test)]
pub(super) fn solve_test(width: u32, height: u32, packed: Vec<u8>) -> Result<Vec<u8>, String> {
    solve(&Job {
        generation: 1,
        width,
        height,
        packed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn synthetic() -> Job {
        let (width, height) = (128, 64);
        let mut packed = Vec::new();
        for y in 0..height {
            for x in 0..width {
                let color = if (60..70).contains(&x) && (12..52).contains(&y) {
                    [210, 40, 30]
                } else {
                    [80, 90, 100]
                };
                packed.extend_from_slice(&[80, 90, 100, if x < 96 { 255 } else { 0 }]);
                packed.extend_from_slice(&[
                    color[0],
                    color[1],
                    color[2],
                    if x >= 32 { 255 } else { 0 },
                ]);
                packed.extend_from_slice(
                    &u32::from(x >= 64)
                        .to_le_bytes()
                        .map(|v| if v == 1 { 255 } else { v }),
                );
            }
        }
        Job {
            generation: 1,
            width,
            height,
            packed,
        }
    }

    #[test]
    fn opencv_graphcut_avoids_conflicting_object_and_preserves_coverage() {
        let job = synthetic();
        let mask = solve(&job).unwrap();
        let mut left = Vec::new();
        let mut right = Vec::new();
        let mut baseline = Vec::new();
        for p in job.packed.chunks_exact(12) {
            left.extend_from_slice(&p[..4]);
            right.extend_from_slice(&p[4..8]);
            baseline.push(p[8]);
        }
        let before = seam_cost(&left, &right, &baseline, job.width, job.height);
        let after = seam_cost(&left, &right, &mask, job.width, job.height);
        assert!(
            before > 20.0 && after < before * 0.1,
            "before={before} after={after}"
        );
        for (i, &value) in mask.iter().enumerate() {
            if i % 128 < 32 {
                assert_eq!(value, 0);
            }
            if i % 128 >= 96 {
                assert_eq!(value, 255);
            }
        }
    }

    #[test]
    fn opencv_color_cost_avoids_parallax_edges_in_textured_overlap() {
        let (width, height) = (128_u32, 64_u32);
        let mut left = vec![[0_u8; 4]; (width * height) as usize];
        let mut right = left.clone();
        for y in 0..height {
            for x in 0..width {
                let p = (y * width + x) as usize;
                let value = ((x + y) % 64 + 80) as u8;
                left[p] = [value, value, value, if x < 96 { 255 } else { 0 }];
                right[p] = [value, value, value, if x >= 32 { 255 } else { 0 }];
            }
        }
        // Gradient-normalized costs can prefer a strong, mismatched object edge.
        for (x, y, w, h, color, shift) in [
            (75, 44, 15, 9, [34, 64, 32], 3),
            (74, 21, 18, 6, [99, 185, 190], -3),
            (76, 22, 18, 20, [157, 251, 108], -1),
            (76, 41, 5, 20, [179, 147, 75], -3),
            (44, 30, 18, 10, [115, 225, 0], -3),
            (65, 40, 18, 27, [246, 114, 15], -3),
        ] {
            for row in y..=(y + h).min(63) {
                for col in x..=(x + w).min(95) {
                    left[(row * 128 + col) as usize][..3].copy_from_slice(&color);
                }
                for col in (x + shift)..=(x + w).min(95) {
                    right[(row * 128 + col) as usize][..3].copy_from_slice(&color);
                }
            }
        }
        let mut packed = Vec::new();
        let mut baseline = Vec::new();
        for (i, (a, b)) in left.iter().zip(&right).enumerate() {
            packed.extend_from_slice(a);
            packed.extend_from_slice(b);
            let value = if i % 128 >= 64 { 255_u32 } else { 0 };
            packed.extend_from_slice(&value.to_le_bytes());
            baseline.push(value as u8);
        }
        let mask = solve(&Job {
            generation: 1,
            width,
            height,
            packed,
        })
        .unwrap();
        let a: Vec<u8> = left.into_iter().flatten().collect();
        let b: Vec<u8> = right.into_iter().flatten().collect();
        let before = seam_cost(&a, &b, &baseline, width, height);
        let after = seam_cost(&a, &b, &mask, width, height);
        assert!(
            before > 20.0 && after < 1.0,
            "baseline={before} selected={after}"
        );
    }

    #[test]
    fn opencv_invalid_input_and_empty_overlap_fail_safely() {
        let mut job = synthetic();
        job.width = 0;
        assert!(solve(&job).is_err());
        let mut job = synthetic();
        job.packed.pop();
        assert!(solve(&job).is_err());
        let mut job = synthetic();
        for (i, pixel) in job.packed.chunks_exact_mut(12).enumerate() {
            pixel[3] = if i % 128 < 64 { 255 } else { 0 };
            pixel[7] = if i % 128 >= 64 { 255 } else { 0 };
        }
        assert!(solve(&job).unwrap_err().contains("insufficient overlap"));
    }

    #[test]
    fn opencv_analysis_dimensions_are_bounded() {
        for (w, h) in [
            (4032, 3040),
            (3072, 864),
            (16384, 16384),
            (1, 16384),
            (128, 32),
        ] {
            let (a, b) = analysis_size(w, h);
            assert!(a > 0 && b > 0 && a <= 768 && u64::from(a) * u64::from(b) <= 250_000);
        }
    }

    #[test]
    #[ignore = "requires GPU and OpenCV"]
    fn gpu_opencv_generation_rejects_stale_results_and_keeps_stable_mask() {
        let gpu = GpuContext::new_blocking().unwrap();
        let texture = |label| {
            gpu.device.create_texture(&wgpu::TextureDescriptor {
                label: Some(label),
                size: wgpu::Extent3d {
                    width: 128,
                    height: 64,
                    depth_or_array_layers: 1,
                },
                mip_level_count: 1,
                sample_count: 1,
                dimension: wgpu::TextureDimension::D2,
                format: wgpu::TextureFormat::Rgba16Float,
                usage: wgpu::TextureUsages::TEXTURE_BINDING,
                view_formats: &[],
            })
        };
        let a = texture("test_left");
        let b = texture("test_right");
        let warp = texture("test_warp");
        let mask = gpu.device.create_texture(&wgpu::TextureDescriptor {
            label: Some("test_mask"),
            size: wgpu::Extent3d {
                width: 128,
                height: 64,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::R8Unorm,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        let mut estimator = Estimator::new(&gpu, &a, &b, &warp, 128, 64);
        estimator.set_geometry(1);
        let old = estimator.generation;
        estimator.active = true;
        estimator.set_geometry(1);
        assert!(estimator.active);
        assert_eq!(old, estimator.generation);
        estimator.set_geometry(2);
        assert!(!estimator.active);
        assert_ne!(old, estimator.generation);
        let (tx, rx) = mpsc::sync_channel(1);
        estimator.results = rx;
        tx.send(Outcome {
            generation: old,
            result: Ok(vec![255; 128 * 64]),
        })
        .unwrap();
        assert!(!estimator.begin_frame(&gpu, &mask));
        tx.send(Outcome {
            generation: estimator.generation,
            result: Ok(vec![255; 128 * 64]),
        })
        .unwrap();
        assert!(estimator.begin_frame(&gpu, &mask));
        estimator.set_geometry(2);
        assert!(estimator.active);
    }
}
