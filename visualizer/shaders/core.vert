#version 330 core

// Inner core ring — a thin bright loop just inside u_base_radius that
// pulses with overall energy. Acts as the "album art frame" outline that
// trap-style visualizers usually have around the centerpiece.

in float in_angle;
in float in_side;

uniform float u_radius;
uniform float u_thickness;
uniform float u_aspect;
uniform float u_rotation;
uniform float u_zoom;
uniform vec2  u_shake;
uniform float u_bass;

out float v_side;

void main() {
    // Slight pulse on bass. Tiny — the core is supposed to feel anchored.
    float r = u_radius * (1.0 + u_bass * 0.04);
    if (in_side > 0.0) r += u_thickness;

    float a = in_angle + u_rotation;
    vec2 pos = vec2(cos(a), sin(a)) * r;
    pos *= u_zoom;
    pos += u_shake;
    pos.x /= u_aspect;

    gl_Position = vec4(pos, 0.0, 1.0);
    v_side = in_side;
}
