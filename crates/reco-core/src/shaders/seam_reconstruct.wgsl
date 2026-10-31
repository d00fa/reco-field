struct Band { level: f32, coarse: f32, pad: vec2<f32> };
@group(0) @binding(0) var left: texture_2d<f32>;
@group(0) @binding(1) var right: texture_2d<f32>;
@group(0) @binding(2) var mask: texture_2d<f32>;
@group(0) @binding(3) var next_left: texture_2d<f32>;
@group(0) @binding(4) var next_right: texture_2d<f32>;
@group(0) @binding(5) var next_blend: texture_2d<f32>;
@group(0) @binding(6) var linear_sampler: sampler;
@group(0) @binding(7) var output_image: texture_storage_2d<rgba16float, write>;
@group(0) @binding(8) var<uniform> band: Band;

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    let size = textureDimensions(left);
    if any(id.xy >= size) { return; }
    let p = vec2<i32>(id.xy);
    let uv = (vec2<f32>(id.xy) + 0.5) / vec2<f32>(size);
    let a = textureLoad(left, p, 0);
    let b = textureLoad(right, p, 0);
    var weight = textureLoad(mask, p, 0).r;
    // Keep fine edges tight, while coarse illumination uses the broad mask.
    let half_width = min(0.5, 0.05 + band.level * 0.09);
    if band.coarse < 0.5 { weight = smoothstep(0.5 - half_width, 0.5 + half_width, weight); }
    let wa = (1.0 - weight) * a.a;
    let wb = weight * b.a;
    let total = max(wa + wb, 0.00001);
    var color = (a.rgb * wa + b.rgb * wb) / total;
    if band.coarse < 0.5 {
        let low_a = textureSampleLevel(next_left, linear_sampler, uv, 0.0).rgb;
        let low_b = textureSampleLevel(next_right, linear_sampler, uv, 0.0).rgb;
        let low_blend = textureSampleLevel(next_blend, linear_sampler, uv, 0.0).rgb;
        color = low_blend + ((a.rgb - low_a) * wa + (b.rgb - low_b) * wb) / total;
    }
    textureStore(output_image, p, vec4<f32>(color, max(a.a, b.a)));
}
