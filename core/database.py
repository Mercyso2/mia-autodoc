from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from supabase import create_client, Client

from .config import settings
from .logger import logger


_client: Optional[Client] = None


# Campos que podem ser timestamp/data no banco.
# Nunca podemos enviar string vazia ("") para eles.
TIMESTAMP_FIELD_NAMES = {
    "received_at",
    "processing_started_at",
    "processing_finished_at",
    "last_processed_at",
    "approved_at",
    "ignored_at",
    "download_started_at",
    "download_finished_at",
    "upload_started_at",
    "upload_finished_at",
    "created_at",
    "updated_at",
    "hml_ready_at",
    "folder_map_ready_at",
    "prod_ready_at",
    "last_sync_at",
    "autodoc_datetime",
}


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


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _sanitize_top_level_value(key: str, value: Any) -> Any:
    """
    Sanitiza somente campos top-level que vão para colunas do Supabase.

    Importante:
    - JSONB interno pode conter strings vazias sem problema.
    - Colunas timestamp/timestamptz NÃO aceitam "".
    - Para produção, também removemos strings vazias top-level para evitar erro em colunas tipadas.
    """
    if value is None:
        return None

    # Nunca enviar string vazia para timestamp/date.
    if key in TIMESTAMP_FIELD_NAMES and isinstance(value, str) and value.strip() == "":
        return None

    # Blindagem geral: top-level string vazia vira None.
    # Isso evita erro de cast em uuid/timestamp/numeric quando algum fluxo mandar "".
    if isinstance(value, str) and value.strip() == "":
        return None

    return value


def _clean_data(data: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}

    for key, value in (data or {}).items():
        sanitized = _sanitize_top_level_value(key, value)

        if sanitized is not None:
            clean[key] = sanitized

    return clean


def insert_row(table: str, data: Dict[str, Any]) -> Dict[str, Any]:
    clean_data = _clean_data(data)
    res = _table(table).insert(clean_data).execute()
    return res.data[0] if res.data else {}


def upsert_row(
    table: str,
    data: Dict[str, Any],
    on_conflict: Optional[str] = None,
) -> Dict[str, Any]:
    clean_data = _clean_data(data)

    if on_conflict:
        q = _table(table).upsert(clean_data, on_conflict=on_conflict)
    else:
        q = _table(table).upsert(clean_data)

    res = q.execute()
    return res.data[0] if res.data else {}


def update_row(table: str, row_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    clean_data = _clean_data(data)

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
    data = _clean_data(kwargs)
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
