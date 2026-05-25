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

# =========================================================
# Robot queue helpers — Fase 6
# =========================================================

def _is_due_now(value: Any) -> bool:
    """True quando next_attempt_at está vazio ou já venceu."""
    if value in (None, ""):
        return True
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt <= datetime.now(timezone.utc)
    except Exception:
        return True


def claim_next_robot_job(worker_id: str, limit: int = 25) -> Optional[Dict[str, Any]]:
    """
    Busca e bloqueia o próximo job PENDING.

    Observação: seguro para HML/um worker. Para múltiplos workers em produção,
    o ideal é evoluir para RPC Postgres com SELECT FOR UPDATE SKIP LOCKED.
    """
    worker = (worker_id or "autodoc-worker").strip() or "autodoc-worker"

    res = (
        _table("autodoc_robot_queue")
        .select("*")
        .eq("status", "PENDING")
        .order("priority", desc=False)
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )

    picked: Optional[Dict[str, Any]] = None

    for job in res.data or []:
        attempts = int(job.get("attempts") or 0)
        max_attempts = int(job.get("max_attempts") or 3)
        if attempts >= max_attempts:
            continue
        if not _is_due_now(job.get("next_attempt_at")):
            continue
        picked = job
        break

    if not picked:
        return None

    attempts = int(picked.get("attempts") or 0) + 1
    now = now_iso()

    return update_row(
        "autodoc_robot_queue",
        picked["id"],
        {
            "status": "RUNNING",
            "locked_by": worker,
            "locked_at": now,
            "started_at": picked.get("started_at") or now,
            "attempts": attempts,
            "last_error": None,
            "next_attempt_at": None,
        },
    )


def complete_robot_job(job_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """Finaliza job como DONE."""
    now = now_iso()
    return update_row(
        "autodoc_robot_queue",
        job_id,
        {
            "status": "DONE",
            "result": result or {},
            "last_error": None,
            "locked_by": None,
            "locked_at": None,
            "finished_at": now,
            "next_attempt_at": None,
        },
    )


def fail_robot_job(
    job_id: str,
    error_payload: Dict[str, Any],
    retryable: bool = True,
    retry_delay_minutes: int = 5,
) -> Dict[str, Any]:
    """Marca falha do job. Se ainda puder tentar, volta para PENDING; senão vira ERROR."""
    from datetime import timedelta

    job = get_row("autodoc_robot_queue", job_id)
    if not job:
        raise RuntimeError(f"Job não encontrado: {job_id}")

    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 3)
    should_retry = bool(retryable) and attempts < max_attempts

    now_dt = datetime.now(timezone.utc)
    data: Dict[str, Any] = {
        "status": "PENDING" if should_retry else "ERROR",
        "last_error": error_payload or {},
        "locked_by": None,
        "locked_at": None,
    }

    if should_retry:
        data["next_attempt_at"] = (now_dt + timedelta(minutes=max(1, int(retry_delay_minutes or 5)))).isoformat()
        data["finished_at"] = None
    else:
        data["next_attempt_at"] = None
        data["finished_at"] = now_dt.isoformat()

    return update_row("autodoc_robot_queue", job_id, data)


def get_robot_job(job_id: str) -> Optional[Dict[str, Any]]:
    return get_row("autodoc_robot_queue", job_id)

