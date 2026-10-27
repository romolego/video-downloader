import importlib.metadata
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run  # configures optional vendor/bin

errors = []
for tool in ("ffmpeg", "ffprobe"):
    path = shutil.which(tool)
    print(f"{tool}: {path or 'NOT FOUND'}")
    if not path:
        errors.append(tool)
js = shutil.which("deno") or shutil.which("node")
if js:
    result = subprocess.run([js, "--version"], capture_output=True, text=True)
    print("JavaScript:", result.stdout.splitlines()[0])
    if Path(js).stem == "node" and int(result.stdout.strip().lstrip("v").split(".")[0]) < 22:
        errors.append("Node.js 22+")
else:
    errors.append("Node.js 22+ or Deno")
for name in ("fastapi", "uvicorn", "requests", "yt-dlp", "yt-dlp-ejs"):
    try:
        print(f"{name}: {importlib.metadata.version(name)}")
    except importlib.metadata.PackageNotFoundError:
        errors.append(name)
if errors:
    print("Missing:", ", ".join(errors))
    sys.exit(1)
print("OK: standalone downloader is ready; no Whisper/ML dependencies required.")
