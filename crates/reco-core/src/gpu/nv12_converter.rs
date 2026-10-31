//! GPU RGBA→NV12 format converter.
//!
//! Converts the RGBA render target to NV12 on the GPU using a compute shader,
//! then reads back the smaller NV12 buffer to the CPU. This eliminates
//! CPU-side swscale (which was the main encode bottleneck) and reduces
//! GPU→CPU readback bandwidth by 2.7x (NV12 is 1.5 bpp vs RGBA 4 bpp).
//!
//! ## Why not convert in the fragment shader?
//!
//! The render target must remain RGBA for post-stitch plugins (overlays,
//! color grading). The NV12 conversion happens *after* plugins, at the
//! encode boundary - invisible to plugin authors.
//!
//! ## Triple-buffered readback
//!
//! Uses three staging buffers to overlap GPU copies and CPU readback. The CPU
//! reads back from 2 frames ago. Mapping is scheduled with each copy,
//! allowing it to complete before the CPU consumes that slot. A delayed
//! slot waits only for its own submission, with a bounded timeout.
//!
//! ## Performance
//!
//! On an RTX 5070 at 1080p, this replaces:
//! - CPU swscale RGBA→NV12: ~2.5ms
//! - GPU readback of RGBA (8.3 MB): ~0.78ms
//!
//! With:
//! - GPU compute shader: ~0.1ms
//! - GPU readback of NV12 (3.1 MB): ~0.29ms

use super::GpuContext;
use bytemuck::{Pod, Zeroable};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use wgpu::util::DeviceExt;

struct ReadbackSlot {
    tx: mpsc::SyncSender<Result<(), wgpu::BufferAsyncError>>,
    rx: mpsc::Receiver<Result<(), wgpu::BufferAsyncError>>,
    submission: Option<wgpu::SubmissionIndex>,
}

/// Uniform parameters for the RGBA→NV12 compute shader.
#[repr(C)]
#[derive(Copy, Clone, Debug, Pod, Zeroable)]
struct Nv12Params {
    width: u32,
    height: u32,
}

/// GPU-accelerated RGBA → NV12 converter with triple-buffered readback.
///
/// Created once per pipeline alongside the internal renderer.
/// Call [`convert_and_readback`](Self::convert_and_readback) after rendering
/// each frame to get NV12 data ready for the encoder.
///
/// Uses three staging buffers so copying and mapping can overlap with
/// subsequent frames. A late readback waits for that slot's submission,
/// rather than all queued rendering work.
///
/// The write slot cycles 0 -> 1 -> 2 -> 0. The read slot is always
/// `(write_slot + 1) % 3`, which was written 2 frames ago.
///
/// Note: returns `None` on the first two calls (2-frame latency). Call
/// [`flush_pending`](Self::flush_pending) after the frame loop to get the
/// last two frames.
pub struct Nv12Converter {
    pipeline: wgpu::ComputePipeline,
    bind_group_layout: wgpu::BindGroupLayout,
    params_buffer: wgpu::Buffer,
    nv12_gpu_buffer: wgpu::Buffer,
    /// Triple-buffered staging buffers for pipelined readback.
    nv12_staging_buffers: [wgpu::Buffer; 3],
    /// Which staging buffer to write to next (0, 1, or 2).
    current_slot: usize,
    /// Number of frames pending readback (0, 1, or 2).
    pending_count: u8,
    /// Cached bind group for the current render target. Avoids per-frame
    /// descriptor pool allocation which causes OOM on Vulkan (wgpu#7525).
    ///
    /// Stores a raw pointer to the texture for identity comparison (never
    /// dereferenced). Stale addresses are not a risk here because wgpu
    /// textures are `Arc`-wrapped internally - the pointer remains stable
    /// for the texture's lifetime. The caller always passes the same
    /// render target reference, and if a new texture is created (e.g.,
    /// on resize), its address will differ, correctly invalidating the cache.
    cached_bind_group: Option<(*const wgpu::Texture, wgpu::BindGroup)>,
    /// Triple-buffered readback buffers (avoids 3 MB allocation per frame at 1080p).
    readback_buffers: [Vec<u8>; 3],
    readback_slots: [ReadbackSlot; 3],
    readback_count: u64,
    wait_count: u64,
    wait_time: Duration,
    copy_time: Duration,
    width: u32,
    height: u32,
    /// Dispatch dimensions for the compute shader.
    dispatch_x: u32,
    dispatch_y: u32,
}

