#version 330 core

// Final composite pass. Combines:
//   1. The scene texture (background + waveform + particles)
//   2. The blurred bright pass (the bloom result)
//   3. Subtle chromatic aberration on the scene RGB channels
//
// The CA samples R, G, B at slightly different UVs offset radially from
// the screen center, so the effect intensifies toward the edges — mimics
// real lens chromatic distortion. Strength is tiny (≈0.005) so it's felt,
// not seen.

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_scene;
uniform sampler2D u_bloom;
uniform float u_bloom_strength;
uniform float u_chromatic;     // 0 disables; 0.003–0.01 looks good

void main() {
    // Chromatic aberration: each channel sampled at an offset proportional
    // to distance from center. Vector from center scaled by u_chromatic.
    vec2 from_center = v_uv - vec2(0.5);
    vec2 offset = from_center * u_chromatic;

    float r = texture(u_scene, v_uv + offset).r;
    float g = texture(u_scene, v_uv         ).g;
    float b = texture(u_scene, v_uv - offset).b;
    vec3 scene = vec3(r, g, b);

    // Bloom — read from the blurred bright pass, scaled by strength.
    vec3 bloom = texture(u_bloom, v_uv).rgb;
    vec3 final = scene + bloom * u_bloom_strength;

    // Cheap Reinhard tonemap so accumulated bloom doesn't blow out to
    // pure white on loud kicks. Keeps highlights tasteful.
    final = final / (final + vec3(0.85));

    frag_color = vec4(final, 1.0);
}
