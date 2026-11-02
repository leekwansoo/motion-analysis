"""
main.py - FastAPI + Jinja2 API Gateway for IoT Both-way Dashboard
- Serves client .html via Jinja2 templates
- Proxies FastMCP Server tools
- Integrated Camera, Sensors, Lights, Doors, Plugs

Run: uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import asyncio, base64, cv2, json, random, os
import datetime
from pathlib import Path
from dotenv import load_dotenv

# Import our FastMCP logic (reuse from server.py)
# For demo, we embed state here - in real project, import from server.py

from main import STATE, CAMERA_RTSP, _mjpeg_frames, _webcam_mjpeg, get_usb_camera, get_usb_camera_index
from usb_device import list_usb_devices
app = FastAPI(title="IoT Gateway API - FastAPI + Jinja2", version="1.0")

# Create directories
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

# Load environment variables from .env
load_dotenv()

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static if exists
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ===================== API Gateway - Both-way Tools =====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Main dashboard - Integrated Control Center"""
    return templates.TemplateResponse(request, "integrated-dashboard.html", {"now": datetime.datetime.now()})

@app.get("/ipcamera", response_class=HTMLResponse)
async def root(request: Request):
    """Main dashboard - Integrated Control Center"""
    return templates.TemplateResponse(request, "ipcamera-viewer.html", {"now": datetime.datetime.now()})

@app.get("/basic", response_class=HTMLResponse)
async def basic_dashboard(request: Request):
    """Basic Both-way Table dashboard"""
    return templates.TemplateResponse(request, "both-way-dashboard.html", {"now": datetime.datetime.now()})

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

@app.post("/api/control/door")
async def api_control_door(payload: dict):
    """[DOWNLINK] Server -> Terminal : Door OPEN/CLOSE"""
    door_id = payload.get("door_id", "Door1")
    command = payload.get("command", "OPEN").upper()
    
    if door_id not in STATE:
        return JSONResponse({"error": "Invalid door_id"}, status_code=400)
    
    STATE[door_id] = "OPEN" if command == "OPEN" else "CLOSED"
    
    return {
        "result": "OK",
        "door_id": door_id,
        "command": command,
        "new_status": STATE[door_id],
        "timestamp": datetime.datetime.now().isoformat(),
        "direction": "Server -> Terminal (DOWNLINK)",
        "log": f"[DOWNLINK] Server -> {door_id}: {command} | [UPLINK ACK] {door_id} -> Server: {STATE[door_id]}"
    }

@app.post("/api/control/light")
async def api_control_light(payload: dict):
    """[DOWNLINK] Server -> Terminal : Light ON/OFF switch"""
    light_id = payload.get("light_id", "Light1")
    state = payload.get("state", "ON").upper()
    
    if light_id not in STATE:
        return JSONResponse({"error": "Invalid light_id"}, status_code=400)
    
    power = 12.5 if state == "ON" else 0
    if "Switch" in light_id:
        power = random.randint(800, 1500) if state == "ON" else 0
    
    STATE[light_id] = {"state": state, "power_w": power, "today_kwh": STATE[light_id].get("today_kwh", 0.5)}
    
    # Here you would call real Tapo API:
    # from tapo import ApiClient
    # client = ApiClient(TAPO_EMAIL, TAPO_PASSWORD)
    # device = await client.p115(DEVICE_IPS[light_id])
    # await device.on() if state=="ON" else device.off()
    
    return {
        "result": "OK",
        "light_id": light_id,
        "new_state": state,
        "power_w": power,
        "timestamp": datetime.datetime.now().isoformat(),
        "direction": "Server -> Terminal (DOWNLINK)",
        "bothway": f"Server->{light_id}:{state} => Terminal reports power={power}W (UPLINK)"
    }

# Tapo RTSP: rtsp://<camera-account>:<password>@<ip>:554/stream1 (stream2 = low-res)
CAMERA_RTSP = CAMERA_RTSP
print(f"Camera RTSP URLs: {CAMERA_RTSP}")

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

@app.post("/api/control/camera/{camera_id}")
async def api_control_camera(camera_id: str, payload: dict):
    """[DOWNLINK] Server -> Terminal : Camera control + [UPLINK] snapshot"""
    action = payload.get("action", "SNAPSHOT").upper()
    
    return {
        "result": "OK",
        "camera_id": camera_id,
        "action": action,
        "snapshot_url": f"/static/snapshots/{camera_id}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg" if action=="SNAPSHOT" else None,
        "timestamp": datetime.datetime.now().isoformat(),
        "direction": "Both-way: DOWNLINK command + UPLINK video stream"
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

# Health
@app.get("/api/health")
async def health():
    return {"status": "OK", "gateway": "FastAPI + Jinja2 + FastMCP", "bothway": "Server<->Terminal active"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("tapo-server:app", host="0.0.0.0", port=8000, reload=True)