import ctypes
import os
import sys
import numpy as np
import sdl2
import openvr
import base64

LIB_PATH = os.path.join(os.getcwd(), "virtualjaguar_libretro.dll")
ROM_PATH = os.path.join(os.getcwd(), "mc.rom")

if not os.path.exists(LIB_PATH) or not os.path.exists(ROM_PATH):
    print("Error: Missing 'virtualjaguar_libretro.dll' or 'mc.rom'")
    sys.exit(1)

TARGET_FPS = 60.0
TARGET_FRAME_TIME = 1.0 / TARGET_FPS

UPSCALE_FILTER = 0x2601  # GL_LINEAR (use 0x2600 / GL_NEAREST for crisp pixels)

# Render target scale for each eye FBO, relative to the HMD's recommended
# size. 1.0 = full res. Drop to ~0.8 if Quest 3 over Link/Air Link can't
# hold 60fps with three overdrawn layers.
RENDER_SCALE = 1.0

# --- Faux depth-layer settings -------------------------------------------
THRESH_MID = 90
THRESH_FRONT = 175
BASE_DEPTH = 2.4
MID_DEPTH = 1.9
FRONT_DEPTH = 1.55
BASE_HEIGHT = 1.3
ZNEAR, ZFAR = 0.05, 50.0

print("Pre-compiling ROM into high-speed RAM buffers for zero-latency execution...")
with open(ROM_PATH, "rb") as f:
    raw_rom_bytes = f.read()

rom_size = len(raw_rom_bytes)
rom_buffer = ctypes.create_string_buffer(raw_rom_bytes)
rom_memory_address = ctypes.addressof(rom_buffer)

core = ctypes.CDLL(LIB_PATH)


@ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_uint, ctypes.c_void_p)
def env_cb(cmd, data):
    if cmd == 10:  # RETRO_ENVIRONMENT_SET_PIXEL_FORMAT
        ctypes.cast(data, ctypes.POINTER(ctypes.c_int))[0] = 1  # XRGB8888
        return True
    elif cmd == 9:  # RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY
        ctypes.cast(data, ctypes.POINTER(ctypes.c_char_p))[0] = os.getcwd().encode("utf-8")
        return True
    return False


fb_data = None
fb_w, fb_h = 320, 240
frame_ready = False


@ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t)
def video_cb(data, width, height, pitch):
    global fb_data, fb_w, fb_h, frame_ready
    if data and width > 0 and height > 0:
        fb_w, fb_h = width, height
        size = width * height * 4
        if fb_data is None or len(fb_data) != size:
            fb_data = ctypes.create_string_buffer(size)
        ctypes.memmove(fb_data, data, size)
        frame_ready = True


audio_buffer = bytearray()


@ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
def audio_sample_cb(left, right):
    global audio_buffer
    audio_buffer.extend(left.to_bytes(2, "little", signed=True))
    audio_buffer.extend(right.to_bytes(2, "little", signed=True))


@ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
def audio_sample_batch_cb(data, frames):
    global audio_buffer
    if data and frames > 0:
        size = frames * 4
        audio_buffer.extend(ctypes.string_at(data, size))
    return frames


# --- Input: improved OpenVR controller mapping for Quest 3 ---------------
# We implement a simple poller that reads OpenVR controller states and
# synthesizes a classic Jaguar controller mapping. This is best-effort and
# may need tuning for your setup. The plumbing below exposes button states
# to libretro via input_state_cb.

_controller_state = {
    'left_axes': (0.0, 0.0),
    'right_axes': (0.0, 0.0),
    'left_trigger': 0.0,
    'right_trigger': 0.0,
    'left_grip': False,
    'right_grip': False,
    'left_buttons': 0,
    'right_buttons': 0,
}

# Deadzone for thumbsticks
_THUMBSTICK_DEADZONE = 0.35


