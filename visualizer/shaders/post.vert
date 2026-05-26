#version 330 core

// Pass-through vertex shader for fullscreen post-process passes.
// Caller binds a quad VBO of clip-space positions [-1, 1]; we forward
// uv = pos*0.5+0.5 so frag shaders can sample the previous-pass texture.

in vec2 in_pos;
out vec2 v_uv;

void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
