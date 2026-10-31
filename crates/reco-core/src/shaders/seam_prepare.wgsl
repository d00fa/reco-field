struct MatchState {
    left_gain: vec4<f32>,
    right_gain: vec4<f32>,
};
@group(0) @binding(0) var left: texture_2d<f32>;
@group(0) @binding(1) var right: texture_2d<f32>;
@group(0) @binding(2) var<storage, read> state: MatchState;
@group(0) @binding(3) var out_left: texture_storage_2d<rgba16float, write>;
@group(0) @binding(4) var out_right: texture_storage_2d<rgba16float, write>;
@group(0) @binding(5) var out_mask: texture_storage_2d<rgba16float, write>;
@group(0) @binding(6) var ownership: texture_2d<f32>;
@group(0) @binding(7) var<uniform> options: vec4<f32>;

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    let p = vec2<i32>(id.xy);
    if any(id.xy >= textureDimensions(left)) { return; }
    let a = textureLoad(left, p, 0);
    let b = textureLoad(right, p, 0);
    let valid_a = select(0.0, 1.0, a.a > 0.0005);
    let valid_b = select(0.0, 1.0, b.a > 0.0005);
    textureStore(out_left, p, vec4<f32>(a.rgb * state.left_gain.rgb, valid_a));
    textureStore(out_right, p, vec4<f32>(b.rgb * state.right_gain.rgb, valid_b));
    if options.z > 0.5 {
        let fade = clamp((b.a - 0.001) / 0.999, 0.0, 1.0);
        var weight = fade;
        if options.y > 0.5 {
            let uv = (vec2<f32>(id.xy) + 0.5) / vec2<f32>(textureDimensions(left));
            let size = textureDimensions(ownership);
            let sample = min(vec2<u32>(uv * vec2<f32>(size)), size - 1u);
            weight = textureLoad(ownership, vec2<i32>(sample), 0).r;
        }
        if valid_a == 0.0 { weight = 1.0; }
        if valid_b == 0.0 { weight = 0.0; }
        textureStore(out_mask, p, vec4<f32>(weight, 0.0, 0.0, max(valid_a, valid_b)));
    }
}
