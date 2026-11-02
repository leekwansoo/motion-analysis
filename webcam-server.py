import cv2, datetime, time, asyncio, os, threading
import numpy as np
from fastapi import FastAPI, Path, Request
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from contextlib import asynccontextmanager
from fastapi import FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup - nothing to do, cameras open lazily
    print("Server starting...")
    yield
    # Shutdown - this replaces @app.on_event("shutdown")
    print("Server shutting down, releasing cameras...")
    for name, cam in _cameras.items():
        try:
            cam.release()
            print(f"Released {name}")
        except:
            pass
    _cameras.clear()
app = FastAPI(lifespan=lifespan)

templates = Jinja2Templates(directory="templates")
# Map names to physical device indexes
# On Windows, test them. It might be 0 and 1, or 1 and 2 if you have a virtual cam.
CAMERA_CONFIG = {
    "WEBCAM1": 0,
    "WEBCAM2": 1,
}
RECONNECT_DELAY = 2.0
OUTPUT_DIR = Path("recordings")
OUTPUT_DIR.mkdir(exist_ok=True)
# Keep 2 cameras alive
_cameras: dict[str, cv2.VideoCapture] = {}
_locks: dict[str, threading.Lock] = {
    "WEBCAM1": threading.Lock(),
    "WEBCAM2": threading.Lock(),
}

def open_camera_with_fallback(index: int):
    # Try order: MSMF with MJPG -> ANY -> DSHOW
    backends = [
        (cv2.CAP_MSMF, "MSMF"),
        (cv2.CAP_ANY, "ANY"),
        (cv2.CAP_DSHOW, "DSHOW"),
    ]
    for backend, name in backends:
        print(f"Trying {name} for index {index}...")
        cam = cv2.VideoCapture(index, backend)
        if not cam.isOpened():
            cam.release()
            continue
        # CRITICAL FIX for your MSMF error:
        # Force MJPG, not YUY2. This fixes -1072875772 with 2 cams
        cam.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M','J','P','G'))
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cam.set(cv2.CAP_PROP_FPS, 30)
        cam.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # Test read
        ret, _ = cam.read()
        if ret:
            print(f"Success with {name} on index {index}")
            return cam
        else:
            print(f"{name} opened but can't grab frame, trying next")
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

def mjpeg_generator(name: str):
    failures = 0
    lock = _locks[name]
    while True:
        with lock:
            cam = get_camera(name)
            if cam is None or not cam.isOpened():
                print(f"{name} is None in generator")
                break
            ret, frame = cam.read()
        if not ret:
            failures += 1
            print(f"[{name}] read fail {failures}/30")
            if failures > 30:
                with lock:
                    if name in _cameras:
                        _cameras[name].release()
                        del _cameras[name]
                # Re-open after 1 sec
                threading.Event().wait(1.0)
                failures = 0
                continue
            threading.Event().wait(0.05)
            continue
        failures = 0
        ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        threading.Event().wait(0.03)

recording_state = {"active": False, "writer": None, "lock": threading.Lock()}

def get_synced_split_frame():
    # 두 카메라 최신 프레임 가져와서 좌우 분할
    cameras = [get_camera("WEBCAM1"), get_camera("WEBCAM2")]
    frames = []
    for cam in cameras:
        ret, f = cam.read()
        if not ret or f is None:
            f = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(f, f"CAM {cam.get(cv2.CAP_PROP_POS_FRAMES)} NO SIGNAL", (20, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)
        frames.append(f)
    
    # 좌우 합치기
    combined = np.hstack(frames)
    # 상단에 타임스탬프
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(combined, ts, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
    if recording_state["active"]:
        cv2.circle(combined, (combined.shape[1]-30, 30), 10, (0,0,255), -1)
    return combined

def recording_loop():
    print("Recording started")
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    filepath = OUTPUT_DIR / f"dual_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    writer = None

    while recording_state["active"]:
        combined = get_synced_split_frame()
        if writer is None:
            h, w = combined.shape[:2]
            writer = cv2.VideoWriter(str(filepath), fourcc, 20.0, (w, h))
            with recording_state["lock"]:
                recording_state["writer"] = writer
        
        writer.write(combined)
        time.sleep(0.05) # 20fps 동기화

    if writer:
        writer.release()
        print(f"Saved: {filepath}")

def synced_mjpeg_generator():
    while True:
        frame = get_synced_split_frame()
        _, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(0.04)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "webcam_index.html")

@app.get("/webcam", response_class=HTMLResponse)
async def webcam(request: Request):
    return templates.TemplateResponse(request, "webcam-viewer.html", {"now": datetime.datetime.now()})
# Generic endpoint: /api/webcam/stream?webcam=0  -> WEBCAM1
#                   /api/webcam/stream?webcam=1  -> WEBCAM2
@app.get("/api/webcam/stream")
async def api_webcam_stream(webcam: int = 0):
    name = "WEBCAM1" if webcam == 0 else "WEBCAM2"
    cam = get_camera(name)
    if cam is None or not cam.isOpened():
        return JSONResponse({"error": f"{name} not available, check if used by another app"}, status_code=404)
    return StreamingResponse(
        mjpeg_generator(name),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

# If you want pretty names:
@app.get("/api/webcam/{cam_name}/stream")
async def api_named_stream(cam_name: str):
    cam_name = cam_name.upper()
    if cam_name not in CAMERA_CONFIG:
        return JSONResponse({"error": "Use WEBCAM1 or WEBCAM2"}, status_code=404)
    cam = get_camera(cam_name)
    if cam is None or not cam.isOpened():
        return JSONResponse({"error": f"{cam_name} failed to open"}, status_code=404)
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