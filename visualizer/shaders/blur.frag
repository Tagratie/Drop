#version 330 core

// Separable Gaussian blur, one axis per pass. Caller invokes this twice
// per bloom iteration — once with u_direction=(1,0), then with (0,1) —
// which gives the same result as a 2D Gaussian for ~N times less work
// (Gaussian is the only kernel where this separability holds exactly).
//
// 9-tap with hand-picked weights from a sigma≈2 Gaussian normalized to
// sum to 1. The tap offsets are in pixel units, scaled by 1/resolution
// to get UV-space deltas.

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_tex;
uniform vec2  u_direction;     // (1,0) or (0,1) — pure horizontal or vertical
uniform vec2  u_resolution;    // tex size in pixels
uniform float u_radius;        // tap-spacing multiplier (effectively widens the blur)

void main() {
    vec2 step = u_direction / u_resolution * u_radius;

    // Weights from a normalized Gaussian (σ≈2). Sum = 1.0.
    vec3 acc = vec3(0.0);
    acc += texture(u_tex, v_uv - step * 4.0).rgb * 0.0162;
    acc += texture(u_tex, v_uv - step * 3.0).rgb * 0.0540;
    acc += texture(u_tex, v_uv - step * 2.0).rgb * 0.1216;
    acc += texture(u_tex, v_uv - step * 1.0).rgb * 0.1946;
    acc += texture(u_tex, v_uv              ).rgb * 0.2270;
    acc += texture(u_tex, v_uv + step * 1.0).rgb * 0.1946;
    acc += texture(u_tex, v_uv + step * 2.0).rgb * 0.1216;
    acc += texture(u_tex, v_uv + step * 3.0).rgb * 0.0540;
    acc += texture(u_tex, v_uv + step * 4.0).rgb * 0.0162;

    frag_color = vec4(acc, 1.0);
}
