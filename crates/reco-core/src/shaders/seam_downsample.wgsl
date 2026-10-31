@group(0) @binding(0) var input_image: texture_2d<f32>;
@group(0) @binding(1) var output_image: texture_storage_2d<rgba16float, write>;

@compute @workgroup_size(8, 8)
fn main(@builtin(global_invocation_id) id: vec3<u32>) {
    if any(id.xy >= textureDimensions(output_image)) { return; }
    let size = vec2<i32>(textureDimensions(input_image));
    let center = vec2<i32>(id.xy * 2u);
    var sum = vec4<f32>(0.0);
    var total = 0.0;
    for (var y = -1; y <= 2; y++) {
        for (var x = -1; x <= 2; x++) {
            let p = clamp(center + vec2<i32>(x, y), vec2<i32>(0), size - 1);
            let v = textureLoad(input_image, p, 0);
            let w = select(3.0, 1.0, x == -1 || x == 2) * select(3.0, 1.0, y == -1 || y == 2);
            sum += vec4<f32>(v.rgb * v.a, v.a) * w;
            total += w;
        }
    }
    textureStore(output_image, vec2<i32>(id.xy), vec4<f32>(sum.rgb / max(sum.a, 0.00001), sum.a / total));
}
