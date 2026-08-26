import ctypes
import os
import sys
import numpy as np
import sdl2
import openvr

# Lightweight realtime optimizer for jagvr.py
# - caches GL functions
# - uses a single VBO for quad drawing instead of glBegin/glEnd
# - reuses numpy buffers for layer extraction
# - sets GL_UNPACK_ALIGNMENT=1
# - includes a small NorthbridgeShim skeleton to allow RAM patching

LIB_PATH = os.path.join(os.getcwd(), "virtualjaguar_libretro.dll")
ROM_PATH = os.path.join(os.getcwd(), "mc.rom")

if not os.path.exists(LIB_PATH) or not os.path.exists(ROM_PATH):
    print("Error: Missing 'virtualjaguar_libretro.dll' or 'mc.rom'")
    sys.exit(1)

TARGET_FPS = 60.0
TARGET_FRAME_TIME = 1.0 / TARGET_FPS

UPSCALE_FILTER = int(os.environ.get("JAGVR_UPSCALE_FILTER", "0x2601"), 16)  # GL_LINEAR by default
RENDER_SCALE = float(os.environ.get("JAGVR_RENDER_SCALE", "0.85"))

# Depth-layer thresholds
THRESH_MID = 90
THRESH_FRONT = 175
BASE_DEPTH = 2.4
MID_DEPTH = 1.9
FRONT_DEPTH = 1.55
BASE_HEIGHT = 1.3
ZNEAR, ZFAR = 0.05, 50.0

print("Loading ROM into RAM buffers...")
with open(ROM_PATH, "rb") as f:
    raw_rom_bytes = f.read()

rom_size = len(raw_rom_bytes)
rom_buffer = ctypes.create_string_buffer(raw_rom_bytes)
rom_memory_address = ctypes.addressof(rom_buffer)

core = ctypes.CDLL(LIB_PATH)

# --- libretro callbacks --------------------------------------------------
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
        if fb_data is None or ctypes.sizeof(fb_data) != size:
            fb_data = ctypes.create_string_buffer(size)
        ctypes.memmove(fb_data, data, size)
        frame_ready = True

audio_buffer = bytearray()

@ctypes.CFUNCTYPE(None, ctypes.c_int16, ctypes.c_int16)
def audio_sample_cb(left, right):
    global audio_buffer
    # append 16-bit little-endian samples
    audio_buffer.extend(int(left).to_bytes(2, 'little', signed=True))
    audio_buffer.extend(int(right).to_bytes(2, 'little', signed=True))

@ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
def audio_sample_batch_cb(data, frames):
    global audio_buffer
    if data and frames > 0:
        size = frames * 4
        audio_buffer.extend(ctypes.string_at(data, size))
    return frames

# --- Input shim: simple OpenVR -> Jaguar mapping -------------------------
_controller_state = {
    'left_axes': (0.0, 0.0),
    'right_axes': (0.0, 0.0),
    'left_trigger': 0.0,
    'right_trigger': 0.0,
    'left_buttons': 0,
    'right_buttons': 0,
}
_THUMBSTICK_DEADZONE = 0.35

@ctypes.CFUNCTYPE(None)
def input_poll_cb():
    try:
        vr = vr_system
    except NameError:
        return
    s = _controller_state
    s['left_axes'] = (0.0, 0.0)
    s['right_axes'] = (0.0, 0.0)
    s['left_trigger'] = 0.0
    s['right_trigger'] = 0.0
    s['left_buttons'] = 0
    s['right_buttons'] = 0

    for i in range(openvr.k_unMaxTrackedDeviceCount):
        try:
            cls = vr.getTrackedDeviceClass(i)
        except Exception:
            continue
        if cls != openvr.TrackedDeviceClass_Controller:
            continue
        try:
            state = vr.getControllerState(i)
        except Exception:
            try:
                state = vr.getControllerStateWithPose(openvr.TrackingUniverseStanding, i)
            except Exception:
                continue
        if isinstance(state, (tuple, list)):
            state = state[0]
        buttons = getattr(state, 'ulButtonPressed', 0)
        try:
            axes = state.rAxis
        except Exception:
            axes = None
        try:
            role = vr.getControllerRoleForTrackedDeviceIndex(i)
        except Exception:
            role = None
        is_left = (role == openvr.TrackedControllerRole_LeftHand)
        if axes:
            ax0 = getattr(axes[0], 'x', 0.0); ay0 = getattr(axes[0], 'y', 0.0)
            ax1 = getattr(axes[1], 'x', 0.0) if len(axes) > 1 else 0.0
            ay1 = getattr(axes[1], 'y', 0.0) if len(axes) > 1 else 0.0
        else:
            ax0 = ay0 = ax1 = ay1 = 0.0
        if is_left:
            s['left_axes'] = (ax0, ay0)
            s['left_trigger'] = ax1 if abs(ax1) > 0.01 else ay1
            s['left_buttons'] = buttons
        else:
            s['right_axes'] = (ax0, ay0)
            s['right_trigger'] = ax1 if abs(ax1) > 0.01 else ay1
            s['right_buttons'] = buttons

