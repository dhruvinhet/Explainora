import importlib.util
import os
import shutil
from pathlib import Path


def yesno(value):
    return "OK" if value else "MISSING"


print("Python packages:")
for package in ["flask", "openai", "requests", "cairosvg", "dotenv"]:
    print(f"  {package:12} {yesno(importlib.util.find_spec(package) is not None)}")

print("\nExecutables:")
for exe in [os.environ.get("FFMPEG_BIN", "ffmpeg"), os.environ.get("FFPROBE_BIN", "ffprobe"), os.environ.get("ESPEAK_BIN", "espeak")]:
    print(f"  {exe:12} {shutil.which(exe) or 'MISSING'}")

piper_bin = os.environ.get("PIPER_BIN", "piper")
piper_model = os.environ.get("PIPER_MODEL", "")
print(f"  {piper_bin:12} {shutil.which(piper_bin) or 'not configured / missing'}")
print(f"  PIPER_MODEL  {piper_model if piper_model and Path(piper_model).exists() else 'not configured / missing'}")

print("\nEnvironment:")
print("  NVIDIA_API_KEY", "SET" if os.environ.get("NVIDIA_API_KEY") else "MISSING")