from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
import json


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_dir(job_id: str = '', file_id: str = '') -> Path:
    name = job_id or file_id or 'manual'
    path = Path('storage/evidence') / str(name)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(name: str, payload: Dict[str, Any], job_id: str = '', file_id: str = '') -> Path:
    path = evidence_dir(job_id, file_id) / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    return path
