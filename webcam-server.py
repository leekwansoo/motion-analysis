import cv2, datetime, time, os, threading
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Server starting...")
    yield
    print("Server shutting down...")
    for name, cam in _cameras.items():
        try: cam.release()
        except: pass
    _cameras.clear()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

CAMERA_CONFIG = {"WEBCAM1": 0, "WEBCAM2": 1}
OUTPUT_DIR = Path("recordings")
OUTPUT_DIR.mkdir(exist_ok=True)

_cameras: dict[str, cv2.VideoCapture] = {}
_locks: dict[str, threading.Lock] = {
    "WEBCAM1": threading.Lock(),
    "WEBCAM2": threading.Lock(),
}

# --- NEW: Portrait frame adjust (same as Tapo IP version) ---
def to_portrait_wide(frame, width_factor=1.75):
    """width_factor 1.0=360x640, 1.75=630x640 추천, 2.0=720x640 더 와이드 - 비율 유지, 뚱뚱해지지 않음"""
    h, w = frame.shape[:2]
    base_w = int(h * 9 / 16)
    target_w = int(base_w * width_factor)
    target_w = min(target_w, w - 10)
    x1 = (w - target_w)//2
    cropped = frame[:, x1:x1+target_w]
    out_w = int(target_w * 640 / h)
    return cv2.resize(cropped, (out_w, 640))

def to_square(frame):
    h, w = frame.shape[:2]
    size = min(h, w)
    x1 = (w - size)//2
    y1 = (h - size)//2
    square = frame[y1:y1+size, x1:x1+size]
    return cv2.resize(square, (640, 640))

# --- Camera open (same as yours) ---
def open_camera_with_fallback(index: int):
    backends = [(cv2.CAP_MSMF, "MSMF"), (cv2.CAP_ANY, "ANY"), (cv2.CAP_DSHOW, "DSHOW")]
    for backend, name in backends:
        print(f"Trying {name} for index {index}...")
        cam = cv2.VideoCapture(index, backend)
        if not cam.isOpened():
            cam.release()
            continue
        cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cam.set(cv2.CAP_PROP_FPS, 30)
        cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        ret, _ = cam.read()
        if ret:
            print(f"Success with {name} on index {index}")
            return cam
        cam.release()
    return None

def get_camera(name: str):
    cam = _cameras.get(name)
    if cam is None or not cam.isOpened():
        idx = CAMERA_CONFIG[name]
        cam = open_camera_with_fallback(idx)
        if cam:
            _cameras[name] = cam
    return cam

# --- NEW: Combined portrait (same logic as IP camera) ---
def get_synced_split_frame(mode="portrait_wide"):
    cams = [get_camera("WEBCAM1"), get_camera("WEBCAM2")]
    frames = []
    for cam in cams:
        ret, f = cam.read()
        if not ret or f is None:
            f = np.zeros((720, 1280, 3), dtype=np.uint8)
        # Apply portrait wide adjust
        if mode == "portrait_wide":
            f = to_portrait_wide(f, width_factor=1.75) # 630x640
        elif mode == "square":
            f = to_square(f) # 640x640
        else:
            f = cv2.resize(f, (640, 360))
        frames.append(f)
    
    combined = np.hstack(frames) # 1260x640 (630+630) = 16:9 와이드, 비율 정상
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(combined, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)
    cv2.putText(combined, "WEBCAM1", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)
    cv2.putText(combined, "WEBCAM2", (combined.shape[1]//2 + 10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)
    if recording_state["active"]:
        cv2.circle(combined, (combined.shape[1]-20, 20), 8, (0,0,255), -1)
    return combined

def mjpeg_generator(name: str):
    lock = _locks[name]
    while True:
        with lock:
            cam = get_camera(name)
            if cam is None or not cam.isOpened():
                break
            ret, frame = cam.read()
        if not ret:
            time.sleep(0.05)
            continue
        # Individual stream also portrait wide
        frame = to_portrait_wide(frame, width_factor=1.75)
        ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ret: continue
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(0.03)

recording_state = {"active": False, "writer": None}

def recording_loop():
    print("Recording started")
    fourcc = cv2.VideoWriter_fourcc(*'avc1')
    filepath = OUTPUT_DIR / f"dual_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    writer = None
    while recording_state["active"]:
        combined = get_synced_split_frame(mode="portrait_wide")
        if writer is None:
            h, w = combined.shape[:2]
            print(f"Writer {w}x{h}")
            writer = cv2.VideoWriter(str(filepath), fourcc, 20.0, (w, h))
            if not writer.isOpened():
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                writer = cv2.VideoWriter(str(filepath), fourcc, 20.0, (w, h))
        writer.write(combined)
        time.sleep(0.05)
    if writer:
        writer.release()
        print(f"Saved: {filepath}")

def synced_mjpeg_generator():
    while True:
        frame = get_synced_split_frame(mode="portrait_wide")
        _, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(0.04)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "webcam_index.html")

@app.get("/webcam", response_class=HTMLResponse)
async def webcam(request: Request):
    return templates.TemplateResponse(request, "webcam-viewer.html", {"now": datetime.datetime.now()})

@app.get("/api/webcam/{cam_name}/stream")
async def api_named_stream(cam_name: str):
    cam_name = cam_name.upper()
    if cam_name not in CAMERA_CONFIG:
        return JSONResponse({"error": "Use WEBCAM1 or WEBCAM2"}, status_code=404)
    cam = get_camera(cam_name)
    if cam is None or not cam.isOpened():
        return JSONResponse({"error": f"{cam_name} failed"}, status_code=404)
    return StreamingResponse(mjpeg_generator(cam_name), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(synced_mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.post("/start_recording")
async def start_recording():
    if not recording_state["active"]:
        recording_state["active"] = True
        threading.Thread(target=recording_loop, daemon=True).start()
    return {"status": "recording"}

@app.post("/stop_recording")
async def stop_recording():
    recording_state["active"] = False
    return {"status": "stopped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webcam-server:app", host="127.0.0.1", port=8000, reload=False)