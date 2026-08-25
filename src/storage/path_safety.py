from __future__ import annotations

import re
from pathlib import Path


def sanitize_download_filename(value: str) -> str:
    text = Path(str(value).replace("\\", "/")).name.strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", "_", text)
    text = re.sub(r"[/\\]+", "_", text)
    text = text.strip(" .")
    return (text or "download.bin")[:200]
