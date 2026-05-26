#version 330 core

// Static twinkly multi-octave starfield with slow inward radial drift.
// No audio reactivity — pure ambient background.
//
// Why this shader doesn't have the diagonal-line seam the previous
// streaming version did: that one used atan() to bin stars into angular
// cells, which has a discontinuity at θ = ±π — adjacent pixels across
// the negative x-axis had different cell IDs and therefore different
// stars, producing a visible line. This shader uses a plain 2D hash grid
// on UVs, no polar discretization, so no seam.
//
// Effects toggleable via uniforms (0.0 = off, 1.0 = on, fed by renderer.py):
//   u_twinkle  — per-star sin() brightness modulation over time
//   u_drift    — slow radial inward motion
//   u_vignette — soft radial darkening around edges

in vec2 v_uv;
out vec4 frag_color;

uniform float u_time;
uniform float u_aspect;

uniform float u_twinkle;
uniform float u_drift;
uniform float u_vignette;

float hash21(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}

void main() {
    // ── Inward drift ───────────────────────────────────────────────────
    // We compute a unit vector pointing OUTWARD from screen center, scaled
    // by time, and add it to the UV used for hash sampling. Sampling at
    // (v_uv + outward_offset) means at any fixed pixel we're seeing the
    // star that *used to be* further out — the visual impression is
    // stars drifting inward toward the center.
    //
    // unit_outward has magnitude 1 for non-center pixels, so the inward
    // speed is uniform across the screen (not radial-speed-dependent).
    vec2 from_center = v_uv - vec2(0.5);
    float dist = length(from_center) + 0.0001;
    vec2 unit_outward = from_center / dist;

    float drift_speed = 0.012;   // UV-units per second; small = slow drift
    vec2 sample_uv = v_uv + unit_outward * u_time * drift_speed * u_drift;

    // Aspect-correct so star cells stay square on wide windows.
    vec2 uv = sample_uv;
    uv.x *= u_aspect;

    // ── Multi-octave stars ─────────────────────────────────────────────
    // Three layers at different densities for the depth feel. Each cell
    // either has a star (top ~3-4% of hash values) or is empty.
    vec3 col = vec3(0.0);
    for (int i = 0; i < 3; i++) {
        float scale  = 14.0 + float(i) * 22.0;
        float thresh = 0.965 + float(i) * 0.008;
        float bright = 1.0 - float(i) * 0.30;

        vec2 p  = uv * scale;
        vec2 ip = floor(p);
        vec2 fp = fract(p) - 0.5;
        float r = hash21(ip + float(i) * 91.7);

        if (r > thresh) {
            float d = length(fp);
            // Small star disc — 5% of cell radius. Soft falloff via smoothstep.
            float disc = 1.0 - smoothstep(0.0, 0.05, d);

            // Twinkle (toggleable). When disabled, brightness is steady.
            // mix(1.0, varying, u_twinkle): u_twinkle=0 → constant 1.0,
            // u_twinkle=1 → full sin()-driven variation.
            float tw_full = 0.45 + 0.55 * sin(u_time * 1.3 + r * 31.4);
            float tw = mix(1.0, tw_full, u_twinkle);

            col += vec3(disc) * bright * tw;
        }
    }

    // ── Vignette (toggleable) ──────────────────────────────────────────
    vec2 vc = v_uv - vec2(0.5);
    float vig = 1.0 - smoothstep(0.35, 0.95, length(vc) * 1.6);
    col += vec3(0.02, 0.02, 0.04) * vig * u_vignette;

    frag_color = vec4(col, 1.0);
}
