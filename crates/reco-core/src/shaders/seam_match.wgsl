struct MatchState {
    left_gain: vec4<f32>,
    right_gain: vec4<f32>,
};

@group(0) @binding(0) var left: texture_2d<f32>;
@group(0) @binding(1) var right: texture_2d<f32>;
@group(0) @binding(2) var<storage, read_write> state: MatchState;

var<workgroup> ratios: array<vec4<f32>, 256>;

// Corresponding output pixels sample the same scene after lens/rig projection.
@compute @workgroup_size(256)
fn main(@builtin(local_invocation_index) tid: u32) {
    let size = textureDimensions(left);
    var sum = vec4<f32>(0.0);
    for (var i = tid; i < 8192u; i += 256u) {
        let grid = vec2<u32>(i % 128u, i / 128u);
        let p = vec2<i32>(min((grid * size + size / 2u) / vec2<u32>(128u, 64u), size - 1u));
        let a = textureLoad(left, p, 0);
        let b = textureLoad(right, p, 0);
        if a.a > 0.0005 && b.a > 0.0005 &&
            all(a.rgb > vec3<f32>(0.04)) && all(b.rgb > vec3<f32>(0.04)) &&
            all(a.rgb < vec3<f32>(0.94)) && all(b.rgb < vec3<f32>(0.94)) {
            let ratio = a.rgb / b.rgb;
            // Exclude large mismatches caused by parallax or a moving foreground.
            if all(ratio > vec3<f32>(0.4)) && all(ratio < vec3<f32>(2.5)) {
                sum += vec4<f32>(log(ratio), 1.0);
            }
        }
    }
    ratios[tid] = sum;
    workgroupBarrier();
    for (var stride = 128u; stride > 0u; stride /= 2u) {
        if tid < stride {
            ratios[tid] += ratios[tid + stride];
        }
        workgroupBarrier();
    }
    if tid == 0u && ratios[0].w >= 64.0 {
        let half_ratio = clamp(exp(0.5 * ratios[0].rgb / ratios[0].w), vec3<f32>(0.75), vec3<f32>(1.333333));
        let amount = select(0.04, 1.0, state.left_gain.w < 0.5);
        state.left_gain = vec4<f32>(mix(state.left_gain.rgb, 1.0 / half_ratio, amount), 1.0);
        state.right_gain = vec4<f32>(mix(state.right_gain.rgb, half_ratio, amount), 1.0);
    }
}
