import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT/"src")); sys.path.insert(0, str(ROOT/"test"))
import test_user_settings as t
cfg = t._load_config()
from albuswall.services.command import register_user_settings
from albuswall.configue.setting_schema import dump_setting_schema
register_user_settings()
d = dump_setting_schema()
for it in d["items"]:
    if it["key"] in ("appearance.theme","appearance.theme_path","appearance.style_sheet","appearance.icon_dir"):
        print(it["key"], "| present:", it["present"], "| value:", repr(it["value"]), "| target:", it["target_file"])
print("by_file:", list(d["by_file"]))
from albuswall.configue.declaration import get_config
c = get_config()
print("raw style_sheet:", c.resolver.get_str("ui","style_sheet"))
print("typed style_sheet:", c.resolver.get_typed("ui","style_sheet"))
print("raw path.theme:", c.resolver.get_str("path","theme") if ("path","theme") in c.resolver else "MISSING")
