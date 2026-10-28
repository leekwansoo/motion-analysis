from utils.video_capture import camera_play
import os
from dotenv import load_dotenv

load_dotenv()

if __name__ == "__main__":
    url = os.getenv("CAMERA1_RTSP", "")
    print(f"Using camera URL: {url}")
    camera_play(url)