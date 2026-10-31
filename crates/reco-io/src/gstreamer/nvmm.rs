//! NvBufSurface metadata extraction for NVMM camera buffers on Jetson.
//!
//! On Jetson, `nvarguscamerasrc` outputs NV12 frames in NVMM memory.
//! `gst_buffer_map` returns a pointer to an `NvBufSurface` metadata struct
//! (not pixel data). The `bufferDesc` field holds the DMA-buf file descriptor
//! for zero-copy Vulkan import, and the surface pointer is passed to
//! NvBufSurfTransform for detection preprocessing.
//!
//! Struct layouts verified against nvbufsurface.h on JetPack 6 (L4T 36.x)
//! via offsetof() on Jetson Orin Nano aarch64.

use std::ffi::c_void;

use reco_core::render::renderer::{InputColorimetry, YuvMatrix};

const NVBUF_MAX_PLANES: usize = 4;
const STRUCTURE_PADDING: usize = 4;

/// NVBUF_MEM_SURFACE_ARRAY = 4 (hardware-allocated NVMM buffers from ISP/NVDEC).
const NVBUF_MEM_SURFACE_ARRAY: u32 = 4;

/// Per-plane layout information (sizeof verified on aarch64).
#[repr(C)]
pub(crate) struct NvBufSurfacePlaneParams {
    pub num_planes: u32,
    pub width: [u32; NVBUF_MAX_PLANES],
    pub height: [u32; NVBUF_MAX_PLANES],
    pub pitch: [u32; NVBUF_MAX_PLANES],
    pub offset: [u32; NVBUF_MAX_PLANES],
    pub psize: [u32; NVBUF_MAX_PLANES],
    pub bytes_per_pix: [u32; NVBUF_MAX_PLANES],
    _reserved: [*mut c_void; STRUCTURE_PADDING * NVBUF_MAX_PLANES],
}

/// Mapped address pointers (used only for CPU mapping, not for zero-copy).
#[repr(C)]
pub(crate) struct NvBufSurfaceMappedAddr {
    pub addr: [*mut c_void; NVBUF_MAX_PLANES],
    pub egl_image: *mut c_void,
    _reserved: [*mut c_void; STRUCTURE_PADDING],
}

/// Per-buffer parameters (sizeof=384, verified on aarch64).
#[repr(C)]
pub(crate) struct NvBufSurfaceParams {
    pub width: u32,                            // offset 0
    pub height: u32,                           // offset 4
    pub pitch: u32,                            // offset 8
    pub color_format: u32,                     // offset 12
    pub layout: u32,                           // offset 16
    _pad0: u32,                                // offset 20
    pub buffer_desc: i64,                      // offset 24 (DMA-buf fd as signed)
    pub data_size: u32,                        // offset 32
    _pad1: u32,                                // offset 36
    pub data_ptr: *mut c_void,                 // offset 40
    pub plane_params: NvBufSurfacePlaneParams, // offset 48
    pub mapped_addr: NvBufSurfaceMappedAddr,
    pub paramex: *mut c_void,
    _reserved: [*mut c_void; STRUCTURE_PADDING - 1],
}

/// Top-level batched buffer container (sizeof=64, verified on aarch64).
#[repr(C)]
pub(crate) struct NvBufSurface {
    pub gpu_id: u32,                           // offset 0
    pub batch_size: u32,                       // offset 4
    pub num_filled: u32,                       // offset 8
    pub is_contiguous: u8,                     // offset 12
    _pad0: [u8; 3],                            // offset 13
    pub mem_type: u32,                         // offset 16
    _pad1: u32,                                // offset 20
    pub surface_list: *mut NvBufSurfaceParams, // offset 24
    pub is_imported_buf: u8,                   // offset 32
    _pad2: [u8; 7],
    _reserved: [*mut c_void; STRUCTURE_PADDING - 1],
}

/// Extracted metadata from an NVMM buffer for zero-copy rendering + detection.
#[derive(Debug)]
pub struct NvmmFrameInfo {
    /// NvBufSurfaceColorFormat value reported by the producer.
    pub color_format: u32,
    /// DMA-buf file descriptor for Vulkan import (rendering path).
    pub dmabuf_fd: i32,
    /// Frame width in pixels.
    pub width: u32,
    /// Frame height in pixels.
    pub height: u32,
    /// Y plane byte offset within the DMA-buf.
    pub y_offset: u32,
    /// Y plane pitch (row stride) in bytes.
    pub y_pitch: u32,
    /// UV plane byte offset within the DMA-buf.
    pub uv_offset: u32,
    /// UV plane pitch in bytes.
    pub uv_pitch: u32,
    /// Total allocation size (needed for Vulkan memory import).
    pub total_size: u32,
    /// Raw NvBufSurface pointer for NvBufSurfTransform (detection path).
    /// Valid only while the GStreamer sample is held.
    pub surface_ptr: *mut c_void,
}

