@group(0) @binding(0) var left: texture_2d<f32>;
@group(0) @binding(1) var right: texture_2d<f32>;
@group(0) @binding(2) var blended: texture_2d<f32>;
@group(0) @binding(3) var mask: texture_2d<f32>;
@group(0) @binding(4) var linear_sampler: sampler;
@group(0) @binding(5) var<uniform> options: vec4<f32>;

struct VertexOut { @builtin(position) position: vec4<f32>, @location(0) uv: vec2<f32> };
@vertex
fn vs_main(@builtin(vertex_index) i: u32) -> VertexOut {
    let pos = array<vec2<f32>, 3>(vec2<f32>(-1.0, -1.0), vec2<f32>(3.0, -1.0), vec2<f32>(-1.0, 3.0));
    var out: VertexOut;
    out.position = vec4<f32>(pos[i], 0.0, 1.0);
    out.uv = vec2<f32>((pos[i].x + 1.0) * 0.5, (1.0 - pos[i].y) * 0.5);
    return out;
}
@fragment
fn fs_main(in: VertexOut) -> @location(0) vec4<f32> {
    let a = textureSample(left, linear_sampler, in.uv);
    let b = textureSample(right, linear_sampler, in.uv);
    if a.a < 0.5 && b.a < 0.5 { return vec4<f32>(0.0, 0.0, 0.0, 1.0); }
    if a.a < 0.5 { return vec4<f32>(clamp(b.rgb, vec3<f32>(0.0), vec3<f32>(1.0)), 1.0); }
    if b.a < 0.5 { return vec4<f32>(clamp(a.rgb, vec3<f32>(0.0), vec3<f32>(1.0)), 1.0); }
    if options.x == 0.0 {
        let w = textureSample(mask, linear_sampler, in.uv).r;
        return vec4<f32>(clamp(select(a.rgb, b.rgb, w >= 0.5), vec3<f32>(0.0), vec3<f32>(1.0)), 1.0);
    }
    let color = textureSample(blended, linear_sampler, in.uv).rgb;
    return vec4<f32>(clamp(color, vec3<f32>(0.0), vec3<f32>(1.0)), 1.0);
}
