import os, cv2, time, datetime, threading, random, json, asyncio
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from main import CAMERA_RTSP, STATE
# ====== 설정 ======
CAMERA_RTSP = CAMERA_RTSP
print(CAMERA_RTSP)
# WiFi AP 환경이라 끊김 대비
# RECONNECT_DELAY = 2.0
OUTPUT_DIR = Path("recordings")
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI()

templates = Jinja2Templates(directory="templates")
       
class CameraReader:
    def __init__(self, url, cam_id, mode="square", start_delay=0):
        self.start_delay = start_delay
        self.url = url
        self.cam_id = cam_id
        self.mode = mode
        self.frame = None
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._capture_loop, daemon=True).start()

    def _capture_loop(self):
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
        # Stagger startup so cameras don't connect at the same instant
        time.sleep(self.start_delay)
        delay = 2
        while self.running:
            print(f"[CONNECT] {self.cam_id} -> {self.url}")
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            
            if not cap.isOpened():
                cap.release()
                print(f"[FAILED OPEN] {self.cam_id} - retry in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            delay = 2

            fails = 0
            while self.running:
                ret, frame = cap.read()
                if not ret or frame is None:
                    fails += 1
                    if fails > 100:  # ~5s without frames: reconnect
                        print(f"[RECONNECT] {self.cam_id}")
                        break
                    time.sleep(0.05)
                    continue
                fails = 0
                
                # Convert to square for golf full-body
                if self.mode == "square":
                    h, w = frame.shape[:2]
                    size = min(h, w)
                    x1 = (w - size)//2
                    y1 = (h - size)//2
                    frame = frame[y1:y1+size, x1:x1+size]
                    frame = cv2.resize(frame, (640, 640))
                elif self.mode == "portrait":
                    h, w = frame.shape[:2]
                    target_w = int(h * 9/16)
                    x1 = (w - target_w)//2
                    frame = frame[:, x1:x1+target_w]
                    frame = cv2.resize(frame, (360, 640))
                else:
                    frame = cv2.resize(frame, (640, 360))

                with self.lock:
                    self.frame = frame
            
            cap.release()

    def get_jpeg(self):
        with self.lock:
            if self.frame is None:
                return None
            _, buf = cv2.imencode(".jpg", self.frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            return buf.tobytes()


    def to_square(self, frame):
        # 1920x1080 -> center 1080x1080 -> 640x640
        h, w = frame.shape[:2]
        size = min(h, w)
        x1 = (w - size) // 2
        y1 = (h - size) // 2
        square = frame[y1:y1+size, x1:x1+size]
        return cv2.resize(square, (640, 640))

    def to_portrait(self, frame, width_factor=1.0):
        # width_factor = 1.0 -> 360x640 (좁음)
        # width_factor = 1.5 -> 540x640 (이전)
        # width_factor = 1.75 -> 630x640 (추천 - 와이드하면서 비율 정상)
        # width_factor = 2.0 -> 720x640 (더 와이드)
        h, w = frame.shape[:2] # 1080, 1920
        base_w = int(h * 9 / 16) # 607px가 기본 portrait 너비
        target_w = int(base_w * width_factor)
        # 원본 1920보다 넓게 잘라지지 않게 제한
        target_w = min(target_w, w - 20)
        x1 = (w - target_w)//2
        cropped = frame[:, x1:x1+target_w]
        # 비율 유지해서 리사이즈 - 찌그러짐 없음
        # target_w:1080 = out_w:640  비율 그대로
        out_w = int(target_w * 640 / h)
        return cv2.resize(cropped, (out_w, 640))

    def get_frame(self):
        with self.lock:
            return self.frame.copy() if self.frame is not None else None
        
# Usage for your golf setup:
managers = {
    "Camera1": CameraReader(CAMERA_RTSP["Camera1"], "Camera1", mode="square"),
    "Camera2": CameraReader(CAMERA_RTSP["Camera2"], "Camera2", mode="square", start_delay=3),
}


# get_combined에서:
def get_combined_portrait():
    # 각 카메라에서 50% 더 넓게
    # 간단하게는 CameraReader 안에서 width_factor를 1.75로 고정:
    f1 = managers["Camera1"].frame # 이제 630x640
    f2 = managers["Camera2"].frame # 630x640
    if f1 is None or f2 is None:
        return None
    # 630+630 = 1260x640 = 16:9 와이드 모니터에 꽉 차면서 사람 비율 정상
    combined = cv2.hconcat([f1, f2])
    return combined

print(managers)
# 카메라 리더 2개 생성
cameras = list(managers.values())

# 녹화 상태
recording_state = {"active": False, "writer": None, "lock": threading.Lock()}

def _mjpeg_frames(url: str):
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        while cap.isOpened():
            ok, frame = cap.read()
            if not ok:
                break
            frame = cv2.resize(frame, (640, 360))
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ok:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n"
    finally:
        cap.release()  
        
def get_synced_split_frame():
    # 두 카메라 최신 프레임 가져와서 좌우 분할
    frames = []
    for cam in cameras:
        f = cam.get_frame()
        if f is None:
            f = np.zeros((360, 640, 3), dtype=np.uint8)
            cv2.putText(f, f"CAM {cam.cam_id} NO SIGNAL", (20, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,0,255), 2)
        frames.append(cv2.resize(f, (640, 360)))
    
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
    fourcc = cv2.VideoWriter_fourcc(*'avc1')
    filepath = OUTPUT_DIR / f"dual_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    writer = None

    while recording_state["active"]:
        combined = get_combined_portrait() # <-- same function
        if combined is None: 
            time.sleep(0.05)
            continue
        if writer is None:
            h, w = combined.shape[:2] # 640, 1080
            writer = cv2.VideoWriter(str(filepath), fourcc, 20.0, (w, h))
        writer.write(combined)
        time.sleep(0.05)
        
    if writer:
        writer.release()
        print(f"Saved: {filepath}")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "ipcamera_index.html")

def mjpeg_generator():
    while True:
        frame = get_synced_split_frame()
        _, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
        time.sleep(0.04)

def mjpeg_gen_combined():
    while True:
        combined = get_combined_portrait()
        if combined is None:
            time.sleep(0.05)
            continue
        # Horizontal combine -> 1280x640
        # This is perfect square-ish and shows full body for both views
        # combined = cv2.hconcat([f1, f2]) replaced by get_combined_portrait()
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
    if not url:
        return JSONResponse({"error": f"Set CAMERA1_RTSP/CAMERA2_RTSP env for {camera_id}"}, status_code=404)
    try:
        import cv2  # noqa: F401
    except ImportError:
        return JSONResponse({"error": "opencv-python not installed"}, status_code=500)
    return StreamingResponse(_mjpeg_frames(url), media_type="multipart/x-mixed-replace; boundary=frame")


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

  
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ipcamera-server:app", host="127.0.0.1", port=8000)
