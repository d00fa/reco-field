@group(0) @binding(0) var left: texture_2d<f32>;
@group(0) @binding(1) var right: texture_2d<f32>;
@group(0) @binding(2) var right_warp: texture_2d<f32>;
@group(0) @binding(3) var<storage, read_write> packed: array<u32>;
@group(0) @binding(4) var<uniform> size: vec4<u32>;

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if any(id.xy >= size.xy) { return; }
    let uv = (vec2<f32>(id.xy) + 0.5) / vec2<f32>(size.xy);
    let source_size = textureDimensions(left);
    let p = min(vec2<u32>(uv * vec2<f32>(source_size)), source_size - 1u);
    let a = textureLoad(left, vec2<i32>(p), 0);
    let b = textureLoad(right, vec2<i32>(p), 0);
    let warp_size = textureDimensions(right_warp);
    let q = min(vec2<u32>(uv * vec2<f32>(warp_size)), warp_size - 1u);
    let fade = clamp((textureLoad(right_warp, vec2<i32>(q), 0).a - 0.001) / 0.999, 0.0, 1.0);
    let index = (id.y * size.x + id.x) * 3u;
    packed[index] = pack4x8unorm(vec4<f32>(clamp(a.rgb, vec3<f32>(0.0), vec3<f32>(1.0)), select(0.0, 1.0, a.a > 0.99)));
    packed[index + 1u] = pack4x8unorm(vec4<f32>(clamp(b.rgb, vec3<f32>(0.0), vec3<f32>(1.0)), select(0.0, 1.0, b.a > 0.99)));
    var weight = fade;
    if a.a <= 0.99 { weight = 1.0; }
    if b.a <= 0.99 { weight = 0.0; }
    packed[index + 2u] = u32(weight >= 0.5) * 255u;
}
