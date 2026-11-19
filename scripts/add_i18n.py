"""Append UI strings to all 7 locale files (en + tr translated, the others get English).

    python scripts/add_i18n.py keys.json      # {"key": ["English", "Türkçe"], ...}
Keys that already exist in a locale are left alone (idempotent)."""
import json
import re
import sys
from pathlib import Path

LOC = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n" / "locales"
keys = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for f in sorted(LOC.glob("*.ts")):
    text = f.read_text(encoding="utf-8")
    have = set(re.findall(r'^\s*"([^"]+)":', text, re.M))
    add = []
    for k, (en, tr) in keys.items():
        if k not in have:
            val = tr if f.stem == "tr" else en
            add.append(f"  {json.dumps(k)}: {json.dumps(val, ensure_ascii=False)},")
    if not add:
        continue
    i = text.rstrip().rfind("}")
    head = text[:i].rstrip()
    if not head.endswith(","):
        head += ","
    f.write_text(head + "\n  // local gaming fork\n" + "\n".join(add) + "\n" + text[i:], encoding="utf-8")
    print(f.name, len(add))