@ctypes.CFUNCTYPE(ctypes.c_int16, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint)
def input_state_cb(port, device, index, id):
    if port != 0:
        return 0
    s = _controller_state
    lx, ly = s['left_axes']
    up = ly < -_THUMBSTICK_DEADZONE
    down = ly > _THUMBSTICK_DEADZONE
    left = lx < -_THUMBSTICK_DEADZONE
    right = lx > _THUMBSTICK_DEADZONE
    a = s['right_trigger'] > 0.45
    b = s['left_trigger'] > 0.45
    if id == 0:
        return 1 if up else 0
    if id == 1:
        return 1 if down else 0
    if id == 2:
        return 1 if left else 0
    if id == 3:
        return 1 if right else 0
    if id == 4:
        return 1 if a else 0
    if id == 5:
        return 1 if b else 0
    if id == 6:
        return 0
    if id == 7:
        return 1 if (s['left_buttons'] or s['right_buttons']) else 0
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
    path=ROM_PATH.encode('utf-8'),
    data=ctypes.cast(rom_memory_address, ctypes.c_void_p),
    size=rom_size,
    meta=None,
)
core.retro_load_game(ctypes.byref(game))

# Memory mapping (if exposed)
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
    print("Warning: core did not expose RETRO_MEMORY_SYSTEM_RAM; northbridge features will be limited")

# Northbridge shim (simple polling-based memory patcher skeleton)
class NorthbridgeShim:
    def __init__(self, ram_ptr, ram_size):
        self.ram_size = ram_size
        self._ram = (ctypes.c_uint8 * ram_size).from_address(ram_ptr)
        # choose scratch area at end of RAM
        self.trampoline_off = ram_size - 0x1000
        self.ipc_flag_off = 0x100  # example
    def read_u32(self, off):
        a = self._ram
        return (a[off] << 24) | (a[off+1] << 16) | (a[off+2] << 8) | a[off+3]
    def write_bytes(self, off, data):
        a = self._ram
        for i, b in enumerate(data):
            a[off + i] = b
    def poll_services(self):
        # check a simple flag and clear it; user code in ROM should set it
        flag = self._ram[self.ipc_flag_off]
        if flag == 1:
            # example: write 'K' to response address
            resp_off = 0x200
            self.write_bytes(resp_off, b'K')
            self._ram[self.ipc_flag_off] = 0

nb = None
if jaguar_ram is not None:
    nb = NorthbridgeShim(jaguar_ram_addr, jaguar_ram_size)

# --- SDL / GL setup -----------------------------------------------------
sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_AUDIO)
sdl2.SDL_GL_SetAttribute(sdl2.SDL_GL_CONTEXT_PROFILE_MASK, sdl2.SDL_GL_CONTEXT_PROFILE_COMPATIBILITY)
# create a logical window size; mirror drawable will match desktop DPI automatically
window = sdl2.SDL_CreateWindow(b"Jaguar VR - Quest 3 / SteamVR", 100, 100, 1280, 720, sdl2.SDL_WINDOW_OPENGL | sdl2.SDL_WINDOW_SHOWN)
gl_context = sdl2.SDL_GL_CreateContext(window)
sdl2.SDL_GL_MakeCurrent(window, gl_context)
sdl2.SDL_GL_SetSwapInterval(0)

# Audio: allow lowering device sample rate and reducing internal buffer size to improve latency/CPU
WANT_SR = int(os.environ.get("JAGVR_AUDIO_RATE", "32000"))  # default to 32 kHz to reduce CPU
SRC_SR = int(os.environ.get("JAGVR_SRC_RATE", "48000"))    # expected core sample rate (override if different)
WANT_SAMPLES = int(os.environ.get("JAGVR_AUDIO_SAMPLES", "256"))
want_spec = sdl2.SDL_AudioSpec(WANT_SR, sdl2.AUDIO_S16LSB, 2, WANT_SAMPLES)
have_spec = sdl2.SDL_AudioSpec(0,0,0,0)
audio_dev = sdl2.SDL_OpenAudioDevice(None, 0, ctypes.byref(want_spec), ctypes.byref(have_spec), 0)
if audio_dev > 0:
    sdl2.SDL_PauseAudioDevice(audio_dev, 0)
    actual_dev_rate = int(getattr(have_spec, 'freq', WANT_SR))
