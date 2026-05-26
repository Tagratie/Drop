#version 330 core

// Vertex shader for the circular waveform mesh. The mesh is a triangle strip
// running around a ring — pairs of vertices (inner edge, outer edge) at each
// angle around the circle. Per-frame the CPU writes a per-vertex magnitude
// (the smoothed FFT band magnitude for that angle) and we displace radially.
//
// Camera effects (zoom, shake, rotation drift) are applied here so all the
// transforms happen in one place and the fragment shader stays simple.

in float in_angle;     // [0, 2π) — fixed at mesh build time
in float in_side;      // -1 = inner edge of ring, +1 = outer edge
in float in_mag;       // per-vertex magnitude, updated each frame

uniform float u_base_radius;   // ring inner radius in NDC
uniform float u_thickness;     // ring thickness in NDC
uniform float u_amplitude;     // how far loud bands push outward
uniform float u_aspect;        // window aspect, so circles stay circular
uniform float u_rotation;      // accumulated rotation drift
uniform float u_zoom;          // bass-reactive zoom multiplier
uniform vec2  u_shake;         // bass-pulse camera shake in NDC

out float v_mag;
out float v_side;
out float v_radial;            // 0..1 from inner to outer edge — used by frag for gradient

void main() {
    // Inner radius starts at u_base_radius; outer is base + magnitude*amplitude.
    // Width of the bright "core" is u_thickness — the magnitude lifts both edges
    // outward together so the entire band moves, not just the outer edge.
    float r_inner = u_base_radius + in_mag * u_amplitude;
    float r_outer = r_inner + u_thickness;
    float r = mix(r_inner, r_outer, (in_side + 1.0) * 0.5);

    float a = in_angle + u_rotation;
    vec2 pos = vec2(cos(a), sin(a)) * r;

    // Zoom around origin, then add shake. Order matters — shaking before zoom
    // would make the shake amplitude scale with zoom, which looks wrong.
    pos *= u_zoom;
    pos += u_shake;

    // Aspect correction: divide X by aspect so the ring is circular on
    // non-square windows. We could use an actual projection matrix but
    // for a single-screen viz this is one line and equivalent.
    pos.x /= u_aspect;

    gl_Position = vec4(pos, 0.0, 1.0);
    v_mag    = in_mag;
    v_side   = in_side;
    v_radial = (in_side + 1.0) * 0.5;
}
