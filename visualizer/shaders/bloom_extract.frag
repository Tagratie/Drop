#version 330 core

// Bloom step 1: threshold the scene to extract only the bright stuff.
// Anything below u_threshold goes to black, anything above keeps its color.
// Following passes blur this extracted image, and the final composite adds
// the blurred bright back onto the scene → "glow".

in vec2 v_uv;
out vec4 frag_color;

uniform sampler2D u_tex;
uniform float u_threshold;     // typical 0.5–0.8

void main() {
    vec3 c = texture(u_tex, v_uv).rgb;

    // Use perceived luminance, not max-channel — a saturated red pixel
    // (1,0,0) is dimmer than white (1,1,1), and we want bloom strength
    // to reflect that. Rec.709 weights are the standard choice.
    float lum = dot(c, vec3(0.2126, 0.7152, 0.0722));

    // Soft knee around the threshold so the extraction edge isn't a hard
    // step — smoothstep over a small range looks more cinematic.
    float knee = smoothstep(u_threshold, u_threshold + 0.15, lum);
    frag_color = vec4(c * knee, 1.0);
}
