#version 330 core

// Render a GL_POINTS sprite as a soft circular disc.
// gl_PointCoord is in [0,1] within the point's bounding square.

in float v_life;
out vec4 frag_color;

uniform vec3 u_color;          // particle base color, tinted to bloom-friendly hue

void main() {
    vec2 c = gl_PointCoord - vec2(0.5);
    float d = length(c);

    // Discard outside the disc — saves bandwidth vs alpha=0.
    if (d > 0.5) discard;

    // Soft falloff from center. ^2 gives a nice "spark" look that's
    // bright in the middle and dies quickly toward the edge.
    float falloff = pow(1.0 - d * 2.0, 2.0);
    float alpha = falloff * v_life;

    // Additive-friendly premultiplied output.
    frag_color = vec4(u_color * alpha, alpha);
}