@ctypes.CFUNCTYPE(None)
def input_poll_cb():
    # Poll OpenVR controllers and fill _controller_state
    try:
        vr = vr_system
    except NameError:
        return
    # reset
    s = _controller_state
    s['left_axes'] = (0.0, 0.0)
    s['right_axes'] = (0.0, 0.0)
    s['left_trigger'] = 0.0
    s['right_trigger'] = 0.0
    s['left_grip'] = False
    s['right_grip'] = False
    s['left_buttons'] = 0
    s['right_buttons'] = 0

    # Iterate tracked devices and sample controller states
    for i in range(openvr.k_unMaxTrackedDeviceCount):
        try:
            cls = vr.getTrackedDeviceClass(i)
        except Exception:
            continue
        if cls != openvr.TrackedDeviceClass_Controller:
            continue
        # Some openvr bindings return a (state, pose) tuple; others return state
        try:
            state = vr.getControllerState(i)
        except Exception:
            # try the alternative accessor
            try:
                state = vr.getControllerStateWithPose(openvr.TrackingUniverseStanding, i)
            except Exception:
                continue
        # unwrap tuple if necessary
        if isinstance(state, tuple) or isinstance(state, list):
            state = state[0]
        # state should have ulButtonPressed and rAxis entries
        buttons = getattr(state, 'ulButtonPressed', 0)
        # axes are in rAxis: each axis has x,y floats
        try:
            axes = state.rAxis
        except Exception:
            axes = None
        # decide handedness by pose role if available
        try:
            role = vr.getControllerRoleForTrackedDeviceIndex(i)
        except Exception:
            # fallback unknown: try to assign first controller -> left, second -> right
            role = None
        is_left = (role == openvr.TrackedControllerRole_LeftHand)
        # map axes and triggers depending on controller implementation
        # Many SteamVR drivers put trigger pressure in rAxis[1].x or rAxis[1].y
        if axes:
            ax0 = getattr(axes[0], 'x', 0.0)
            ay0 = getattr(axes[0], 'y', 0.0)
            # try axis 1 for trigger/grip if present
            ax1 = getattr(axes[1], 'x', 0.0) if len(axes) > 1 else 0.0
            ay1 = getattr(axes[1], 'y', 0.0) if len(axes) > 1 else 0.0
        else:
            ax0 = ay0 = ax1 = ay1 = 0.0
        if is_left:
            # thumbstick on left
            s['left_axes'] = (ax0, ay0)
            s['left_trigger'] = ax1 if abs(ax1) > 0.01 else ay1
            s['left_buttons'] = buttons
        else:
            s['right_axes'] = (ax0, ay0)
            s['right_trigger'] = ax1 if abs(ax1) > 0.01 else ay1
            s['right_buttons'] = buttons


# Map synthesized controller state to libretro input_state calls.
# libretro's input_state signature: (port, device, index, id) -> int16
# We implement a simple mapping: port 0 -> player 1. device ignored.
# id mapping (choice):
# 0 = up, 1 = down, 2 = left, 3 = right, 4 = A (primary), 5 = B (secondary),
# 6 = Option/Secondary keypad, 7 = Pause


@ctypes.CFUNCTYPE(ctypes.c_int16, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)
def input_state_cb(port, device, index, id):
    # Only support player 0
    if port != 0:
        return 0
    s = _controller_state
    # digital D-pad from left thumbstick
    lx, ly = s['left_axes']
    up = ly < -_THUMBSTICK_DEADZONE
    down = ly > _THUMBSTICK_DEADZONE
    left = lx < -_THUMBSTICK_DEADZONE
    right = lx > _THUMBSTICK_DEADZONE
    # primary/secondary mapped to right trigger & left trigger
    a_pressed = s['right_trigger'] > 0.45 or (s['right_buttons'] & getattr(openvr, 'ButtonMaskFromId', 0))
    b_pressed = s['left_trigger'] > 0.45
    # grip presses as a modifier
    opt = s['left_grip'] or s['right_grip']

    if id == 0:
        return 1 if up else 0
    if id == 1:
        return 1 if down else 0
    if id == 2:
        return 1 if left else 0
    if id == 3:
        return 1 if right else 0
    if id == 4:
        return 1 if a_pressed else 0
    if id == 5:
        return 1 if b_pressed else 0
    if id == 6:
        return 1 if opt else 0
    if id == 7:
        # map menu button (application menu) if present on controller buttons
        # try to detect application menu bit
        appmenu_mask = getattr(openvr, 'k_EButton_ApplicationMenu', None)
        if appmenu_mask is not None:
            return 1 if (s['left_buttons'] & appmenu_mask or s['right_buttons'] & appmenu_mask) else 0
        return 0
    return 0


