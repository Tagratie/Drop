#version 330 core

// Help overlay fragment shader.
// Simple textured-quad sampler. The texture is built once at startup by
// PIL (rounded-rect background + text glyphs) and reused. We rely on
// the host setting straight-alpha blending — SRC_ALPHA, ONE_MINUS_SRC_ALPHA
// — so transparent pixels in the texture show through to the visualizer
// underneath.

in  vec2 v_uv;
out vec4 frag;

uniform sampler2D u_tex;

void main() {
    frag = texture(u_tex, v_uv);
}
