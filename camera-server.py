import os, cv2, time, datetime, threading, random, json, asyncio
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi import WebSocket, WebSocketDisconnect
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from fastapi import WebSocket

from main import camera, _webcam_mjpeg, _mjpeg_frames, CAMERA_RTSP, STATE
# ====== 설정 ======
CAMERA_URLS = [
    os.getenv("CAMERA1_RTSP"),
    os.getenv("CAMERA2_RTSP")
]
print(CAMERA_URLS)
STATE = STATE
CAMERA_RTSP = CAMERA_RTSP
# WiFi AP 환경이라 끊김 대비
RECONNECT_DELAY = 2.0
OUTPUT_DIR = Path("recordings")
OUTPUT_DIR.mkdir(exist_ok=True)


app = FastAPI()

templates = Jinja2Templates(directory="templates")

class CameraReader:
    def __init__(self, rtsp_url, cam_id):
        self.url = rtsp_url
        self.cam_id = cam_id
        self.frame = None
        self.last_time = 0
        self.lock = threading.Lock()
        self.running = True
        self.cap = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _connect(self):
        # FFMPEG 옵션으로 지연 줄이기
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def _loop(self):
        self._connect()
        while self.running:
            if not self.cap or not self.cap.isOpened():
                time.sleep(RECONNECT_DELAY)
                self._connect()
                continue
            
            ret, frame = self.cap.read()
            if not ret:
                print(f"[CAM {self.cam_id}] frame fail, reconnect...")
                self.cap.release()
                time.sleep(RECONNECT_DELAY)
                self._connect()
                continue

            # 720p 정도로 리사이즈해서 부하 줄이기 (원본 유지하고 싶으면 제거)
            frame = cv2.resize(frame, (640, 360))
            
            with self.lock:
                self.frame = frame
                self.last_time = time.time()

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None, self.last_time

# 카메라 리더 2개 생성
cameras = [CameraReader(url, i) for i, url in enumerate(CAMERA_URLS)]

# 녹화 상태
recording_state = {"active": False, "writer": None, "lock": threading.Lock()}

def get_synced_split_frame():
    # 두 카메라 최신 프레임 가져와서 좌우 분할
    frames = []
    for cam in cameras:
        f, t = cam.get_frame()
        if f is None:
            f = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(f, f"CAM {cam.cam_id} NO SIGNAL", (20, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)
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

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")

def mjpeg_generator():
    while True:
        frame = get_synced_split_frame()
        _, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(0.04)

@app.get("/ipcamera", response_class=HTMLResponse)
async def root(request: Request):
    """Main dashboard - Integrated Control Center"""
    return templates.TemplateResponse(request, "ipcamera-viewer.html", {"now": datetime.datetime.now()})

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/camera/{camera_id}/stream")
async def api_camera_stream(camera_id: str):
    """[UPLINK] RTSP -> MJPEG stream for browser <img>"""

    url = CAMERA_RTSP.get(camera_id)
    print(url)
    if not url:
        return JSONResponse({"error": f"Set CAMERA1_RTSP/CAMERA2_RTSP env for {camera_id}"}, status_code=404)
    try:
        import cv2  # noqa: F401
    except ImportError:
        return JSONResponse({"error": "opencv-python not installed"}, status_code=500)
    return StreamingResponse(_mjpeg_frames(url), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/webcam/stream")
async def api_webcam_stream(webcam: int = 0):
    """[UPLINK] USB Webcam -> MJPEG stream for browser <img>"""
    if not camera.isOpened():
        return JSONResponse({"error": "Webcam not available"}, status_code=404)
    return StreamingResponse(_webcam_mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame")

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

@app.get("/api/status")
async def api_all_status():
    """[UPLINK] Terminal -> Server : Get all devices status (Both-way demo)"""
    # Simulate temp drift
    STATE["Temp1"]["temperature"] = round(22.5 + random.random()*3.5, 1)
    STATE["Temp1"]["humidity"] = random.randint(52, 68)
    
    return {
        "timestamp": datetime.datetime.now().isoformat(),
        "direction": "Terminal -> Server (UPLINK)",
        "devices": {
            "Temp1": {
                "temperature_c": STATE["Temp1"]["temperature"],
                "humidity": STATE["Temp1"]["humidity"],
                "battery": 87
            },
            "Door1": {"status": STATE["Door1"]},
            "Door2": {"status": STATE["Door2"]},

            "Switch1": {
                **STATE["Switch1"],
                "current_power_w": random.randint(800,1500) if STATE["Switch1"]["state"]=="ON" else 0
            },
            "Switch2": {
                **STATE["Switch2"],
                "current_power_w": random.randint(300,800) if STATE["Switch2"]["state"]=="ON" else 0
            },
            "Camera1": {"online": True, "motion": random.choice([True, False]), "recording": True},
            "Camera2": {"online": True, "motion": random.choice([True, False]), "recording": True},
        }
    }

# WebSocket for real-time Both-way push
@app.websocket("/ws/bothway")
async def websocket_bothway(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Simulate spontaneous UPLINK every 2 sec
            data = {
                "type": "UPLINK",
                "timestamp": datetime.datetime.now().isoformat(),
                "Temp1": {"temperature": round(22.5 + random.random()*3.5, 1)},
                "Camera1": {"motion": random.choice([True, False])},
                "Camera2": {"motion": random.choice([True, False])},
                "Switch1": {"power_w": random.randint(800,1500) if STATE["Switch1"]["state"]=="ON" else 0}
            }
            await websocket.send_text(json.dumps(data))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass

  
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("camera-server:app", host="127.0.0.1", port=8000, reload=True)