core.retro_set_environment(env_cb)
core.retro_set_video_refresh(video_cb)
core.retro_set_audio_sample(audio_sample_cb)
core.retro_set_audio_sample_batch(audio_sample_batch_cb)
core.retro_set_input_poll(input_poll_cb)
core.retro_set_input_state(input_state_cb)
core.retro_init()


class RetroGameInfo(ctypes.Structure):
    _fields_ = [
        ("path", ctypes.c_char_p),
        ("data", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
        ("meta", ctypes.c_char_p),
    ]


game = RetroGameInfo(
    path=ROM_PATH.encode("utf-8"),
    data=ctypes.cast(rom_memory_address, ctypes.c_void_p),
    size=rom_size,
    meta=None,
)
core.retro_load_game(ctypes.byref(game))

# --- Real core memory access (legitimate libretro API, not a DLL hack) ----
RETRO_MEMORY_SAVE_RAM = 0
RETRO_MEMORY_SYSTEM_RAM = 2

core.retro_get_memory_data.restype = ctypes.c_void_p
core.retro_get_memory_data.argtypes = [ctypes.c_uint]
core.retro_get_memory_size.restype = ctypes.c_size_t
core.retro_get_memory_size.argtypes = [ctypes.c_uint]

jaguar_ram_addr = core.retro_get_memory_data(RETRO_MEMORY_SYSTEM_RAM)
jaguar_ram_size = core.retro_get_memory_size(RETRO_MEMORY_SYSTEM_RAM)
jaguar_ram = None
if jaguar_ram_addr and jaguar_ram_size:
    jaguar_ram = (ctypes.c_uint8 * jaguar_ram_size).from_address(jaguar_ram_addr)
    print(f"Jaguar system RAM mapped: {jaguar_ram_size} bytes at {hex(jaguar_ram_addr)}")
else:
    print("Warning: core did not expose RETRO_MEMORY_SYSTEM_RAM; falling back to luminance-only layers.")

# --- SDL / GL setup ---------------------------------------------------
sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_AUDIO)
sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_PROFILE_MASK, sdl2.SDL_GL_CONTEXT_PROFILE_COMPATIBILITY)

window = sdl2.SDL_CreateWindow(
    b"Jaguar VR - Quest 3 / SteamVR",
    100, 100, 640, 480,
    sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_SHOWN,
)
gl_context = sdl2.SDL_GL_CreateContext(window)
sdl2.SDL_GL_MakeCurrent(window, gl_context)
sdl2.SDL_GL_SetSwapInterval(0)  # compositor.waitGetPoses() paces us; don't double up on vsync

want_spec = sdl2.SDL_AudioSpec(48000, sdl2.AUDIO_S16LSB, 2, 512)
have_spec = sdl2.SDL_AudioSpec(0, 0, 0, 0)
audio_dev = sdl2.SDL_OpenAudioDevice(None, 0, ctypes.byref(want_spec), ctypes.byref(have_spec), 0)
if audio_dev > 0:
    sdl2.SDL_PauseAudioDevice(audio_dev, 0)

# --- OpenVR setup -------------------------------------------------------
try:
    vr_system = openvr.init(openvr.VRApplication_Scene)
except openvr.OpenVRError as e:
    print(f"Could not start SteamVR: {e}")
    print("Make sure SteamVR is running and a headset (Quest 3 via Link/Air Link, "
          "or any SteamVR-tracked headset) is connected.")
    sdl2.SDL_Quit()
    sys.exit(1)

compositor = openvr.VRCompositor()
hmd_w, hmd_h = vr_system.getRecommendedRenderTargetSize()
eye_w, eye_h = int(hmd_w * RENDER_SCALE), int(hmd_h * RENDER_SCALE)

# --- Raw GL constants / extension loading --------------------------------
opengl32 = ctypes.windll.opengl32

GL_TEXTURE_2D = 0x0DE1
GL_UNSIGNED_BYTE = 0x1401
GL_BGRA = 0x80E1
GL_RGBA = 0x1908
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_QUADS = 0x0007
GL_COLOR_BUFFER_BIT = 0x4000
GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_PROJECTION = 0x1701
GL_MODELVIEW = 0x1700
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5

