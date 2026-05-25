from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from supabase import create_client, Client

from .config import settings
from .logger import logger


_client: Optional[Client] = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_supabase_client() -> Client:
    global _client

    if _client is None:
        if not settings.supabase_url or not settings.supabase_service_key:
            raise RuntimeError("SUPABASE_URL e SUPABASE_SERVICE_KEY não configurados.")

        _client = create_client(settings.supabase_url, settings.supabase_service_key)

    return _client


def _table(name: str):
    return get_supabase_client().table(name)


def _remove_none(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def insert_row(table: str, data: Dict[str, Any]) -> Dict[str, Any]:
    clean_data = _remove_none(data)
    res = _table(table).insert(clean_data).execute()
    return res.data[0] if res.data else {}


def upsert_row(
    table: str,
    data: Dict[str, Any],
    on_conflict: Optional[str] = None,
) -> Dict[str, Any]:
    clean_data = _remove_none(data)

    if on_conflict:
        q = _table(table).upsert(clean_data, on_conflict=on_conflict)
    else:
        q = _table(table).upsert(clean_data)

    res = q.execute()
    return res.data[0] if res.data else {}


def update_row(table: str, row_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    clean_data = _remove_none(data)

    if "updated_at" not in clean_data:
        clean_data["updated_at"] = now_iso()

    res = _table(table).update(clean_data).eq("id", row_id).execute()
    return res.data[0] if res.data else {}


def get_row(table: str, row_id: str) -> Optional[Dict[str, Any]]:
    res = _table(table).select("*").eq("id", row_id).limit(1).execute()
    return res.data[0] if res.data else None


def list_rows(
    table: str,
    limit: int = 100,
    order_by: str = "created_at",
    desc: bool = True,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    q = _table(table).select("*")

    if filters:
        for key, value in filters.items():
            if value is not None and value != "":
                q = q.eq(key, value)

    q = q.order(order_by, desc=desc).limit(limit)
    return q.execute().data or []


def insert_history(**kwargs) -> Dict[str, Any]:
    """
    Registra histórico do pipeline.

    Aceita os campos antigos:
    - email_id
    - file_id
    - action
    - file_name
    - from_path
    - to_path
    - environment
    - status

    E também aceita campos novos:
    - message
    - payload
    - obsolete_path
    """

    data = _remove_none(kwargs)
    return insert_row("autodoc_history", data)


def insert_error(
    error_type: str,
    message: str,
    payload: Any = None,
    email_id: str = None,
    file_id: str = None,
    status: str = "ABERTO",
    source: str = "api",
    severity: str = "ERROR",
) -> Dict[str, Any]:
    logger.error("%s | %s", error_type, message)

    data = {
        "email_id": email_id,
        "file_id": file_id,
        "error_type": error_type,
        "message": message,
        "payload": payload,
        "status": status,
        "source": source,
        "severity": severity,
    }

    return insert_row("autodoc_errors", data)


def health_check() -> Dict[str, Any]:
    data = _table("autodoc_settings").select("key,value").limit(1).execute().data
    return {"ok": True, "sample": data}