impl Nv12Converter {
    /// Create a new NV12 converter for the given output dimensions.
    ///
    /// Returns an error if `width` is not divisible by 4 or `height` is not even.
    pub fn new(gpu: &GpuContext, width: u32, height: u32) -> Result<Self, Nv12Error> {
        if !width.is_multiple_of(4) {
            return Err(Nv12Error::InvalidDimensions(format!(
                "width must be divisible by 4, got {width}"
            )));
        }
        if !height.is_multiple_of(2) {
            return Err(Nv12Error::InvalidDimensions(format!(
                "height must be even, got {height}"
            )));
        }

        let device = &gpu.device;

        // Compile compute shader
        let shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("rgba_to_nv12"),
            source: wgpu::ShaderSource::Wgsl(include_str!("../shaders/rgba_to_nv12.wgsl").into()),
        });

        // Bind group layout: input texture + output buffer + params
        let bind_group_layout = device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
            label: Some("nv12_bind_group_layout"),
            entries: &[
                // @binding(0): input RGBA texture
                wgpu::BindGroupLayoutEntry {
                    binding: 0,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: wgpu::BindingType::Texture {
                        sample_type: wgpu::TextureSampleType::Float { filterable: false },
                        view_dimension: wgpu::TextureViewDimension::D2,
                        multisampled: false,
                    },
                    count: None,
                },
                // @binding(1): output NV12 storage buffer
                wgpu::BindGroupLayoutEntry {
                    binding: 1,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Storage { read_only: false },
                        has_dynamic_offset: false,
                        min_binding_size: None,
                    },
                    count: None,
                },
                // @binding(2): params uniform (width, height)
                wgpu::BindGroupLayoutEntry {
                    binding: 2,
                    visibility: wgpu::ShaderStages::COMPUTE,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Uniform,
                        has_dynamic_offset: false,
                        min_binding_size: None,
                    },
                    count: None,
                },
            ],
        });

        let pipeline_layout = device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
            label: Some("nv12_pipeline_layout"),
            bind_group_layouts: &[&bind_group_layout],
            immediate_size: 0,
        });

        let pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("nv12_compute_pipeline"),
            layout: Some(&pipeline_layout),
            module: &shader,
            entry_point: Some("main"),
            compilation_options: Default::default(),
            cache: None,
        });

        // NV12 buffer size: Y plane (w*h) + UV plane (w*h/2) = w*h*3/2 bytes
        let nv12_bytes = width as u64 * height as u64 * 3 / 2;
        // GPU storage buffer for compute shader output
        let nv12_gpu_buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("nv12_gpu"),
            size: nv12_bytes,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC,
            mapped_at_creation: false,
        });

        // Triple-buffered CPU-readable staging buffers for pipelined readback.
        // The third buffer lets copying and mapping overlap later frames.
        let nv12_staging_buffers = [
            device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("nv12_staging_0"),
                size: nv12_bytes,
                usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
                mapped_at_creation: false,
            }),
            device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("nv12_staging_1"),
                size: nv12_bytes,
                usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
                mapped_at_creation: false,
            }),
            device.create_buffer(&wgpu::BufferDescriptor {
                label: Some("nv12_staging_2"),
                size: nv12_bytes,
                usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
                mapped_at_creation: false,
            }),
        ];

        // Uniform buffer with width/height
        let params = Nv12Params { width, height };
        let params_buffer = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("nv12_params"),
            contents: bytemuck::bytes_of(&params),
            usage: wgpu::BufferUsages::UNIFORM,
        });

        // Dispatch: one thread per 4 horizontal pixels, one thread per row
        // Workgroup size is (16, 4), so dispatch groups are:
        let dispatch_x = (width / 4).div_ceil(16);
        let dispatch_y = height.div_ceil(4);

        log::debug!(
            "NV12 converter: {}x{} → {} bytes ({:.1} MB), dispatch ({}, {}, 1)",
            width,
            height,
            nv12_bytes,
            nv12_bytes as f64 / 1_048_576.0,
            dispatch_x,
            dispatch_y,
        );

        Ok(Self {
            pipeline,
            bind_group_layout,
            params_buffer,
            nv12_gpu_buffer,
            nv12_staging_buffers,
            current_slot: 0,
            pending_count: 0,
            cached_bind_group: None,
            readback_buffers: [
                vec![0u8; nv12_bytes as usize],
                vec![0u8; nv12_bytes as usize],
                vec![0u8; nv12_bytes as usize],
            ],
            readback_slots: std::array::from_fn(|_| {
                let (tx, rx) = mpsc::sync_channel(1);
                ReadbackSlot {
                    tx,
                    rx,
                    submission: None,
                }
            }),
            readback_count: 0,
            wait_count: 0,
            wait_time: Duration::ZERO,
            copy_time: Duration::ZERO,
            width,
            height,
            dispatch_x,
            dispatch_y,
        })
    }

    /// Convert the RGBA render target to NV12 and read back to CPU.
    ///
    /// Uses triple-buffered staging: this frame's GPU work writes to the
    /// current slot, while the CPU reads back from 2 frames ago. A delayed
    /// slot waits only for its own submission.
    ///
    /// `render_commands` is the command buffer from the preceding render pass.
    /// It is submitted together with the compute shader in a single
    /// `queue.submit` call to guarantee correct GPU synchronization.
    ///
    /// Returns `None` on the first two calls (GPU work is submitted but no
    /// frame from 2 frames ago is available yet). From the third call onward,
    /// returns NV12 data from 2 frames ago as a borrowed slice:
    /// `[Y plane: width*height bytes] [UV plane: width*height/2 bytes]`
    ///
    /// Call [`flush_pending`](Self::flush_pending) after the frame loop
    /// to get the last frames' data.
    #[cfg_attr(
        feature = "profiling",
        tracing::instrument(skip_all, name = "nv12_convert_readback")
    )]
    pub fn convert_and_readback(
        &mut self,
        gpu: &GpuContext,
        render_target: &wgpu::Texture,
        render_commands: wgpu::CommandBuffer,
    ) -> Result<Option<&[u8]>, Nv12Error> {
        let write_slot = self.current_slot;

        // --- Submit this frame's GPU work FIRST ---
        // By submitting before readback, the GPU starts working on the
        // current frame while the CPU reads back data from 2 frames ago.
        self.submit_gpu_work(gpu, render_target, render_commands, write_slot)?;

        // --- Read back from 2 frames ago (if available) ---
        // read_slot = (write_slot + 1) % 3 is the oldest pending slot.
        // When write_slot cycles 0->1->2->0:
        //   write 0, read 1 (was written 2 frames ago)
        //   write 1, read 2 (was written 2 frames ago)
        //   write 2, read 0 (was written 2 frames ago)
        let has_result = if self.pending_count >= 2 {
            let read_slot = (write_slot + 1) % 3;
            self.readback_staging(gpu, read_slot)?;
            true
        } else {
            false
        };

        self.pending_count = (self.pending_count + 1).min(2);
        self.current_slot = (write_slot + 1) % 3;

        if has_result {
            let read_slot = (write_slot + 1) % 3;
            Ok(Some(&self.readback_buffers[read_slot]))
        } else {
            Ok(None)
        }
    }

    /// Flush one pending frame from the triple-buffer pipeline.
    ///
    /// Call this in a loop after the frame loop ends to drain remaining
    /// frames. Returns `None` when no frames are pending.
    ///
    /// Waits for the slot's submission when its mapping is still pending.
    #[cfg_attr(
        feature = "profiling",
        tracing::instrument(skip_all, name = "nv12_flush")
    )]
    pub fn flush_pending(&mut self, gpu: &GpuContext) -> Result<Option<&[u8]>, Nv12Error> {
        if self.pending_count == 0 {
            return Ok(None);
        }
        // Oldest pending slot: current_slot was already advanced, so walk
        // back by pending_count to find the oldest unread buffer.
        let read_slot = (self.current_slot + 3 - self.pending_count as usize) % 3;
        self.readback_staging(gpu, read_slot)?;
        self.pending_count -= 1;
        Ok(Some(&self.readback_buffers[read_slot]))
    }

    /// Submit GPU render + NV12 compute + copy to a specific staging slot.
    fn submit_gpu_work(
        &mut self,
        gpu: &GpuContext,
        render_target: &wgpu::Texture,
        render_commands: wgpu::CommandBuffer,
        slot: usize,
    ) -> Result<(), Nv12Error> {
        // Cache the bind group to avoid per-frame descriptor pool allocation,
        // which causes OOM on the Vulkan backend (wgpu#7525). Rebuild only
        // if the render target texture changes.
        let texture_ptr: *const wgpu::Texture = render_target;
        let needs_rebuild = self
            .cached_bind_group
            .as_ref()
            .is_none_or(|(ptr, _)| *ptr != texture_ptr);

        if needs_rebuild {
            let render_target_view =
                render_target.create_view(&wgpu::TextureViewDescriptor::default());
            let bind_group = gpu.device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some("nv12_bind_group"),
                layout: &self.bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&render_target_view),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: self.nv12_gpu_buffer.as_entire_binding(),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: self.params_buffer.as_entire_binding(),
                    },
                ],
            });
            self.cached_bind_group = Some((texture_ptr, bind_group));
        }

        let (_, bind_group) = self
            .cached_bind_group
            .as_ref()
            .expect("bind group was just built above");

        let mut encoder = gpu
            .device
            .create_command_encoder(&wgpu::CommandEncoderDescriptor {
                label: Some("nv12_encoder"),
            });

        // Dispatch compute shader
        {
            crate::profile_scope!("nv12_compute");
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("nv12_pass"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.pipeline);
            pass.set_bind_group(0, bind_group, &[]);
            pass.dispatch_workgroups(self.dispatch_x, self.dispatch_y, 1);
        }

        // Copy GPU buffer to the target staging buffer
        encoder.copy_buffer_to_buffer(
            &self.nv12_gpu_buffer,
            0,
            &self.nv12_staging_buffers[slot],
            0,
            self.nv12_gpu_buffer.size(),
        );

        let tx = self.readback_slots[slot].tx.clone();
        encoder.map_buffer_on_submit(
            &self.nv12_staging_buffers[slot],
            wgpu::MapMode::Read,
            ..,
            move |result| {
                let _ = tx.send(result);
            },
        );

        {
            crate::profile_scope!("nv12_submit");
            self.readback_slots[slot].submission =
                Some(gpu.queue.submit([render_commands, encoder.finish()]));
        }

        Ok(())
    }

    fn readback_staging(&mut self, gpu: &GpuContext, slot: usize) -> Result<(), Nv12Error> {
        crate::profile_scope!("nv12_readback");
        gpu.device
            .poll(wgpu::PollType::Poll)
            .map_err(|_| Nv12Error::BufferMapFailed)?;
        let result = match self.readback_slots[slot].rx.try_recv() {
            Ok(result) => result,
            Err(mpsc::TryRecvError::Empty) => {
                self.wait_count += 1;
                let start = Instant::now();
                let submission = self.readback_slots[slot]
                    .submission
                    .clone()
                    .ok_or(Nv12Error::BufferMapFailed)?;
                gpu.device
                    .poll(wgpu::PollType::Wait {
                        submission_index: Some(submission),
                        timeout: Some(Duration::from_secs(5)),
                    })
                    .map_err(|_| Nv12Error::BufferMapFailed)?;
                let result = self.readback_slots[slot]
                    .rx
                    .recv_timeout(Duration::from_secs(1))
                    .map_err(|_| Nv12Error::BufferMapFailed)?;
                self.wait_time += start.elapsed();
                result
            }
            Err(mpsc::TryRecvError::Disconnected) => return Err(Nv12Error::BufferMapFailed),
        };
        result.map_err(|_| Nv12Error::BufferMapFailed)?;
        let start = Instant::now();
        let mapped = self.nv12_staging_buffers[slot].slice(..).get_mapped_range();
        self.readback_buffers[slot].copy_from_slice(&mapped);
        drop(mapped);
        self.nv12_staging_buffers[slot].unmap();
        self.readback_slots[slot].submission = None;
        self.copy_time += start.elapsed();
        self.readback_count += 1;
        Ok(())
    }

    pub(crate) fn log_readback_stats(&self) {
        if self.readback_count > 0 {
            log::info!(
                "NV12 readback: {} frames, {} bounded waits ({:.2}ms total), avg CPU copy {:.3}ms",
                self.readback_count,
                self.wait_count,
                self.wait_time.as_secs_f64() * 1000.0,
                self.copy_time.as_secs_f64() * 1000.0 / self.readback_count as f64,
            );
        }
    }

    /// Output width in pixels.
    pub fn width(&self) -> u32 {
        self.width
    }

    /// Output height in pixels.
    pub fn height(&self) -> u32 {
        self.height
    }
}