opengl32.wglGetProcAddress.restype = ctypes.c_void_p
opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]


def get_proc(name):
    # Try the normal wglGetProcAddress first (requires a current GL context).
    addr = opengl32.wglGetProcAddress(name.encode("ascii"))
    if addr:
        return addr
    # Fall back to an exported symbol on opengl32.dll (some drivers do this).
    try:
        exported = getattr(opengl32, name)
    except AttributeError:
        exported = None
    if exported is not None:
        for attr in ("address", "value"):
            addr_val = getattr(exported, attr, None)
            if isinstance(addr_val, int) and addr_val != 0:
                return addr_val
        try:
            return ctypes.cast(exported, ctypes.c_void_p).value
        except Exception:
            pass
    raise RuntimeError(
        f"Could not load GL function '{name}'. Ensure an OpenGL context is current "
        f"and your GPU driver supports the required functions (GL_ARB_framebuffer_object, etc.)."
    )


glGenFramebuffers = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(get_proc("glGenFramebuffers"))
glBindFramebuffer = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(get_proc("glBindFramebuffer"))
glFramebufferTexture2D = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(get_proc("glFramebufferTexture2D"))
glCheckFramebufferStatus = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_uint)(get_proc("glCheckFramebufferStatus"))


def make_texture(w, h, internal_fmt=GL_RGBA):
    tex = ctypes.c_uint(0)
    opengl32.glGenTextures(1, ctypes.byref(tex))
    opengl32.glBindTexture(GL_TEXTURE_2D, tex.value)
    opengl32.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, UPSCALE_FILTER)
    opengl32.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, UPSCALE_FILTER)
    opengl32.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    opengl32.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    opengl32.glTexImage2D(GL_TEXTURE_2D, 0, internal_fmt, w, h, 0, internal_fmt, GL_UNSIGNED_BYTE, None)
    opengl32.glBindTexture(GL_TEXTURE_2D, 0)
    return tex.value


def make_eye_fbo(w, h):
    color_tex = make_texture(w, h, GL_RGBA)
    fbo = ctypes.c_uint(0)
    glGenFramebuffers(1, ctypes.byref(fbo))
    glBindFramebuffer(GL_FRAMEBUFFER, fbo.value)
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, color_tex, 0)
    status = glCheckFramebufferStatus(GL_FRAMEBUFFER)
    glBindFramebuffer(GL_FRAMEBUFFER, 0)
    if status != GL_FRAMEBUFFER_COMPLETE:
        raise RuntimeError(f"Eye framebuffer incomplete (status 0x{status:X})")
    return fbo.value, color_tex


left_fbo, left_tex = make_eye_fbo(eye_w, eye_h)
right_fbo, right_tex = make_eye_fbo(eye_w, eye_h)

eye_bounds = openvr.VRTextureBounds_t()
eye_bounds.uMin, eye_bounds.uMax = 0.0, 1.0
eye_bounds.vMin, eye_bounds.vMax = 0.0, 1.0

left_vr_tex = openvr.Texture_t()
left_vr_tex.handle = left_tex
left_vr_tex.eType = openvr.TextureType_OpenGL
left_vr_tex.eColorSpace = openvr.ColorSpace_Gamma

right_vr_tex = openvr.Texture_t()
right_vr_tex.handle = right_tex
right_vr_tex.eType = openvr.TextureType_OpenGL
right_vr_tex.eColorSpace = openvr.ColorSpace_Gamma

# --- Depth-layer textures -------------------------------------------------
layer_tex = {}   # name -> gl texture id
layer_size = (0, 0)


def ensure_layer_textures(w, h):
    global layer_tex, layer_size
    if layer_size == (w, h) and layer_tex:
        return
    layer_size = (w, h)
    for name in ("back", "mid", "front"):
        layer_tex[name] = make_texture(w, h, GL_RGBA)