impl NvmmFrameInfo {
    /// Decode the JetPack surface's matrix/range, rejecting unsupported formats.
    pub fn input_colorimetry(&self) -> Result<InputColorimetry, String> {
        let (matrix, full_range) = match self.color_format {
            6 => (YuvMatrix::Bt601, false),
            7 => (YuvMatrix::Bt601, true),
            33 => (YuvMatrix::Bt709, false),
            34 => (YuvMatrix::Bt709, true),
            format => {
                return Err(format!(
                    "unsupported NvBufSurface color_format={format}; expected NV12 BT.601 (6/7) or BT.709 (33/34)"
                ));
            }
        };
        Ok(InputColorimetry { matrix, full_range })
    }
}

// SAFETY: surface_ptr is a stable kernel-managed address (NVMM pool buffer).
// The capture thread holds the GstSample keeping it alive until release.
unsafe impl Send for NvmmFrameInfo {}

/// Extract NV12 plane metadata from a GStreamer NVMM buffer's mapped data.
///
/// # Safety
///
/// `mapped_data` must be the raw pointer from `gst_buffer_map(GST_MAP_READ)`
/// on an NVMM buffer. On Jetson, this points to an `NvBufSurface` struct.
pub unsafe fn extract_nvmm_frame_info(mapped_data: *const u8) -> Result<NvmmFrameInfo, String> {
    let surface = unsafe { &*(mapped_data as *const NvBufSurface) };

    if surface.mem_type != NVBUF_MEM_SURFACE_ARRAY {
        return Err(format!(
            "expected NVBUF_MEM_SURFACE_ARRAY (4), got memType={}",
            surface.mem_type
        ));
    }

    if surface.num_filled == 0 {
        return Err("NvBufSurface has 0 filled buffers".into());
    }

    let params = unsafe { &*surface.surface_list };

    if params.plane_params.num_planes < 2 {
        return Err(format!(
            "expected 2 NV12 planes, got {}",
            params.plane_params.num_planes
        ));
    }

    let total_size = params.plane_params.offset[1]
        + params.plane_params.pitch[1] * params.plane_params.height[1];

    let info = NvmmFrameInfo {
        color_format: params.color_format,
        dmabuf_fd: params.buffer_desc as i32,
        width: params.width,
        height: params.height,
        y_offset: params.plane_params.offset[0],
        y_pitch: params.plane_params.pitch[0],
        uv_offset: params.plane_params.offset[1],
        uv_pitch: params.plane_params.pitch[1],
        total_size,
        surface_ptr: mapped_data as *mut c_void,
    };
    info.input_colorimetry()?;
    Ok(info)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extraction_preserves_surface_color_format() {
        // Both C structs contain only integers and nullable pointers.
        let mut params: NvBufSurfaceParams = unsafe { std::mem::zeroed() };
        params.color_format = 6;
        params.width = 64;
        params.height = 32;
        params.plane_params.num_planes = 2;
        params.plane_params.pitch = [64, 64, 0, 0];
        params.plane_params.offset = [0, 2048, 0, 0];
        params.plane_params.height = [32, 16, 0, 0];
        let mut surface: NvBufSurface = unsafe { std::mem::zeroed() };
        surface.mem_type = NVBUF_MEM_SURFACE_ARRAY;
        surface.num_filled = 1;
        surface.surface_list = &mut params;

        let mut info =
            unsafe { extract_nvmm_frame_info((&surface as *const NvBufSurface).cast::<u8>()) }
                .unwrap();
        assert_eq!(info.color_format, 6);
        assert_eq!((info.width, info.height, info.total_size), (64, 32, 3072));

        for (format, matrix, full_range) in [
            (6, YuvMatrix::Bt601, false),
            (7, YuvMatrix::Bt601, true),
            (33, YuvMatrix::Bt709, false),
            (34, YuvMatrix::Bt709, true),
        ] {
            info.color_format = format;
            assert_eq!(
                info.input_colorimetry().unwrap(),
                InputColorimetry { matrix, full_range }
            );
        }

        for format in [0, 19, 36, u32::MAX] {
            info.color_format = format;
            assert!(
                info.input_colorimetry()
                    .unwrap_err()
                    .contains(&format.to_string())
            );
            params.color_format = format;
            surface.surface_list = &mut params;
            let result =
                unsafe { extract_nvmm_frame_info((&surface as *const NvBufSurface).cast::<u8>()) };
            assert!(result.unwrap_err().contains(&format.to_string()));
        }
    }
}
