#version 330 core

// Point-sprite particles. Each particle is a single GL_POINTS vertex with
// (position, size, life). The fragment shader turns the square point into
// a soft circular dot.

in vec2  in_pos;       // NDC position
in float in_size;      // point sprite size in pixels
in float in_life;      // [0..1] — fades out as life decreases

uniform float u_aspect;
uniform float u_zoom;
uniform vec2  u_shake;

out float v_life;

void main() {
    vec2 p = in_pos;
    p *= u_zoom;
    p += u_shake;
    p.x /= u_aspect;     // aspect correction matches the waveform pass
    gl_Position  = vec4(p, 0.0, 1.0);
    gl_PointSize = in_size;
    v_life = in_life;
}