else:
    actual_dev_rate = WANT_SR

# OpenVR
try:
    vr_system = openvr.init(openvr.VRApplication_Scene)
except openvr.OpenVRError as e:
    print(f"Could not start SteamVR: {e}")
    sdl2.SDL_Quit()
    sys.exit(1)
compositor = openvr.VRCompositor()
hmd_w, hmd_h = vr_system.getRecommendedRenderTargetSize()
eye_w, eye_h = int(hmd_w * RENDER_SCALE), int(hmd_h * RENDER_SCALE)

# Load GL functions and constants
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
GL_COLOR_BUFFER_BIT = 0x4000
GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_PROJECTION = 0x1701
GL_MODELVIEW = 0x1700
GL_FRAMEBUFFER = 0x8D40
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_TRIANGLE_STRIP = 0x0005
GL_ARRAY_BUFFER = 0x8892
GL_STATIC_DRAW = 0x88E4
GL_FLOAT = 0x1406
GL_UNPACK_ALIGNMENT = 0x0CF5

opengl32.wglGetProcAddress.restype = ctypes.c_void_p
opengl32.wglGetProcAddress.argtypes = [ctypes.c_char_p]

def get_proc(name):
    addr = opengl32.wglGetProcAddress(name.encode('ascii'))
    if addr:
        return addr
    try:
        exported = getattr(opengl32, name)
    except AttributeError:
        exported = None
    if exported is not None:
        for attr in ('address','value'):
            v = getattr(exported, attr, None)
            if isinstance(v, int) and v!=0:
                return v
        try:
            return ctypes.cast(exported, ctypes.c_void_p).value
        except Exception:
            pass
    raise RuntimeError(f"Could not load GL function '{name}'.")

# Cache function pointers
glGenFramebuffers = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(get_proc('glGenFramebuffers'))
glBindFramebuffer = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(get_proc('glBindFramebuffer'))
glFramebufferTexture2D = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(get_proc('glFramebufferTexture2D'))
glCheckFramebufferStatus = ctypes.WINFUNCTYPE(ctypes.c_uint, ctypes.c_uint)(get_proc('glCheckFramebufferStatus'))

glGenTextures = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(get_proc('glGenTextures'))
glBindTexture = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(get_proc('glBindTexture'))
glTexParameteri = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint, ctypes.c_int)(get_proc('glTexParameteri'))
glTexImage2D = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(get_proc('glTexImage2D'))
glTexSubImage2D = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(get_proc('glTexSubImage2D'))

glViewport = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int)(get_proc('glViewport'))
glClearColor = ctypes.WINFUNCTYPE(None, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float)(get_proc('glClearColor'))
glClear = ctypes.WINFUNCTYPE(None, ctypes.c_uint)(get_proc('glClear'))
glMatrixMode = ctypes.WINFUNCTYPE(None, ctypes.c_uint)(get_proc('glMatrixMode'))
glLoadMatrixf = ctypes.WINFUNCTYPE(None, ctypes.POINTER(ctypes.c_float))(get_proc('glLoadMatrixf'))

# Load glLoadIdentity (with fallback to glLoadMatrixf(identity))
try:
    glLoadIdentity = ctypes.WINFUNCTYPE(None)(get_proc('glLoadIdentity'))
except Exception:
    def glLoadIdentity():
        identity = (ctypes.c_float * 16)(
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0
        )
        glLoadMatrixf(identity)

glEnable = ctypes.WINFUNCTYPE(None, ctypes.c_uint)(get_proc('glEnable'))
glDisable = ctypes.WINFUNCTYPE(None, ctypes.c_uint)(get_proc('glDisable'))
glBlendFunc = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(get_proc('glBlendFunc'))

# VBO functions
glGenBuffers = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.POINTER(ctypes.c_uint))(get_proc('glGenBuffers'))
glBindBuffer = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_uint)(get_proc('glBindBuffer'))
glBufferData = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint)(get_proc('glBufferData'))
glEnableClientState = ctypes.WINFUNCTYPE(None, ctypes.c_uint)(get_proc('glEnableClientState'))
glDisableClientState = ctypes.WINFUNCTYPE(None, ctypes.c_uint)(get_proc('glDisableClientState'))
glVertexPointer = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p)(get_proc('glVertexPointer'))
glTexCoordPointer = ctypes.WINFUNCTYPE(None, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_void_p)(get_proc('glTexCoordPointer'))
glDrawArrays = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_int, ctypes.c_int)(get_proc('glDrawArrays'))

