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
    def __init__(self, url, cam_id, mode="square"):
        # mode = "square" or "portrait" or "landscape"
        self.url = url
        self.cam_id = cam_id
        self.mode = mode
        self.frame = None
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def _capture_loop(self):
        while self.running:
            print(f"[CONNECT] {self.cam_id} -> {self.url}")
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            while self.running:
                ret, frame = cap.read()
                if not ret:
                    print(f"[LOST] {self.cam_id}, retry...")
                    cap.release()
                    time.sleep(2)
                    break
                
                # Convert to desired shape
                if self.mode == "square":
                    frame = self.to_square(frame)
                elif self.mode == "portrait":
                    frame = self.to_portrait(frame)
                else: # landscape
                    frame = cv2.resize(frame, (640, 360))

                with self.lock:
                    self.frame = frame
            time.sleep(0.5)

    def to_square(self, frame):
        # 1920x1080 -> center 1080x1080 -> 640x640
        h, w = frame.shape[:2]
        size = min(h, w)
        x1 = (w - size) // 2
        y1 = (h - size) // 2
        square = frame[y1:y1+size, x1:x1+size]
        return cv2.resize(square, (640, 640))

    def to_portrait(self, frame):
        # 1920x1080 -> center 608x1080 (9:16) -> 360x640
        h, w = frame.shape[:2]
        target_w = int(h * 9 / 16)
        x1 = (w - target_w) // 2
        portrait = frame[:, x1:x1+target_w]
        return cv2.resize(portrait, (360, 640))

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None

# Usage for your golf setup:
managers = {
    "Camera1": CameraReader(CAMERA_URLS[0], "Camera1", mode="square"),
    "Camera2": CameraReader(CAMERA_URLS[1], "Camera2", mode="square"),
}
print(managers)
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

def mjpeg_gen_combined():
    while True:
        f1 = managers["Camera1"].frame # 640x640 square
        f2 = managers["Camera2"].frame # 640x640 square
        if f1 is None or f2 is None:
            time.sleep(0.05)
            continue
        # Horizontal combine -> 1280x640
        # This is perfect square-ish and shows full body for both views
        combined = cv2.hconcat([f1, f2]) 
        # Add labels for golf
        cv2.putText(combined, "FRONT", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)
        cv2.putText(combined, "SIDE DTL", (660, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,255,0), 2)
        _, jpeg = cv2.imencode('.jpg', combined, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n'
        time.sleep(0.04)

@app.get("/api/combined/stream")
def combined_stream():
    return StreamingResponse(mjpeg_gen_combined(), media_type="multipart/x-mixed-replace; boundary=frame")  
         
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