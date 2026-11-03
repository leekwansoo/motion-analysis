from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI()
# recordings 폴더 마운트 (한번만)
try:
    app.mount("/recordings_files", StaticFiles(directory="recordings"), name="recordings_files")
except:
    pass # 이미 마운트 되어있으면 패스

@app.get("/recordings", response_class=HTMLResponse)
async def recordings_viewer():
    rec_dir = Path("recordings")
    rec_dir.mkdir(exist_ok=True)
    files = sorted(rec_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)
    
    # JS에서 안전하게 쓰기 위해 파일명 리스트를 JSON으로
    import json
    file_list_json = json.dumps([f.name for f in files])

    html = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>Recording Viewer</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-900 text-white p-4">
<h1 class="text-xl font-bold mb-3">📹 녹화 뷰어 - 클릭하면 재생</h1>
<div class="grid grid-cols-12 gap-4">
  <div class="col-span-3 bg-gray-800 rounded p-3">
    <div id="fileList" class="space-y-2"></div>
  </div>
  <div class="col-span-9">
    <div class="bg-black rounded aspect-video relative overflow-hidden">
      <video id="videoPlayer" class="w-full h-full" controls playsinline></video>
      <div id="noFile" class="absolute inset-0 flex items-center justify-center text-gray-500">왼쪽 파일 클릭</div>
    </div>
    <div class="mt-3 flex gap-2">
      <button onclick="video.play()" class="bg-green-600 px-6 py-2 rounded">▶ 재생</button>
      <button onclick="video.pause()" class="bg-yellow-600 px-6 py-2 rounded">⏸ 정지</button>
      <button onclick="capture()" class="bg-blue-600 px-6 py-2 rounded">📸 캡처</button>
      <button onclick="video.currentTime-=0.1" class="bg-gray-700 px-3 py-2 rounded">-0.1s</button>
      <button onclick="video.currentTime+=0.1" class="bg-gray-700 px-3 py-2 rounded">+0.1s</button>
      <span id="time" class="ml-2 text-sm text-gray-400"></span>
    </div>
    <canvas id="canvas" style="display:none"></canvas>
    <div id="caps" class="mt-3 grid grid-cols-5 gap-2"></div>
  </div>
</div>

<script>
const files = __FILES__;
const listDiv = document.getElementById('fileList');
const video = document.getElementById('videoPlayer');

files.forEach(name => {
  const btn = document.createElement('button');
  btn.textContent = name;
  btn.className = 'w-full text-left text-xs bg-gray-700 hover:bg-blue-600 px-2 py-2 rounded truncate';
  btn.onclick = () => playFile(name);
  listDiv.appendChild(btn);
});

function playFile(name) {
  console.log("Playing:", name);
  document.getElementById('noFile').style.display = 'none';
  // encodeURIComponent로 공백/한글 파일명 처리
  video.src = '/recordings_files/' + encodeURIComponent(name);
  video.load();
  video.play().catch(e => alert("재생 실패: " + e.message));
}

video.addEventListener('timeupdate', () => {
  document.getElementById('time').textContent = video.currentTime.toFixed(2) + 's / ' + (video.duration||0).toFixed(2) + 's';
});

function capture() {
  if (!video.src) { alert('파일 먼저 선택'); return; }
  const canvas = document.getElementById('canvas');
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  const url = canvas.toDataURL('image/jpeg', 0.95);
  const a = document.createElement('a');
  a.download = 'capture_' + Date.now() + '.jpg';
  a.href = url;
  a.click();
  const img = document.createElement('img');
  img.src = url;
  img.className = 'rounded border border-gray-700';
  document.getElementById('caps').prepend(img);
}

document.addEventListener('keydown', e => {
  if (e.code === 'Space') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  if (e.key.toLowerCase() === 'c') capture();
});
</script>
</body>
</html>
""".replace("__FILES__", file_list_json)

    return HTMLResponse(html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("video-server:app", host="127.0.0.1", port=8000, reload=True)