glPixelStorei = ctypes.WINFUNCTYPE(None, ctypes.c_uint, ctypes.c_int)(get_proc('glPixelStorei'))

# Create eye FBOs and textures
def make_texture(w, h, internal_fmt=GL_RGBA):
    tex = ctypes.c_uint(0)
    glGenTextures(1, ctypes.byref(tex))
    glBindTexture(GL_TEXTURE_2D, tex.value)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, UPSCALE_FILTER)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, UPSCALE_FILTER)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glTexImage2D(GL_TEXTURE_2D, 0, internal_fmt, w, h, 0, internal_fmt, GL_UNSIGNED_BYTE, None)
    glBindTexture(GL_TEXTURE_2D, 0)
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

left_vr_tex = openvr.Texture_t(); left_vr_tex.handle = left_tex; left_vr_tex.eType = openvr.TextureType_OpenGL; left_vr_tex.eColorSpace = openvr.ColorSpace_Gamma
right_vr_tex = openvr.Texture_t(); right_vr_tex.handle = right_tex; right_vr_tex.eType = openvr.TextureType_OpenGL; right_vr_tex.eColorSpace = openvr.ColorSpace_Gamma

# Layer textures and preallocated numpy buffers
layer_tex = {}
layer_size = (0,0)
_layer_back = None
_layer_mid = None
_layer_front = None
_np_fb_view = None
_lum = None

def ensure_layer_textures(w,h):
    global layer_tex, layer_size
    if layer_size == (w,h) and layer_tex:
        return
    layer_size = (w,h)
    for name in ('back','mid','front'):
        layer_tex[name] = make_texture(w,h,GL_RGBA)

def ensure_numpy_buffers(w,h):
    global _layer_back, _layer_mid, _layer_front, _lum, _np_fb_view
    size = (h,w,4)
    if _layer_back is None or _layer_back.shape != size:
        _layer_back = np.empty(size, dtype=np.uint8)
        _layer_mid = np.empty(size, dtype=np.uint8)
        _layer_front = np.empty(size, dtype=np.uint8)
        _lum = np.empty((h,w), dtype=np.float32)
    if fb_data is not None:
        buf = (ctypes.c_uint8 * (w*h*4)).from_buffer(fb_data)
        _np_fb_view = np.frombuffer(buf, dtype=np.uint8).reshape(h,w,4)
    else:
        _np_fb_view = None
    return _np_fb_view

def upload_layer(name, arr):
    tex = layer_tex[name]
    glBindTexture(GL_TEXTURE_2D, tex)
    ptr = arr.ctypes.data_as(ctypes.c_void_p)
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, arr.shape[1], arr.shape[0], GL_BGRA, GL_UNSIGNED_BYTE, ptr)
    glBindTexture(GL_TEXTURE_2D, 0)

# build layers with preallocated buffers
def build_depth_layers():
    global _layer_back, _layer_mid, _layer_front, _lum, _np_fb_view
    arr = ensure_numpy_buffers(fb_w, fb_h)
    if arr is None:
        return
    b = arr[...,0].astype(np.float32, copy=False); g = arr[...,1].astype(np.float32, copy=False); r = arr[...,2].astype(np.float32, copy=False)
    _lum[:] = (0.114*b + 0.587*g + 0.299*r)
    _layer_back[:] = arr; _layer_back[...,3] = 255
    np.copyto(_layer_mid, arr)
    mask_mid = (_lum >= THRESH_MID)
    _layer_mid[...,3] = (mask_mid * 255).astype(np.uint8)
    np.copyto(_layer_front, arr)
    mask_front = (_lum >= THRESH_FRONT)
    _layer_front[...,3] = (mask_front * 255).astype(np.uint8)
    ensure_layer_textures(fb_w, fb_h)
    # set pixel store alignment for tight uploads
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
    upload_layer('back', _layer_back)
    upload_layer('mid', _layer_mid)
    upload_layer('front', _layer_front)

# Matrix helpers
def hmd_mat34_to_np(m):
    rows = [[m[r][c] for c in range(4)] for r in range(3)]
    rows.append([0.0,0.0,0.0,1.0])
    return np.array(rows, dtype=np.float64)