/// Errors from the NV12 converter.
#[derive(Debug, Clone, thiserror::Error)]
pub enum Nv12Error {
    /// GPU buffer mapping failed.
    #[error("NV12 buffer mapping failed")]
    BufferMapFailed,

    /// Invalid output dimensions.
    #[error("invalid NV12 dimensions: {0}")]
    InvalidDimensions(String),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_nv12_readback_preserves_ring_order_and_short_session_flush() {
        let gpu = GpuContext::new_blocking().unwrap();
        let (width, height) = (68, 6);
        let target = gpu.device.create_texture(&wgpu::TextureDescriptor {
            label: Some("nv12_ring_test"),
            size: wgpu::Extent3d {
                width,
                height,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::Rgba8Unorm,
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT | wgpu::TextureUsages::TEXTURE_BINDING,
            view_formats: &[],
        });
        let view = target.create_view(&Default::default());
        let mut converter = Nv12Converter::new(&gpu, width, height).unwrap();
        let y_size = (width * height) as usize;
        let mut value = 10_u8;
        for frames in [1, 2, 8, 1] {
            let mut expected = std::collections::VecDeque::new();
            let mut actual = Vec::new();
            for _ in 0..frames {
                let gray = f64::from(value) / 255.0;
                expected.push_back((16.0 + 219.0 * gray).round() as u8);
                let mut commands = gpu.device.create_command_encoder(&Default::default());
                {
                    let _pass = commands.begin_render_pass(&wgpu::RenderPassDescriptor {
                        color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                            view: &view,
                            resolve_target: None,
                            depth_slice: None,
                            ops: wgpu::Operations {
                                load: wgpu::LoadOp::Clear(wgpu::Color {
                                    r: gray,
                                    g: gray,
                                    b: gray,
                                    a: 1.0,
                                }),
                                store: wgpu::StoreOp::Store,
                            },
                        })],
                        ..Default::default()
                    });
                }
                if let Some(data) = converter
                    .convert_and_readback(&gpu, &target, commands.finish())
                    .unwrap()
                {
                    actual.push(data.to_vec());
                }
                value += 13;
            }
            while let Some(data) = converter.flush_pending(&gpu).unwrap() {
                actual.push(data.to_vec());
            }
            assert!(converter.flush_pending(&gpu).unwrap().is_none());
            assert_eq!(actual.len(), frames);
            for data in actual {
                let y = expected.pop_front().unwrap();
                assert!(data[..y_size].iter().all(|v| v.abs_diff(y) <= 1));
                assert!(data[y_size..].iter().all(|v| v.abs_diff(128) <= 1));
            }
            assert!(expected.is_empty());
            assert!(
                converter
                    .readback_slots
                    .iter()
                    .all(|slot| slot.submission.is_none())
            );
        }
        assert_eq!(converter.readback_count, 12);
    }
}
