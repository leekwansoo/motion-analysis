# main functions for fastapi server
from usbx import usb
import cv2
import base64
import asyncio
import datetime
import random
import os
import json
from dotenv import load_dotenv
load_dotenv()

CAMERA_RTSP = {
    "Camera1": os.getenv("CAMERA1_RTSP"),
    "Camera2": os.getenv("CAMERA2_RTSP"),
}

from pathlib import Path
from utils.usb_device import list_usb_devices


BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

STATE = {
    "Temp1": {"temperature": 0, "humidity": 0},
    "Door1": "CLOSED",
    "Door2": "CLOSED",
    "Camera1": {"online": True, "motion": False},
    "Webcam1": {"online": True, "motion": False},
    "Light1": "OFF",
    "Light2": "OFF",
    "Switch1": {"state": "OFF", "power_w": 0},
    "Switch2": {"state": "OFF", "power_w": 0},
    "H200": {"ip": os.getenv("H200_IP")},
}


# Open your USB webcam
def get_usb_camera_index():
    usb_devices = list_usb_devices()
    if "WEBCAM" in usb_devices:
        return usb_devices.index("WEBCAM")
    else:
        return 0

def _open_camera():
    indices = [get_usb_camera_index()] + [i for i in range(4)]
    for idx in dict.fromkeys(indices):
        for backend in (cv2.CAP_DSHOW, cv2.CAP_ANY):
            cap = cv2.VideoCapture(idx, backend)
            if cap.isOpened():
                return cap
            cap.release()
    return cv2.VideoCapture(indices[0])

camera = _open_camera()

def get_usb_camera():
    """Return the shared camera (a second VideoCapture on the same device fails on Windows)."""
    global camera
    if not camera.isOpened():
        camera.release()
        camera = _open_camera()
    return camera

def play_video(video_path):
    cap = cv2.VideoCapture(str(video_path))
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame = cv2.resize(frame, (640, 480))
        cv2.imshow('Video', frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break
    cap.release()
    cv2.destroyAllWindows()   
    return True

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

        
def _webcam_mjpeg():
    """Generator for MJPEG frames from USB webcam"""
    failures = 0
    while True:
        ret, frame = get_usb_camera().read()
        if not ret:
            failures += 1
            if failures > 50:
                break
            continue
        failures = 0
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        frame_bytes = jpeg.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
      