def hmd_mat44_to_np(m):
    return np.array([[m[r][c] for c in range(4)] for r in range(4)], dtype=np.float64)

def to_gl(mat4):
    # Robustly accept any array-like with 16 elements and produce a ctypes float[16]
    arr = np.asarray(mat4, dtype=np.float32)
    if arr.size != 16:
        raise ValueError(f"to_gl expects a 4x4 matrix (16 elements), got shape {arr.shape} with {arr.size} elements")
    arr = arr.reshape((4,4))
    flat = arr.T.flatten()
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

LAYERS = (('back', BASE_DEPTH), ('mid', MID_DEPTH), ('front', FRONT_DEPTH))

# Setup a single VBO for a unit quad (triangle strip)
_quad_vbo = ctypes.c_uint(0)
def setup_quad_vbo():
    global _quad_vbo
    if _quad_vbo.value != 0:
        return _quad_vbo.value
    data = np.array([
        -0.5,  0.5, 0.0, 0.0, 0.0,
         0.5,  0.5, 0.0, 1.0, 0.0,
        -0.5, -0.5, 0.0, 0.0, 1.0,
         0.5, -0.5, 0.0, 1.0, 1.0,
    ], dtype=np.float32)
    # Use a single c_uint (not a 1-element array) so ctypes.byref() gives a pointer to c_uint
    buf = ctypes.c_uint(0)
    glGenBuffers(1, ctypes.byref(buf))
    vbo = buf.value
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glBufferData(GL_ARRAY_BUFFER, data.nbytes, data.ctypes.data_as(ctypes.c_void_p), GL_STATIC_DRAW)
    glBindBuffer(GL_ARRAY_BUFFER, 0)
    _quad_vbo = ctypes.c_uint(vbo)
    return vbo

def draw_layers_vbo(view_np):
    vbo = setup_quad_vbo()
    aspect = fb_w / fb_h
    glEnable(GL_BLEND)
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    glEnable(GL_TEXTURE_2D)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    stride = 5 * 4
    glEnableClientState(0x8078)  # GL_TEXTURE_COORD_ARRAY
    glEnableClientState(0x8074)  # GL_VERTEX_ARRAY
    glVertexPointer(3, GL_FLOAT, stride, ctypes.c_void_p(0))
    glTexCoordPointer(2, GL_FLOAT, stride, ctypes.c_void_p(12))

    # view_np should already be a 4x4 numpy matrix; use it directly and ensure shape
    view_arr = np.asarray(view_np, dtype=np.float32)
    if view_arr.size != 16:
        raise ValueError(f"draw_layers_vbo expects a 4x4 view matrix (16 elems), got shape {view_arr.shape} with {view_arr.size} elements")
    view_arr = view_arr.reshape((4,4))

    for name, depth in LAYERS:
        scale = depth / BASE_DEPTH
        half_h = (BASE_HEIGHT * scale) / 2.0
        half_w = half_h * aspect
        model = np.eye(4, dtype=np.float32)
        model[0,0] = 2.0 * half_w
        model[1,1] = 2.0 * half_h
        model[2,3] = -depth
        mv = view_arr @ model
        glLoadMatrixf(to_gl(mv))
        glBindTexture(GL_TEXTURE_2D, layer_tex[name])
        glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
    glBindTexture(GL_TEXTURE_2D, 0)
    glDisableClientState(0x8074)
    glDisableClientState(0x8078)
    glBindBuffer(GL_ARRAY_BUFFER, 0)
    glDisable(GL_BLEND)

def render_eye(fbo, view_gl, proj_gl, w, h):
    glBindFramebuffer(GL_FRAMEBUFFER, fbo)
    glViewport(0, 0, w, h)
    glClearColor(ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(1))
    glClear(GL_COLOR_BUFFER_BIT)
    glMatrixMode(GL_PROJECTION); glLoadMatrixf(proj_gl)
    glMatrixMode(GL_MODELVIEW)
    # convert view_gl ctypes array to numpy 4x4
    view_np = np.array([[view_gl[j*4 + i] for j in range(4)] for i in range(4)], dtype=np.float32).T
    draw_layers_vbo(view_np)
    glBindFramebuffer(GL_FRAMEBUFFER, 0)

