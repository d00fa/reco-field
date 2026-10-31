//! Coverage-normalized multiband stitching and temporally stable overlap matching.

#[cfg(feature = "opencv-seam")]
use super::opencv_seam::Estimator;
use crate::gpu::GpuContext;
use std::sync::Mutex;
use wgpu::util::DeviceExt;

const FORMAT: wgpu::TextureFormat = wgpu::TextureFormat::Rgba16Float;

struct Level {
    left: wgpu::Texture,
    right: wgpu::Texture,
    mask: wgpu::Texture,
    blended: wgpu::Texture,
}

#[derive(Default)]
struct MaskCache {
    geometry: Option<u64>,
    prepared: Option<(u64, u32, bool)>,
}

impl MaskCache {
    fn prepare(&mut self, blend_width: f32, seam_active: bool) -> bool {
        let Some(geometry) = self.geometry else {
            return true;
        };
        let key = (geometry, blend_width.to_bits(), seam_active);
        if self.prepared == Some(key) {
            return false;
        }
        self.prepared = Some(key);
        true
    }
}

/// Reused GPU resources with optional reduced seam samples, without per-frame texture allocation.
pub(super) struct SeamBlender {
    #[cfg(test)]
    left_warp: wgpu::Texture,
    #[cfg(test)]
    right_warp: wgpu::Texture,
    warp_views: [wgpu::TextureView; 2],
    mask_cache: Mutex<MaskCache>,
    levels: Vec<Level>,
    match_pipeline: wgpu::ComputePipeline,
    match_group: wgpu::BindGroup,
    prepare_pipeline: wgpu::ComputePipeline,
    prepare_group: wgpu::BindGroup,
    down_pipeline: wgpu::ComputePipeline,
    down_groups: Vec<[wgpu::BindGroup; 3]>,
    reconstruct_pipeline: wgpu::ComputePipeline,
    reconstruct_groups: Vec<wgpu::BindGroup>,
    output_pipeline: wgpu::RenderPipeline,
    output_group: wgpu::BindGroup,
    options: wgpu::Buffer,
    #[cfg(feature = "opencv-seam")]
    chosen_mask: wgpu::Texture,
    #[cfg(feature = "opencv-seam")]
    estimator: Mutex<Estimator>,
}

fn texture(device: &wgpu::Device, width: u32, height: u32, label: &str) -> wgpu::Texture {
    device.create_texture(&wgpu::TextureDescriptor {
        label: Some(label),
        size: wgpu::Extent3d {
            width,
            height,
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: FORMAT,
        usage: wgpu::TextureUsages::RENDER_ATTACHMENT
            | wgpu::TextureUsages::TEXTURE_BINDING
            | wgpu::TextureUsages::STORAGE_BINDING
            | wgpu::TextureUsages::COPY_DST,
        view_formats: &[],
    })
}

pub(super) fn view(texture: &wgpu::Texture) -> wgpu::TextureView {
    texture.create_view(&wgpu::TextureViewDescriptor::default())
}

pub(super) fn compute(device: &wgpu::Device, label: &str, source: &str) -> wgpu::ComputePipeline {
    let module = device.create_shader_module(wgpu::ShaderModuleDescriptor {
        label: Some(label),
        source: wgpu::ShaderSource::Wgsl(source.into()),
    });
    device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
        label: Some(label),
        layout: None,
        module: &module,
        entry_point: Some("main"),
        compilation_options: Default::default(),
        cache: None,
    })
}

