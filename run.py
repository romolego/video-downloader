"""Run the standalone service. No imports from the Whisper project."""
import argparse
import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
vendor = ROOT / "vendor" / "bin"
if vendor.is_dir():
    os.environ["PATH"] = str(vendor) + os.pathsep + os.environ.get("PATH", "")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8877)
    args = parser.parse_args()
    import uvicorn
    from downloader.app import app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port))
    app.state.stop = lambda: setattr(server, "should_exit", True)
    server.run()