def upload_layer(name, arr):
    tex = layer_tex[name]
    opengl32.glBindTexture(GL_TEXTURE_2D, tex)
    ptr = arr.ctypes.data_as(ctypes.c_void_p)
    opengl32.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, arr.shape[1], arr.shape[0],
                              GL_BGRA, GL_UNSIGNED_BYTE, ptr)
    opengl32.glBindTexture(GL_TEXTURE_2D, 0)


def build_depth_layers():
    arr = np.frombuffer(fb_data, dtype=np.uint8).reshape(fb_h, fb_w, 4)  # B,G,R,X
    b = arr[..., 0].astype(np.float32)
    g = arr[..., 1].astype(np.float32)
    r = arr[..., 2].astype(np.float32)
    lum = 0.114 * b + 0.587 * g + 0.299 * r

    back = arr.copy()
    back[..., 3] = 255

    mid = arr.copy()
    mid[..., 3] = np.where(lum >= THRESH_MID, 255, 0).astype(np.uint8)

    front = arr.copy()
    front[..., 3] = np.where(lum >= THRESH_FRONT, 255, 0).astype(np.uint8)

    ensure_layer_textures(fb_w, fb_h)
    upload_layer("back", back)
    upload_layer("mid", mid)
    upload_layer("front", front)


# --- Matrix helpers --------------------------------------------------------
def hmd_mat34_to_np(m):
    rows = [[m[r][c] for c in range(4)] for r in range(3)]
    rows.append([0.0, 0.0, 0.0, 1.0])
    return np.array(rows, dtype=np.float64)

def hmd_mat44_to_np(m):
    return np.array([[m[r][c] for c in range(4)] for r in range(4)], dtype=np.float64)

def to_gl(mat4):
    flat = mat4.T.astype(np.float32).flatten()
    return (ctypes.c_float * 16)(*flat)

def eye_view_matrix(eye):
    eye_to_head = hmd_mat34_to_np(vr_system.getEyeToHeadTransform(eye).m)
    return np.linalg.inv(eye_to_head)

def eye_projection_matrix(eye):
    return hmd_mat44_to_np(vr_system.getProjectionMatrix(eye, ZNEAR, ZFAR).m)

left_view_gl = to_gl(eye_view_matrix(openvr.Eye_Left))
right_view_gl = to_gl(eye_view_matrix(openvr.Eye_Right))
left_proj_gl = to_gl(eye_projection_matrix(openvr.Eye_Left))
right_proj_gl = to_gl(eye_projection_matrix(openvr.Eye_Right))

LAYERS = (("back", BASE_DEPTH), ("mid", MID_DEPTH), ("front", FRONT_DEPTH))

def draw_layers():
    aspect = fb_w / fb_h
    opengl32.glEnable(GL_BLEND)
    opengl32.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    opengl32.glEnable(GL_TEXTURE_2D)

    for name, depth in LAYERS:
        scale = depth / BASE_DEPTH
        half_h = (BASE_HEIGHT * scale) / 2.0
        half_w = half_h * aspect

        opengl32.glBindTexture(GL_TEXTURE_2D, layer_tex[name])
        opengl32.glBegin(GL_QUADS)
        opengl32.glTexCoord2f(ctypes.c_float(0), ctypes.c_float(0))
        opengl32.glVertex3f(ctypes.c_float(-half_w), ctypes.c_float(half_h), ctypes.c_float(-depth))
        opengl32.glTexCoord2f(ctypes.c_float(1), ctypes.c_float(0))
        opengl32.glVertex3f(ctypes.c_float(half_w), ctypes.c_float(half_h), ctypes.c_float(-depth))
        opengl32.glTexCoord2f(ctypes.c_float(1), ctypes.c_float(1))
        opengl32.glVertex3f(ctypes.c_float(half_w), ctypes.c_float(-half_h), ctypes.c_float(-depth))
        opengl32.glTexCoord2f(ctypes.c_float(0), ctypes.c_float(1))
        opengl32.glVertex3f(ctypes.c_float(-half_w), ctypes.c_float(-half_h), ctypes.c_float(-depth))
        opengl32.glEnd()

    opengl32.glBindTexture(GL_TEXTURE_2D, 0)
    opengl32.glDisable(GL_BLEND)


