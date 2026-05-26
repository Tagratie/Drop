#version 330 core

// Fragment shader for the ring band. Renders white-hot at the inner edge
// fading to a warm red/orange at the outer edge. The bloom post-pass
// catches the bright pixels and turns the edge falloff into actual glow;
// here we just need the gradient and per-vertex brightness modulation.

in float v_mag;
in float v_side;
in float v_radial;     // 0 = inner edge, 1 = outer edge

uniform vec3 u_color_core;     // white-hot inner color  (e.g. (1, 1, 1))
uniform vec3 u_color_glow;     // warm outer color       (e.g. (1.0, 0.35, 0.10))

out vec4 frag_color;

void main() {
    // Color: solid white inside, fading to the glow color outward.
    // Smoothstep gives a softer transition than a linear mix.
    float t = smoothstep(0.0, 1.0, v_radial);
    vec3 col = mix(u_color_core, u_color_glow, t);

    // Brightness: hot core, falling off toward the edge. Loud bands also
    // make their slice of the ring brighter, which feeds the bloom pass
    // and makes spikes "glow" stronger than quiet sections.
    float core_brightness = 1.0 - t * 0.55;          // core ~1.0, edge ~0.45
    float energy = 0.55 + v_mag * 0.85;              // mag from 0..1.5 → 0.55..1.83
    float intensity = core_brightness * energy;

    // Output is pre-additive: we render this with additive blending
    // (src_alpha, ONE), so the alpha is what actually controls how much
    // ends up in the framebuffer. Premultiplying intensity into rgb here
    // gives us the same effect with simpler math downstream.
    frag_color = vec4(col * intensity, intensity);
}
