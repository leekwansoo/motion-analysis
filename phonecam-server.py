from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import numpy as np, cv2, time, asyncio, threading
from pathlib import Path
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Server starting...")
    yield
    print("Server shutting down...")

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
OUTPUT_DIR = Path("recordings")
OUTPUT_DIR.mkdir(exist_ok=True)

# --- Dual Phone Frames ---
phones = {
    "PHONE1": {"jpeg": None, "frame": None, "last": 0},
    "PHONE2": {"jpeg": None, "frame": None, "last": 0},
}
viewers = set()
combined_jpeg = None

recording_state = {"active": False}

def decode_jpeg(jpeg_bytes):
    nparr = np.frombuffer(jpeg_bytes, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)

def get_combined_frame():
    f1 = phones["PHONE1"]["frame"]
    f2 = phones["PHONE2"]["frame"]
    
    # 360x480 포터레이트
    if f1 is None:
        f1 = np.zeros((480, 360, 3), dtype=np.uint8)
        cv2.putText(f1, "PHONE1 Waiting", (20, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
    if f2 is None:
        f2 = np.zeros((480, 360, 3), dtype=np.uint8)
        cv2.putText(f2, "PHONE2 Waiting /phone2", (20, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2)
    
    # 좌우 합치기 -> 720x480 (16:9 비슷한 와이드)
    combined = np.hstack([f1, f2])
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(combined, ts, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,0), 1)
    cv2.putText(combined, "TAB1", (10, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,255), 1)
    cv2.putText(combined, "TAB2", (370, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,255,255), 1)
    if recording_state["active"]:
        cv2.circle(combined, (700, 20), 8, (0,0,255), -1)
    return combined

def recording_loop():
    print("Recording started")
    filepath = OUTPUT_DIR / f"dual_phone_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
    writer = None
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    while recording_state["active"]:
        combined = get_combined_frame()
        if writer is None:
            h,w = combined.shape[:2]
            writer = cv2.VideoWriter(str(filepath), fourcc, 15.0, (w,h))
            print(f"Writer {w}x{h} -> {filepath}")
        writer.write(combined)
        time.sleep(0.066) # 15fps
    if writer:
        writer.release()
        print(f"Saved: {filepath} {filepath.stat().st_size/1024:.1f}KB")

# --- Phone Pages ---
def phone_html(phone_id):
    return f"""
<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{margin:0;background:#000;color:#fff;display:flex;flex-direction:column;height:100vh;font-family:sans-serif}}
#box{{flex:1;background:#111;display:flex;align-items:center;justify-content:center}} 
video{{width:100%;height:100%;object-fit:contain}}
#ctrl{{background:#111;padding:8px;display:flex;gap:6px;justify-content:center;border-top:1px solid #333}}
button{{padding:10px 12px;border-radius:8px;border:1px solid #555;background:#222;color:#fff;flex:1}}
#info{{position:absolute;top:5px;left:0;right:0;text-align:center;font-size:12px;color:#0f0;background:rgba(0,0,0,0.5)}}
</style></head><body>
<div id="box"><video id="v" autoplay playsinline muted></video><div id="info">PHONE {phone_id} - 연결중</div></div>
<div id="ctrl"><button id="btnFlip">📷 전환</button><button id="btnStart">▶️ 시작</button></div>
<script>
const v=document.getElementById('v'), info=document.getElementById('info');
let ws=null, stream=null, facing="environment", sending=true;
function connect(){{
  const proto=location.protocol==='https:'?'wss://':'ws://';
  ws=new WebSocket(proto+location.host+'/ws/{phone_id.lower()}');
  ws.binaryType='arraybuffer';
  ws.onopen=()=>info.textContent='✅ PHONE {phone_id} 연결됨';
  ws.onclose=()=>setTimeout(connect,1000);
}}
async function startCam(){{
  if(stream) stream.getTracks().forEach(t=>t.stop());
  stream=await navigator.mediaDevices.getUserMedia({{video:{{facingMode:facing, width:{{ideal:640}}, height:{{ideal:480}}}}, audio:false}});
  v.srcObject=stream;
}}
function loop(){{
  const canvas=document.createElement('canvas'), ctx=canvas.getContext('2d');
  const W=360, H=480;
  function frame(){{
    if(sending && v.videoWidth && ws && ws.readyState===1){{
      canvas.width=W; canvas.height=H;
      const vw=v.videoWidth, vh=v.videoHeight, ta=W/H;
      let sx,sy,sw,sh;
      if(vw/vh>ta){{ sh=vh; sw=sh*ta; sx=(vw-sw)/2; sy=0; }} else {{ sw=vw; sh=sw/ta; sx=0; sy=(vh-sh)/2; }}
      ctx.drawImage(v, sx, sy, sw, sh, 0, 0, W, H);
      canvas.toBlob(b=>{{ if(b) b.arrayBuffer().then(x=>ws.send(x)) }},'image/jpeg',0.6);
    }}
    setTimeout(()=>requestAnimationFrame(frame),66);
  }}
  frame();
}}
document.getElementById('btnFlip').onclick=()=>{{ facing=facing==='environment'?'user':'environment'; startCam(); }};
document.getElementById('btnStart').onclick=e=>{{ sending=!sending; e.target.textContent=sending?'⏹ 정지':'▶️ 시작'; }};
connect(); startCam(); loop();
</script>
</body></html>
"""

@app.get("/phone1", response_class=HTMLResponse)
async def phone1_page(): return HTMLResponse(phone_html("PHONE1"))

@app.get("/phone2", response_class=HTMLResponse)
async def phone2_page(): return HTMLResponse(phone_html("PHONE2"))

# --- WebSockets for 2 phones ---
@app.websocket("/ws/phone1")
async def ws_phone1(websocket: WebSocket):
    await websocket.accept()
    print("PHONE1 connected")
    try:
        while True:
            data = await websocket.receive_bytes()
            phones["PHONE1"]["jpeg"]=data
            phones["PHONE1"]["frame"]=decode_jpeg(data)
            phones["PHONE1"]["last"]=time.time()
            await broadcast_combined()
    except WebSocketDisconnect:
        print("PHONE1 disconnected")

@app.websocket("/ws/phone2")
async def ws_phone2(websocket: WebSocket):
    await websocket.accept()
    print("PHONE2 connected")
    try:
        while True:
            data = await websocket.receive_bytes()
            phones["PHONE2"]["jpeg"]=data
            phones["PHONE2"]["frame"]=decode_jpeg(data)
            phones["PHONE2"]["last"]=time.time()
            await broadcast_combined()
    except WebSocketDisconnect:
        print("PHONE2 disconnected")

async def broadcast_combined():
    global combined_jpeg
    combined = get_combined_frame()
    _, jpg = cv2.imencode('.jpg', combined, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    combined_jpeg = jpg.tobytes()
    dead=set()
    for vws in viewers:
        try: await vws.send_bytes(combined_jpeg)
        except: dead.add(vws)
    for d in dead: viewers.discard(d)

@app.websocket("/ws/viewer")
async def ws_viewer(websocket: WebSocket):
    await websocket.accept()
    viewers.add(websocket)
    try:
        while True: await asyncio.sleep(10)
    except WebSocketDisconnect:
        viewers.discard(websocket)

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "phonecam-index.html", {})

@app.post("/start_recording")
async def start_recording():
    if not recording_state["active"]:
        recording_state["active"]=True
        threading.Thread(target=recording_loop, daemon=True).start()
    return {"status":"recording"}

@app.post("/stop_recording")
async def stop_recording():
    recording_state["active"]=False
    return {"status":"stopped"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("phonecam-server:app", host="0.0.0.0", port=8080, reload=False)