pub(super) fn group(
    device: &wgpu::Device,
    layout: &wgpu::BindGroupLayout,
    resources: &[wgpu::BindingResource<'_>],
) -> wgpu::BindGroup {
    let entries: Vec<_> = resources
        .iter()
        .enumerate()
        .map(|(binding, resource)| wgpu::BindGroupEntry {
            binding: binding as u32,
            resource: resource.clone(),
        })
        .collect();
    device.create_bind_group(&wgpu::BindGroupDescriptor {
        label: Some("seam_resources"),
        layout,
        entries: &entries,
    })
}

impl SeamBlender {
    pub(super) fn new(
        gpu: &GpuContext,
        width: u32,
        height: u32,
        output_format: wgpu::TextureFormat,
    ) -> Self {
        let device = &gpu.device;
        let left_warp = texture(device, width, height, "seam_left_warp");
        let right_warp = texture(device, width, height, "seam_right_warp");
        let warp_views = [view(&left_warp), view(&right_warp)];
        let mut levels = Vec::new();
        let (mut w, mut h) = (width, height);
        loop {
            levels.push(Level {
                left: texture(device, w, h, "seam_left_pyramid"),
                right: texture(device, w, h, "seam_right_pyramid"),
                mask: texture(device, w, h, "seam_mask_pyramid"),
                blended: texture(device, w, h, "seam_reconstruction"),
            });
            if levels.len() == 6 || w == 1 || h == 1 {
                break;
            }
            (w, h) = (w.div_ceil(2), h.div_ceil(2));
        }
        let match_pipeline = compute(
            device,
            "seam_match",
            include_str!("../shaders/seam_match.wgsl"),
        );
        let prepare_pipeline = compute(
            device,
            "seam_prepare",
            include_str!("../shaders/seam_prepare.wgsl"),
        );
        let down_pipeline = compute(
            device,
            "seam_downsample",
            include_str!("../shaders/seam_downsample.wgsl"),
        );
        let reconstruct_pipeline = compute(
            device,
            "seam_reconstruct",
            include_str!("../shaders/seam_reconstruct.wgsl"),
        );
        let state = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("seam_color_state"),
            contents: bytemuck::cast_slice(&[1.0_f32, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0]),
            usage: wgpu::BufferUsages::STORAGE,
        });
        let match_group = group(
            device,
            &match_pipeline.get_bind_group_layout(0),
            &[
                wgpu::BindingResource::TextureView(&view(&left_warp)),
                wgpu::BindingResource::TextureView(&view(&right_warp)),
                state.as_entire_binding(),
            ],
        );
        let options = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("seam_options"),
            contents: bytemuck::cast_slice(&[0.15_f32, 0.0, 0.0, 0.0]),
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
        });
        #[cfg(feature = "opencv-seam")]
        let sample_level = levels
            .iter()
            .find(|level| {
                let (w, h) = (level.left.width(), level.left.height());
                w <= 768 && u64::from(w) * u64::from(h) <= 250_000
            })
            .unwrap_or_else(|| levels.last().unwrap());
        #[cfg(feature = "opencv-seam")]
        let mask_size = super::opencv_seam::analysis_size(
            sample_level.left.width(),
            sample_level.left.height(),
        );
        #[cfg(not(feature = "opencv-seam"))]
        let mask_size = (1, 1);
        let chosen_mask = device.create_texture(&wgpu::TextureDescriptor {
            label: Some("opencv_seam_ownership"),
            size: wgpu::Extent3d {
                width: mask_size.0,
                height: mask_size.1,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::R8Unorm,
            usage: wgpu::TextureUsages::TEXTURE_BINDING | wgpu::TextureUsages::COPY_DST,
            view_formats: &[],
        });
        #[cfg(feature = "opencv-seam")]
        let estimator = Mutex::new(Estimator::new(
            gpu,
            &sample_level.left,
            &sample_level.right,
            &right_warp,
            mask_size.0,
            mask_size.1,
        ));
        let prepare_group = group(
            device,
            &prepare_pipeline.get_bind_group_layout(0),
            &[
                wgpu::BindingResource::TextureView(&view(&left_warp)),
                wgpu::BindingResource::TextureView(&view(&right_warp)),
                state.as_entire_binding(),
                wgpu::BindingResource::TextureView(&view(&levels[0].left)),
                wgpu::BindingResource::TextureView(&view(&levels[0].right)),
                wgpu::BindingResource::TextureView(&view(&levels[0].mask)),
                wgpu::BindingResource::TextureView(&view(&chosen_mask)),
                options.as_entire_binding(),
            ],
        );
        let mut down_groups = Vec::new();
        for pair in levels.windows(2) {
            down_groups.push(
                [
                    (&pair[0].left, &pair[1].left),
                    (&pair[0].right, &pair[1].right),
                    (&pair[0].mask, &pair[1].mask),
                ]
                .map(|(input, output)| {
                    group(
                        device,
                        &down_pipeline.get_bind_group_layout(0),
                        &[
                            wgpu::BindingResource::TextureView(&view(input)),
                            wgpu::BindingResource::TextureView(&view(output)),
                        ],
                    )
                }),
            );
        }
        let sampler = device.create_sampler(&wgpu::SamplerDescriptor {
            label: Some("seam_linear"),
            mag_filter: wgpu::FilterMode::Linear,
            min_filter: wgpu::FilterMode::Linear,
            ..Default::default()
        });
        let mut reconstruct_groups = Vec::new();
        for (index, level) in levels.iter().enumerate() {
            let next = levels.get(index + 1).unwrap_or(level);
            let coarse = index + 1 == levels.len();
            let band = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
                label: Some("seam_band"),
                contents: bytemuck::cast_slice(&[
                    index as f32,
                    if coarse { 1.0 } else { 0.0 },
                    0.0,
                    0.0,
                ]),
                usage: wgpu::BufferUsages::UNIFORM,
            });
            let next_blend = if coarse { &next.left } else { &next.blended };
            reconstruct_groups.push(group(
                device,
                &reconstruct_pipeline.get_bind_group_layout(0),
                &[
                    wgpu::BindingResource::TextureView(&view(&level.left)),
                    wgpu::BindingResource::TextureView(&view(&level.right)),
                    wgpu::BindingResource::TextureView(&view(&level.mask)),
                    wgpu::BindingResource::TextureView(&view(&next.left)),
                    wgpu::BindingResource::TextureView(&view(&next.right)),
                    wgpu::BindingResource::TextureView(&view(next_blend)),
                    wgpu::BindingResource::Sampler(&sampler),
                    wgpu::BindingResource::TextureView(&view(&level.blended)),
                    band.as_entire_binding(),
                ],
            ));
        }
        let output_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("seam_output"),
            source: wgpu::ShaderSource::Wgsl(include_str!("../shaders/seam_output.wgsl").into()),
        });
        let output_pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("seam_output"),
            layout: None,
            vertex: wgpu::VertexState {
                module: &output_shader,
                entry_point: Some("vs_main"),
                compilation_options: Default::default(),
                buffers: &[],
            },
            fragment: Some(wgpu::FragmentState {
                module: &output_shader,
                entry_point: Some("fs_main"),
                compilation_options: Default::default(),
                targets: &[Some(wgpu::ColorTargetState {
                    format: output_format,
                    blend: None,
                    write_mask: wgpu::ColorWrites::ALL,
                })],
            }),
            primitive: Default::default(),
            depth_stencil: None,
            multisample: Default::default(),
            multiview_mask: None,
            cache: None,
        });
        let output_group = group(
            device,
            &output_pipeline.get_bind_group_layout(0),
            &[
                wgpu::BindingResource::TextureView(&view(&levels[0].left)),
                wgpu::BindingResource::TextureView(&view(&levels[0].right)),
                wgpu::BindingResource::TextureView(&view(&levels[0].blended)),
                wgpu::BindingResource::TextureView(&view(&levels[0].mask)),
                wgpu::BindingResource::Sampler(&sampler),
                options.as_entire_binding(),
            ],
        );
        Self {
            #[cfg(test)]
            left_warp,
            #[cfg(test)]
            right_warp,
            warp_views,
            mask_cache: Mutex::new(MaskCache::default()),
            levels,
            match_pipeline,
            match_group,
            prepare_pipeline,
            prepare_group,
            down_pipeline,
            down_groups,
            reconstruct_pipeline,
            reconstruct_groups,
            output_pipeline,
            output_group,
            options,
            #[cfg(feature = "opencv-seam")]
            chosen_mask,
            #[cfg(feature = "opencv-seam")]
            estimator,
        }
    }

    pub(super) fn set_geometry(&self, key: u64) {
        self.mask_cache
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .geometry = Some(key);
        #[cfg(feature = "opencv-seam")]
        self.estimator
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .set_geometry(key);
    }

    pub(super) fn warp_views(&self) -> &[wgpu::TextureView; 2] {
        &self.warp_views
    }

    pub(super) fn encode(
        &self,
        gpu: &GpuContext,
        encoder: &mut wgpu::CommandEncoder,
        target: &wgpu::TextureView,
        blend_width: f32,
    ) {
        #[cfg(feature = "opencv-seam")]
        let mut estimator = self.estimator.lock().unwrap_or_else(|e| e.into_inner());
        #[cfg(feature = "opencv-seam")]
        let seam_active = estimator.begin_frame(gpu, &self.chosen_mask);
        #[cfg(not(feature = "opencv-seam"))]
        let seam_active = false;
        let rebuild_mask = self
            .mask_cache
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .prepare(blend_width, seam_active);
        gpu.queue.write_buffer(
            &self.options,
            0,
            bytemuck::cast_slice(&[
                blend_width,
                f32::from(seam_active),
                f32::from(rebuild_mask),
                0.0,
            ]),
        );
        {
            let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
                label: Some("seam_match"),
                timestamp_writes: None,
            });
            pass.set_pipeline(&self.match_pipeline);
            pass.set_bind_group(0, &self.match_group, &[]);
            pass.dispatch_workgroups(1, 1, 1);
        }
        self.dispatch(
            encoder,
            &self.prepare_pipeline,
            &self.prepare_group,
            &self.levels[0].left,
        );
        if blend_width > 0.0 || cfg!(feature = "opencv-seam") {
            for (index, groups) in self.down_groups.iter().enumerate() {
                for group in groups.iter().take(if rebuild_mask { 3 } else { 2 }) {
                    self.dispatch(
                        encoder,
                        &self.down_pipeline,
                        group,
                        &self.levels[index + 1].left,
                    );
                }
            }
            #[cfg(feature = "opencv-seam")]
            estimator.encode_sample(encoder);
            for (index, group) in self.reconstruct_groups.iter().enumerate().rev() {
                self.dispatch(
                    encoder,
                    &self.reconstruct_pipeline,
                    group,
                    &self.levels[index].left,
                );
            }
        }
        let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
            label: Some("seam_output"),
            color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                view: target,
                resolve_target: None,
                ops: wgpu::Operations {
                    load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                    store: wgpu::StoreOp::Store,
                },
                depth_slice: None,
            })],
            depth_stencil_attachment: None,
            occlusion_query_set: None,
            timestamp_writes: None,
            multiview_mask: None,
        });
        pass.set_pipeline(&self.output_pipeline);
        pass.set_bind_group(0, &self.output_group, &[]);
        pass.draw(0..3, 0..1);
    }

    fn dispatch(
        &self,
        encoder: &mut wgpu::CommandEncoder,
        pipeline: &wgpu::ComputePipeline,
        group: &wgpu::BindGroup,
        output: &wgpu::Texture,
    ) {
        let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor {
            label: Some("seam_pyramid"),
            timestamp_writes: None,
        });
        pass.set_pipeline(pipeline);
        pass.set_bind_group(0, group, &[]);
        pass.dispatch_workgroups(output.width().div_ceil(8), output.height().div_ceil(8), 1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const W: u32 = 128;
    const H: u32 = 32;

    fn half(value: f32) -> u16 {
        if value == 0.0 {
            return 0;
        }
        let bits = value.to_bits();
        let exponent = ((bits >> 23) & 255) as i32 - 127 + 15;
        assert!(exponent > 0 && exponent < 31);
        ((exponent as u16) << 10) | ((bits >> 13) & 1023) as u16
    }

    fn upload(gpu: &GpuContext, texture: &wgpu::Texture, pixels: &[[f32; 4]]) {
        let data: Vec<u16> = pixels
            .iter()
            .flat_map(|pixel| pixel.iter().map(|v| half(*v)))
            .collect();
        gpu.queue.write_texture(
            wgpu::TexelCopyTextureInfo {
                texture,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            bytemuck::cast_slice(&data),
            wgpu::TexelCopyBufferLayout {
                offset: 0,
                bytes_per_row: Some(W * 8),
                rows_per_image: Some(H),
            },
            wgpu::Extent3d {
                width: W,
                height: H,
                depth_or_array_layers: 1,
            },
        );
    }

    fn run(left: &[[f32; 4]], right: &[[f32; 4]], blend: f32) -> Vec<u8> {
        run_with_setup(left, right, blend, |_, _| {})
    }

    fn run_with_setup(
        left: &[[f32; 4]],
        right: &[[f32; 4]],
        blend: f32,
        setup: impl FnOnce(&GpuContext, &SeamBlender),
    ) -> Vec<u8> {
        let gpu = GpuContext::new_blocking()
            .expect("seam regression requires a Vulkan/Metal/DX12 adapter");
        let blender = SeamBlender::new(&gpu, W, H, wgpu::TextureFormat::Rgba8Unorm);
        upload(&gpu, &blender.left_warp, left);
        upload(&gpu, &blender.right_warp, right);
        setup(&gpu, &blender);
        render_output(&gpu, &blender, blend)
    }

    fn render_output(gpu: &GpuContext, blender: &SeamBlender, blend: f32) -> Vec<u8> {
        let output = gpu.device.create_texture(&wgpu::TextureDescriptor {
            label: Some("seam_test_output"),
            size: wgpu::Extent3d {
                width: W,
                height: H,
                depth_or_array_layers: 1,
            },
            mip_level_count: 1,
            sample_count: 1,
            dimension: wgpu::TextureDimension::D2,
            format: wgpu::TextureFormat::Rgba8Unorm,
            usage: wgpu::TextureUsages::RENDER_ATTACHMENT | wgpu::TextureUsages::COPY_SRC,
            view_formats: &[],
        });
        let staging = gpu.device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("seam_test_readback"),
            size: (W * H * 4) as u64,
            usage: wgpu::BufferUsages::MAP_READ | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let mut encoder = gpu.device.create_command_encoder(&Default::default());
        blender.encode(gpu, &mut encoder, &view(&output), blend);
        encoder.copy_texture_to_buffer(
            wgpu::TexelCopyTextureInfo {
                texture: &output,
                mip_level: 0,
                origin: wgpu::Origin3d::ZERO,
                aspect: wgpu::TextureAspect::All,
            },
            wgpu::TexelCopyBufferInfo {
                buffer: &staging,
                layout: wgpu::TexelCopyBufferLayout {
                    offset: 0,
                    bytes_per_row: Some(W * 4),
                    rows_per_image: Some(H),
                },
            },
            wgpu::Extent3d {
                width: W,
                height: H,
                depth_or_array_layers: 1,
            },
        );
        gpu.queue.submit([encoder.finish()]);
        let (tx, rx) = std::sync::mpsc::channel();
        staging
            .slice(..)
            .map_async(wgpu::MapMode::Read, move |result| {
                tx.send(result).unwrap();
            });
        gpu.device
            .poll(wgpu::PollType::wait_indefinitely())
            .unwrap();
        rx.recv().unwrap().unwrap();
        let pixels = staging.slice(..).get_mapped_range().to_vec();
        staging.unmap();
        pixels
    }

    fn pixel(color: [u8; 3], alpha: f32) -> [f32; 4] {
        [
            color[0] as f32 / 255.0,
            color[1] as f32 / 255.0,
            color[2] as f32 / 255.0,
            alpha,
        ]
    }

    fn pair(mut f: impl FnMut(u32, u32) -> ([f32; 4], [f32; 4])) -> (Vec<[f32; 4]>, Vec<[f32; 4]>) {
        let mut left = Vec::new();
        let mut right = Vec::new();
        for y in 0..H {
            for x in 0..W {
                let (a, b) = f(x, y);
                left.push(a);
                right.push(b);
            }
        }
        (left, right)
    }

    #[test]
    fn mask_cache_tracks_geometry_blend_and_seam_activation() {
        let mut cache = MaskCache::default();
        assert!(cache.prepare(0.15, false));
        assert!(cache.prepare(0.15, false));
        cache.geometry = Some(1);
        assert!(cache.prepare(0.15, false));
        assert!(!cache.prepare(0.15, false));
        assert!(cache.prepare(0.08, false));
        assert!(cache.prepare(0.08, true));
        assert!(!cache.prepare(0.08, true));
        cache.geometry = Some(2);
        assert!(cache.prepare(0.08, true));
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_cached_mask_matches_rebuild_with_changing_pixels_and_geometry() {
        let gpu = GpuContext::new_blocking().unwrap();
        let cached = SeamBlender::new(&gpu, W, H, wgpu::TextureFormat::Rgba8Unorm);
        let rebuilt = SeamBlender::new(&gpu, W, H, wgpu::TextureFormat::Rgba8Unorm);
        for frame in 0..5 {
            let geometry = if frame < 2 { 1 } else { 2 };
            let blend = if frame < 3 { 0.15 } else { 0.08 };
            let edge = if frame < 2 { 32 } else { 40 };
            let (left, right) = pair(|x, y| {
                let color = [60 + frame * 9, 80 + ((x + y) % 32) as u8, 100];
                let fade = ((x as f32 - edge as f32) / 64.0).clamp(0.0, 1.0);
                (
                    pixel(color, if x < 96 { 1.0 } else { 0.0 }),
                    pixel(color, if x >= edge { 0.001 + 0.999 * fade } else { 0.0 }),
                )
            });
            for blender in [&cached, &rebuilt] {
                blender.set_geometry(geometry);
                upload(&gpu, &blender.left_warp, &left);
                upload(&gpu, &blender.right_warp, &right);
                #[cfg(feature = "opencv-seam")]
                if frame == 4 {
                    let mask: Vec<u8> = (0..W * H)
                        .map(|p| if p % W >= 60 { 255 } else { 0 })
                        .collect();
                    gpu.queue.write_texture(
                        wgpu::TexelCopyTextureInfo {
                            texture: &blender.chosen_mask,
                            mip_level: 0,
                            origin: wgpu::Origin3d::ZERO,
                            aspect: wgpu::TextureAspect::All,
                        },
                        &mask,
                        wgpu::TexelCopyBufferLayout {
                            offset: 0,
                            bytes_per_row: Some(W),
                            rows_per_image: Some(H),
                        },
                        wgpu::Extent3d {
                            width: W,
                            height: H,
                            depth_or_array_layers: 1,
                        },
                    );
                    blender.estimator.lock().unwrap().activate_for_test();
                }
            }
            rebuilt.mask_cache.lock().unwrap().prepared = None;
            assert_eq!(
                render_output(&gpu, &cached, blend),
                render_output(&gpu, &rebuilt, blend)
            );
        }
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_seam_matches_exposure_and_white_balance_in_corresponding_overlap() {
        let (left, right) = pair(|x, _| {
            let fade = ((x as f32 - 40.0) / 48.0).clamp(0.0, 1.0);
            (
                pixel([160, 130, 110], if x < 88 { 1.0 } else { 0.0 }),
                pixel(
                    [90, 105, 130],
                    if x >= 40 { 0.001 + 0.999 * fade } else { 0.0 },
                ),
            )
        });
        let output = run(&left, &right, 0.15);
        for x in [20_usize, 42, 60, 70, 86, 108] {
            let p = (H as usize / 2 * W as usize + x) * 4;
            for (channel, expected) in [120_i32, 117, 120].into_iter().enumerate() {
                assert!(
                    (output[p + channel] as i32 - expected).abs() <= 3,
                    "x={x} channel={channel}: {:?}",
                    &output[p..p + 4]
                );
            }
            assert_eq!(output[p + 3], 255);
        }
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_seam_does_not_fade_sole_camera_coverage_to_black() {
        let (left, right) = pair(|x, _| {
            (
                pixel([90, 120, 150], if x < 64 { 1.0 } else { 0.0 }),
                pixel([180, 80, 100], if x >= 64 { 0.001 } else { 0.0 }),
            )
        });
        let output = run(&left, &right, 0.15);
        for (index, rgba) in output.chunks_exact(4).enumerate() {
            let expected = if index % (W as usize) < 64 {
                [90, 120, 150]
            } else {
                [180, 80, 100]
            };
            for channel in 0..3 {
                assert!((rgba[channel] as i32 - expected[channel]).abs() <= 1);
            }
            assert_eq!(rgba[3], 255);
        }
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_seam_preserves_identical_fine_texture_across_bands() {
        let (left, right) = pair(|x, y| {
            let color = if (x + y).is_multiple_of(2) {
                [40, 60, 80]
            } else {
                [190, 200, 210]
            };
            let fade = ((x as f32 - 32.0) / 64.0).clamp(0.0, 1.0);
            (
                pixel(color, if x < 96 { 1.0 } else { 0.0 }),
                pixel(color, if x >= 32 { 0.001 + 0.999 * fade } else { 0.0 }),
            )
        });
        let output = run(&left, &right, 0.15);
        for (index, rgba) in output.chunks_exact(4).enumerate() {
            let x = index % W as usize;
            let y = index / W as usize;
            let expected = if (x + y).is_multiple_of(2) {
                [40, 60, 80]
            } else {
                [190, 200, 210]
            };
            for channel in 0..3 {
                assert!(
                    (rgba[channel] as i32 - expected[channel]).abs() <= 2,
                    "pixel{index}: {rgba:?}"
                );
            }
        }
    }

    #[test]
    #[ignore = "requires GPU"]
    fn gpu_seam_handles_clipped_dark_missing_and_hard_cut_inputs() {
        let (left, right) = pair(|x, _| {
            if x < 16 {
                return (pixel([0, 0, 0], 0.0), pixel([0, 0, 0], 0.0));
            }
            (pixel([0, 0, 0], 1.0), pixel([255, 255, 255], 1.0))
        });
        for blend in [0.0, 0.15] {
            let output = run(&left, &right, blend);
            assert!(output.chunks_exact(4).all(|rgba| rgba[3] == 255));
            assert_eq!(&output[0..4], &[0, 0, 0, 255]);
            assert_eq!(&output[64 * 4..64 * 4 + 4], &[255, 255, 255, 255]);
        }
    }
    #[test]
    #[ignore = "requires GPU"]
    fn gpu_opencv_sampling_shader_is_valid() {
        let gpu = GpuContext::new_blocking().unwrap();
        let _pipeline = compute(
            &gpu.device,
            "opencv_seam_sample",
            include_str!("../shaders/seam_sample.wgsl"),
        );
    }

    #[cfg(feature = "opencv-seam")]
    #[test]
    #[ignore = "requires GPU and OpenCV"]
    fn gpu_opencv_mask_changes_actual_composition_and_preserves_exclusive_pixels() {
        let (left, right) = pair(|x, y| {
            let color = if (60..70).contains(&x) && (8..24).contains(&y) {
                [210, 40, 30]
            } else {
                [80, 90, 100]
            };
            let fade = ((x as f32 - 32.0) / 64.0).clamp(0.0, 1.0);
            (
                pixel([80, 90, 100], if x < 96 { 1.0 } else { 0.0 }),
                pixel(color, if x >= 32 { 0.001 + 0.999 * fade } else { 0.0 }),
            )
        });
        let mut packed = Vec::new();
        for (a, b) in left.iter().zip(&right) {
            for p in [a, b] {
                packed.extend(p[..3].iter().map(|v| (v * 255.0).round() as u8));
                packed.push(if p[3] > 0.0 { 255 } else { 0 });
            }
            packed.extend_from_slice(&(if b[3] >= 0.5005 { 255_u32 } else { 0 }).to_le_bytes());
        }
        let mask = super::super::opencv_seam::solve_test(W, H, packed).unwrap();
        let baseline = run(&left, &right, 0.0);
        let selected = run_with_setup(&left, &right, 0.0, |gpu, blender| {
            gpu.queue.write_texture(
                wgpu::TexelCopyTextureInfo {
                    texture: &blender.chosen_mask,
                    mip_level: 0,
                    origin: wgpu::Origin3d::ZERO,
                    aspect: wgpu::TextureAspect::All,
                },
                &mask,
                wgpu::TexelCopyBufferLayout {
                    offset: 0,
                    bytes_per_row: Some(W),
                    rows_per_image: Some(H),
                },
                wgpu::Extent3d {
                    width: W,
                    height: H,
                    depth_or_array_layers: 1,
                },
            );
            blender.estimator.lock().unwrap().activate_for_test();
        });
        let changed = baseline
            .chunks_exact(4)
            .zip(selected.chunks_exact(4))
            .filter(|(a, b)| (a[0] as i32 - b[0] as i32).abs() > 30)
            .count();
        assert!(
            changed > 10,
            "OpenCV mask must change actual output, changed={changed}"
        );
        for y in 0..H as usize {
            for x in [0_usize, 16, 112, 127] {
                let p = (y * W as usize + x) * 4;
                assert_eq!(&baseline[p..p + 4], &selected[p..p + 4]);
            }
        }
    }
}
