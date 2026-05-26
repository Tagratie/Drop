#version 330 core

in float v_side;
out vec4 frag_color;

uniform vec3 u_color;
uniform float u_brightness;

void main() {
    // Slight gradient across the thickness so the bloom catches a hot center.
    float t = (v_side + 1.0) * 0.5;
    float intensity = u_brightness * (1.0 - t * 0.30);
    frag_color = vec4(u_color * intensity, intensity);
}