def render_eye(fbo, view_gl, proj_gl, w, h):
    glBindFramebuffer(GL_FRAMEBUFFER, fbo)
    opengl32.glViewport(0, 0, w, h)
    opengl32.glClearColor(ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(1))
    opengl32.glClear(GL_COLOR_BUFFER_BIT)

    opengl32.glMatrixMode(GL_PROJECTION)
    opengl32.glLoadMatrixf(proj_gl)
    opengl32.glMatrixMode(GL_MODELVIEW)
    opengl32.glLoadMatrixf(view_gl)

    draw_layers()

    glBindFramebuffer(GL_FRAMEBUFFER, 0)


def draw_mirror():
    win_w, win_h = ctypes.c_int(0), ctypes.c_int(0)
    sdl2.SDL_GetWindowSize(window, ctypes.byref(win_w), ctypes.byref(win_h))
    opengl32.glViewport(0, 0, win_w.value, win_h.value)
    opengl32.glClearColor(ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(1))
    opengl32.glClear(GL_COLOR_BUFFER_BIT)
    opengl32.glMatrixMode(GL_PROJECTION)
    opengl32.glLoadIdentity()
    opengl32.glMatrixMode(GL_MODELVIEW)
    opengl32.glLoadIdentity()
    opengl32.glEnable(GL_TEXTURE_2D)
    opengl32.glBindTexture(GL_TEXTURE_2D, left_tex)
    opengl32.glBegin(GL_QUADS)
    opengl32.glTexCoord2f(ctypes.c_float(0), ctypes.c_float(0)); opengl32.glVertex2f(ctypes.c_float(-1), ctypes.c_float(-1))
    opengl32.glTexCoord2f(ctypes.c_float(1), ctypes.c_float(0)); opengl32.glVertex2f(ctypes.c_float(1), ctypes.c_float(-1))
    opengl32.glTexCoord2f(ctypes.c_float(1), ctypes.c_float(1)); opengl32.glVertex2f(ctypes.c_float(1), ctypes.c_float(1))
    opengl32.glTexCoord2f(ctypes.c_float(0), ctypes.c_float(1)); opengl32.glVertex2f(ctypes.c_float(-1), ctypes.c_float(1))
    opengl32.glEnd()
    opengl32.glBindTexture(GL_TEXTURE_2D, 0)
    sdl2.SDL_GL_SwapWindow(window)

print(f"HMD recommended render target: {hmd_w}x{hmd_h}  (rendering eyes at {eye_w}x{eye_h})")
print("Running stereo VR loop with faux depth-layer parallax, targeting 60fps. Press Ctrl+C to exit.")

perf_freq = sdl2.SDL_GetPerformanceFrequency()

try:
    while True:
        frame_start = sdl2.SDL_GetPerformanceCounter()

        try:
            compositor.waitGetPoses(None, None)
        except Exception:
            pass

        sdl2.SDL_GL_MakeCurrent(window, gl_context)
        core.retro_run()

        if audio_dev > 0 and len(audio_buffer) > 0:
            sdl2.SDL_QueueAudio(audio_dev, bytes(audio_buffer), len(audio_buffer))
            audio_buffer.clear()

        if frame_ready and fb_data:
            frame_ready = False
            build_depth_layers()

            render_eye(left_fbo, left_view_gl, left_proj_gl, eye_w, eye_h)
            render_eye(right_fbo, right_view_gl, right_proj_gl, eye_w, eye_h)

            draw_mirror()

        compositor.submit(openvr.Eye_Left, left_vr_tex, eye_bounds, 0)
        compositor.submit(openvr.Eye_Right, right_vr_tex, eye_bounds, 0)
        compositor.postPresentHandoff()

        elapsed = (sdl2.SDL_GetPerformanceCounter() - frame_start) / perf_freq
        remaining = TARGET_FRAME_TIME - elapsed
        if remaining > 0:
            sdl2.SDL_Delay(int(remaining * 1000))

except (KeyboardInterrupt, SystemExit):
    pass
finally:
    if audio_dev > 0:
        sdl2.SDL_CloseAudioDevice(audio_dev)
    openvr.shutdown()
    sdl2.SDL_GL_DeleteContext(gl_context)
    sdl2.SDL_DestroyWindow(window)
    sdl2.SDL_Quit()
    core.retro_deinit()
