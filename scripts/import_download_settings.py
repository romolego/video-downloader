"""Optional, one-time import of preferences, never of recordings or credentials."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from downloader import storage

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
args = parser.parse_args()
source = args.database.resolve()
db = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
try:
    settings = {key: json.loads(value) for key, value in db.execute("SELECT key,value_json FROM settings")}
finally:
    db.close()
values = {key: settings[key] for key in ("theme", "youtube_cookies", "project_view", "project_name_format")
          if settings.get(key) in storage.SETTING_VALUES[key]}
quality = settings.get("download_quality")
if quality in storage.SETTING_VALUES["download_quality"]:
    values["download_mode"] = "audio" if quality == "audio" else "video"
    values["download_quality"] = "1080" if quality == "audio" else quality
storage.init_db()
storage.update_settings(values)
print("Imported download preferences:", values)