def draw_mirror_vbo():
    # Use drawable size (framebuffer pixels) to support DPI scaling on desktop
    win_w, win_h = ctypes.c_int(0), ctypes.c_int(0)
    sdl2.SDL_GL_GetDrawableSize(window, ctypes.byref(win_w), ctypes.byref(win_h))
    drawable_w, drawable_h = win_w.value, win_h.value
    if drawable_w <= 0 or drawable_h <= 0:
        # fallback to window size
        w2, h2 = ctypes.c_int(0), ctypes.c_int(0)
        sdl2.SDL_GetWindowSize(window, ctypes.byref(w2), ctypes.byref(h2))
        drawable_w, drawable_h = w2.value, h2.value

    glViewport(0,0, drawable_w, drawable_h)
    glClearColor(ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(0), ctypes.c_float(1))
    glClear(GL_COLOR_BUFFER_BIT)
    # set projection/modelview to identity
    glMatrixMode(GL_PROJECTION); glLoadIdentity()
    glMatrixMode(GL_MODELVIEW); glLoadIdentity()
    # The unit quad in the VBO is -0.5..0.5; scale it to full screen by loading a model matrix of 2x
    scale_model = np.eye(4, dtype=np.float32)
    scale_model[0,0] = 2.0
    scale_model[1,1] = 2.0
    glLoadMatrixf(to_gl(scale_model))

    glEnable(GL_TEXTURE_2D)
    glBindTexture(GL_TEXTURE_2D, left_tex)
    vbo = setup_quad_vbo()
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glEnableClientState(0x8074); glEnableClientState(0x8078)
    glVertexPointer(3, GL_FLOAT, 20, ctypes.c_void_p(0))
    glTexCoordPointer(2, GL_FLOAT, 20, ctypes.c_void_p(12))
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
    glDisableClientState(0x8074); glDisableClientState(0x8078)
    glBindBuffer(GL_ARRAY_BUFFER, 0)
    glBindTexture(GL_TEXTURE_2D, 0)
    sdl2.SDL_GL_SwapWindow(window)

print(f"HMD recommended render target: {hmd_w}x{hmd_h}  (rendering eyes at {eye_w}x{eye_h})")
print("Running stereo VR loop, optimized for realtime. Press Ctrl+C to exit.")

# set tight pixel store
glPixelStorei(GL_UNPACK_ALIGNMENT, 1)

perf_freq = sdl2.SDL_GetPerformanceFrequency()

# simple audio resampler (linear interpolation) to adapt core source rate -> device rate
def resample_audio(raw_bytes, src_rate, dst_rate):
    if src_rate == dst_rate or len(raw_bytes) == 0:
        return raw_bytes
    import numpy as _np
    arr = _np.frombuffer(raw_bytes, dtype=_np.int16)
    if arr.size < 2:
        return b''
    # ensure stereo
    if arr.size % 2 != 0:
        arr = arr[:-1]
    arr = arr.reshape(-1,2)
    src_len = arr.shape[0]
    dst_len = max(1, int(src_len * float(dst_rate) / float(src_rate)))
    # sample positions
    x = _np.linspace(0, src_len - 1, num=src_len)
    x_new = _np.linspace(0, src_len - 1, num=dst_len)
    left = _np.interp(x_new, x, arr[:,0]).astype(_np.int16)
    right = _np.interp(x_new, x, arr[:,1]).astype(_np.int16)
    out = _np.empty((dst_len * 2,), dtype=_np.int16)
    out[0::2] = left
    out[1::2] = right
    return out.tobytes()

try:
    while True:
        frame_start = sdl2.SDL_GetPerformanceCounter()
        try:
            compositor.waitGetPoses(None, None)
        except Exception:
            pass
        sdl2.SDL_GL_MakeCurrent(window, gl_context)
        core.retro_run()
        if nb is not None:
            nb.poll_services()
        if audio_dev > 0 and len(audio_buffer) > 0:
            # resample to the device's actual rate if needed and queue to SDL
            try:
                raw = bytes(audio_buffer)
                out = resample_audio(raw, SRC_SR, actual_dev_rate)
                if out:
                    sdl2.SDL_QueueAudio(audio_dev, out, len(out))
                audio_buffer.clear()
            except Exception:
                # fallback: queue raw bytes
                try:
                    sdl2.SDL_QueueAudio(audio_dev, bytes(audio_buffer), len(audio_buffer))
                except Exception:
                    pass
                audio_buffer.clear()
        if frame_ready and fb_data:
            frame_ready = False
            build_depth_layers()
            render_eye(left_fbo, left_view_gl, left_proj_gl, eye_w, eye_h)
            render_eye(right_fbo, right_view_gl, right_proj_gl, eye_w, eye_h)
            draw_mirror_vbo()
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
