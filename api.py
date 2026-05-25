from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from pathlib import Path
import shutil
import hashlib
import json

from core.config import settings
from core.database import *
from core.security import require_api_key, security_status
from core import statuses as st

from services.email_parser import parse_email, make_autodoc_path_safe
from services.sharepoint_mapper import (
    discover_site_from_url,
    sync_site_folders,
    resolve_folder,
    target_path,
    bootstrap_hml_structure,
    generate_folder_map,
    prepare_site_hml,
)
from services.scoring_service import score_file
from services.file_service import enrich_file_destination, upload_hml
from services.sharepoint_service import sharepoint

from robot.login_manager import check_logged_in, open_login_browser
from robot.downloader import download as autodoc_download


app = FastAPI(title="Autodoc Center V4 Multi-site SharePoint")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


class EmailIngest(BaseModel):
    source: str = "n8n"
    environment: str = "HML"
    message_id: str
    folder: str = "Autodoc"
    subject: str = ""
    sender: str = ""
    received_at: Optional[str] = None
    html: str = ""
    text: str = ""
    attachments: List[Dict[str, Any]] = []


class SiteDiscover(BaseModel):
    project_name: str
    site_url: str
    aliases: str = ""
    library_name: str = "Documentos"


class CorrectPath(BaseModel):
    sharepoint_site_ref: Optional[str] = None
    sharepoint_drive_id: Optional[str] = None
    sharepoint_suggested_path: str


# =========================================================
# Helpers gerais
# =========================================================

def _safe_get_status(name: str, fallback: str) -> str:
    return getattr(st, name, fallback)


def _sha_payload(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(value)

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove valores None para evitar erro de coluna/opcional no Supabase.
    Mantém string vazia, False e 0.
    """
    return {k: v for k, v in data.items() if v is not None}


def _file_identity(email_id: str, file_name: str, autodoc_path_safe: str) -> str:
    """
    Identidade lógica do arquivo detectado.

    IMPORTANTE:
    autodoc_path_safe pode ser coluna gerada no Supabase.
    Usamos aqui só para gerar hash/deduplicação, não para gravar diretamente.
    """
    return _sha_payload(
        {
            "email_id": email_id,
            "file_name": file_name or "",
            "autodoc_path_safe": autodoc_path_safe or "",
        }
    )


def _find_existing_file(
    email_id: str,
    file_name: str,
    autodoc_path_safe: str,
) -> Optional[Dict[str, Any]]:
    """
    Procura arquivo já criado.

    Primeiro tenta por coluna gerada autodoc_path_safe.
    Se houver algum problema no schema/cache, cai para source_hash.
    """
    source_hash = _file_identity(email_id, file_name, autodoc_path_safe)

    try:
        rows = list_rows(
            "autodoc_files",
            limit=1,
            filters={
                "email_id": email_id,
                "file_name": file_name,
                "autodoc_path_safe": autodoc_path_safe,
            },
        )
        if rows:
            return rows[0]
    except Exception:
        # Fallback caso autodoc_path_safe esteja como coluna gerada com schema cache sensível.
        pass

    try:
        rows = list_rows(
            "autodoc_files",
            limit=1,
            filters={"source_hash": source_hash},
        )
        return rows[0] if rows else None
    except Exception:
        return None


def _upsert_file_by_identity(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Upsert manual para não depender de unique index no Supabase.

    IMPORTANTE:
    No banco atual, autodoc_path_safe é coluna gerada.
    Portanto:
    - calculamos autodoc_path_safe para procurar/deduplicar;
    - NÃO enviamos autodoc_path_safe no insert/update.
    """
    email_id = row.get("email_id")
    file_name = row.get("file_name")
    autodoc_path_safe = row.get("autodoc_path_safe") or make_autodoc_path_safe(
        row.get("autodoc_path", "")
    )

    row["source_hash"] = row.get("source_hash") or _file_identity(
        email_id,
        file_name,
        autodoc_path_safe,
    )

    existing = _find_existing_file(email_id, file_name, autodoc_path_safe)
    data = _clean_dict(row)

    # Nunca gravar coluna gerada pelo Supabase.
    data.pop("autodoc_path_safe", None)

    if existing and existing.get("id"):
        return update_row("autodoc_files", existing["id"], data)

    return insert_row("autodoc_files", data)


def _status_for_file(
    fd: Dict[str, Any],
    mapping: Optional[Dict[str, Any]],
    score: Dict[str, Any],
) -> str:
    confidence = int(score.get("confidence_score") or score.get("score") or 0)

    if not fd.get("file_name"):
        return _safe_get_status("FILE_ERRO", "ERRO")

    if not fd.get("project_detected") and not mapping:
        return _safe_get_status("FILE_AGUARDANDO_APROVACAO", "AGUARDANDO_APROVACAO")

    if not fd.get("discipline_detected") and not mapping:
        return _safe_get_status("FILE_AGUARDANDO_APROVACAO", "AGUARDANDO_APROVACAO")

    if fd.get("has_attachment"):
        return _safe_get_status("FILE_AGUARDANDO_DOWNLOAD", "AGUARDANDO_DOWNLOAD")

    if confidence >= int(getattr(settings, "min_auto_score", 90) or 90):
        return _safe_get_status("FILE_AGUARDANDO_DOWNLOAD", "AGUARDANDO_DOWNLOAD")

    return _safe_get_status("FILE_AGUARDANDO_APROVACAO", "AGUARDANDO_APROVACAO")


# =========================================================
# Health / segurança
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.app_env,
        "production_enabled": settings.is_prod_enabled,
        "multi_site_sharepoint": True,
    }


@app.get("/security/health")
def security_health():
    return security_status()


@app.get("/db/health")
def db_health():
    try:
        return health_check()
    except Exception as e:
        raise HTTPException(500, str(e))


# =========================================================
# SharePoint / Graph
# =========================================================

@app.get("/sharepoint/health")
def sp_health():
    try:
        sharepoint.token()
        return {
            "ok": True,
            "hostname": settings.sharepoint_hostname,
            "mode": "multi-site",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sharepoint/sites/discover")
def sp_discover(payload: SiteDiscover, _: bool = Depends(require_api_key)):
    try:
        return discover_site_from_url(
            payload.project_name,
            payload.site_url,
            payload.aliases,
            payload.library_name,
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sharepoint/sites")
def sp_sites():
    return list_rows("autodoc_sharepoint_sites", limit=1000)


@app.post("/sharepoint/sites/{site_row_id}/sync-folders")
def sp_sync_folders(
    site_row_id: str,
    base_folder: str = "",
    _: bool = Depends(require_api_key),
):
    try:
        return {"ok": True, "folders": sync_site_folders(site_row_id, base_folder)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sharepoint/sites/{site_row_id}/bootstrap-hml")
def sp_bootstrap_hml(
    site_row_id: str,
    base_folder: str = "",
    _: bool = Depends(require_api_key),
):
    try:
        return bootstrap_hml_structure(site_row_id, base_folder)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sharepoint/sites/{site_row_id}/generate-folder-map")
def sp_generate_folder_map(
    site_row_id: str,
    file_extensions: str = "pdf,dwg",
    _: bool = Depends(require_api_key),
):
    try:
        exts = [x.strip() for x in file_extensions.split(",") if x.strip()]
        return generate_folder_map(site_row_id, exts)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/sharepoint/sites/{site_row_id}/prepare-hml")
def sp_prepare_hml(
    site_row_id: str,
    base_folder: str = "",
    file_extensions: str = "pdf,dwg",
    _: bool = Depends(require_api_key),
):
    try:
        exts = [x.strip() for x in file_extensions.split(",") if x.strip()]
        return prepare_site_hml(site_row_id, base_folder, exts)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/sharepoint/sites/{site_row_id}/children")
def sp_children(site_row_id: str, folder_path: str = ""):
    site = get_row("autodoc_sharepoint_sites", site_row_id)

    if not site:
        raise HTTPException(404, "Site não encontrado")

    try:
        return sharepoint.children(site["drive_id"], folder_path)
    except Exception as e:
        raise HTTPException(500, str(e))


# =========================================================
# E-mails / parser / file_id real
# =========================================================

@app.post("/emails/ingest")
def ingest(payload: EmailIngest, _: bool = Depends(require_api_key)):
    try:
        raw_payload = payload.model_dump()

        email_data = {
            "message_id": payload.message_id,
            "source": payload.source,
            "environment": payload.environment,
            "folder": payload.folder,
            "subject": payload.subject,
            "sender": payload.sender,
            "received_at": payload.received_at,
            "html": payload.html,
            "text": payload.text,
            "attachments": payload.attachments,
            "status": st.EMAIL_RECEBIDO,
            "raw_payload": raw_payload,
            "retry_count": 0,
        }

        email = upsert_row(
            "autodoc_emails",
            email_data,
            on_conflict="message_id",
        )

        insert_history(
            email_id=email.get("id"),
            action=st.ACTION_EMAIL_RECEBIDO,
            environment=payload.environment,
            status="OK",
            message="E-mail recebido via API/n8n",
            payload={
                "message_id": payload.message_id,
                "source": payload.source,
            },
        )

        return {
            "ok": True,
            "email_id": email.get("id"),
            "status": st.EMAIL_RECEBIDO,
        }

    except Exception as e:
        insert_error(
            "EMAIL_INGEST_ERROR",
            str(e),
            payload=payload.model_dump(),
        )
        raise HTTPException(500, str(e))


@app.post("/emails/{email_id}/parse")
def parse_saved_email(
    email_id: str,
    force: bool = False,
    _: bool = Depends(require_api_key),
):
    email = get_row("autodoc_emails", email_id)

    if not email:
        raise HTTPException(404, "E-mail não encontrado")

    try:
        update_row(
            "autodoc_emails",
            email_id,
            {
                "processing_started_at": now_iso(),
                "error_message": None,
            },
        )

        parsed = parse_email(
            email.get("subject", ""),
            email.get("html", ""),
            email.get("text", ""),
            email.get("attachments") or [],
        )

        created = []

        for fd in parsed.get("files", []):
            autodoc_path = fd.get("autodoc_path", "") or ""
            autodoc_path_safe = make_autodoc_path_safe(autodoc_path)

            project_detected = fd.get("project_detected") or parsed.get("project") or ""
            discipline_detected = fd.get("discipline_detected") or ""

            mapping = resolve_folder(
                project_detected,
                discipline_detected,
                autodoc_path,
                fd.get("extension", ""),
            )

            scored = score_file(fd, mapping)
            status = _status_for_file(
                {**fd, "project_detected": project_detected},
                mapping,
                scored,
            )

            # Não incluir autodoc_path_safe aqui.
            # Essa coluna é gerada no banco Supabase.
            row = {
                "email_id": email_id,
                "file_name": fd["file_name"],
                "extension": fd.get("extension"),
                "environment": settings.app_env,
                "source": fd.get("source") or "email_parser",
                "title": fd.get("title"),
                "file_status_autodoc": fd.get("file_status_autodoc"),
                "autodoc_user": fd.get("autodoc_user"),
                "autodoc_datetime": fd.get("autodoc_datetime"),
                "project_detected": project_detected,
                "discipline_detected": discipline_detected,
                "autodoc_path": autodoc_path,
                "needs_autodoc_robot": not fd.get("has_attachment", False),
                "parser_payload": fd,
                "raw_payload": fd.get("raw") or {},
                "status": status,
                "last_processed_at": now_iso(),
                **scored,
            }

            if mapping:
                site = mapping["_site"]
                row.update(
                    {
                        "sharepoint_site_ref": site["id"],
                        "sharepoint_drive_id": site["drive_id"],
                        "sharepoint_suggested_path": target_path(mapping, settings.app_env),
                        "sharepoint_folder_map_ref": mapping.get("id"),
                    }
                )

            # Envia autodoc_path_safe apenas para deduplicação interna,
            # mas _upsert_file_by_identity remove antes de gravar.
            f = _upsert_file_by_identity(
                {
                    **row,
                    "autodoc_path_safe": autodoc_path_safe,
                }
            )

            created.append(
                {
                    "file_id": f.get("id"),
                    "file_name": f.get("file_name"),
                    "status": f.get("status"),
                    "confidence_score": f.get("confidence_score"),
                    "needs_autodoc_robot": f.get("needs_autodoc_robot"),
                    "sharepoint_suggested_path": f.get("sharepoint_suggested_path"),
                    "row": f,
                }
            )

            insert_history(
                email_id=email_id,
                file_id=f.get("id"),
                action=st.ACTION_ARQUIVO_DETECTADO,
                file_name=f.get("file_name"),
                to_path=f.get("sharepoint_suggested_path"),
                environment=settings.app_env,
                status=f.get("status"),
                message="Arquivo detectado pelo parser com file_id real",
                payload={
                    "source": fd.get("source"),
                    "autodoc_path_safe": autodoc_path_safe,
                },
            )

        update_row(
            "autodoc_emails",
            email_id,
            {
                "status": st.EMAIL_PROCESSADO,
                "email_type": parsed.get("email_type"),
                "parser_payload": parsed,
                "processing_finished_at": now_iso(),
                "error_message": None,
            },
        )

        return {
            "ok": True,
            "email_id": email_id,
            "email_type": parsed.get("email_type"),
            "files_created": len(created),
            "files": created,
        }

    except Exception as e:
        insert_error(
            "PARSER_ERROR",
            str(e),
            email_id=email_id,
            payload={"email_id": email_id},
        )

        update_row(
            "autodoc_emails",
            email_id,
            {
                "status": st.EMAIL_ERRO,
                "processing_finished_at": now_iso(),
                "error_message": str(e),
            },
        )

        raise HTTPException(500, str(e))


@app.post("/emails/parse-latest")
def parse_latest(_: bool = Depends(require_api_key)):
    emails = list_rows(
        "autodoc_emails",
        limit=1,
        filters={"status": st.EMAIL_RECEBIDO},
    )

    if not emails:
        return {
            "ok": False,
            "message": "Nenhum e-mail recebido pendente",
        }

    return parse_saved_email(emails[0]["id"])


# =========================================================
# Arquivos
# =========================================================

@app.get("/files")
def files(status: str = "", limit: int = 100):
    return list_rows(
        "autodoc_files",
        limit=limit,
        filters={"status": status} if status else None,
    )


@app.get("/files/ready")
def files_ready(limit: int = 100):
    return list_rows(
        "autodoc_files",
        limit=limit,
        filters={"status": st.FILE_PRONTO},
    )


@app.get("/files/{file_id}")
def file_get(file_id: str):
    f = get_row("autodoc_files", file_id)

    if not f:
        raise HTTPException(404, "Arquivo não encontrado")

    return f


@app.post("/files/{file_id}/enrich-destination")
def file_enrich(file_id: str, _: bool = Depends(require_api_key)):
    try:
        return enrich_file_destination(file_id)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/files/{file_id}/approve")
def file_approve(file_id: str, _: bool = Depends(require_api_key)):
    f = update_row(
        "autodoc_files",
        file_id,
        {
            "status": st.FILE_PRONTO,
            "approved": True,
            "approved_at": now_iso(),
        },
    )

    insert_history(
        email_id=f.get("email_id"),
        file_id=file_id,
        action=st.ACTION_APROVADO,
        file_name=f.get("file_name"),
        environment=settings.app_env,
        status="OK",
    )

    return f


@app.post("/files/{file_id}/correct-path")
def file_correct(
    file_id: str,
    payload: CorrectPath,
    _: bool = Depends(require_api_key),
):
    data = {
        "sharepoint_suggested_path": payload.sharepoint_suggested_path,
        "status": st.FILE_AGUARDANDO_APROVACAO,
    }

    if payload.sharepoint_site_ref:
        data["sharepoint_site_ref"] = payload.sharepoint_site_ref

    if payload.sharepoint_drive_id:
        data["sharepoint_drive_id"] = payload.sharepoint_drive_id

    f = update_row("autodoc_files", file_id, data)

    insert_history(
        email_id=f.get("email_id"),
        file_id=file_id,
        action=st.ACTION_CORRIGIDO,
        file_name=f.get("file_name"),
        to_path=payload.sharepoint_suggested_path,
        environment=settings.app_env,
        status="OK",
    )

    return f


@app.post("/files/{file_id}/ignore")
def file_ignore(file_id: str, _: bool = Depends(require_api_key)):
    f = update_row(
        "autodoc_files",
        file_id,
        {
            "status": st.FILE_IGNORADO,
            "ignored_at": now_iso(),
        },
    )

    insert_history(
        email_id=f.get("email_id"),
        file_id=file_id,
        action=st.ACTION_IGNORADO,
        file_name=f.get("file_name"),
        environment=settings.app_env,
        status="OK",
    )

    return f


@app.post("/files/{file_id}/upload-local")
def upload_local(
    file_id: str,
    file: UploadFile = File(...),
    _: bool = Depends(require_api_key),
):
    f = get_row("autodoc_files", file_id)

    if not f:
        raise HTTPException(404, "Arquivo não encontrado")

    dest = Path("storage/downloads") / f["file_name"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    return update_row(
        "autodoc_files",
        file_id,
        {
            "local_path": str(dest),
            "status": st.FILE_BAIXADO,
            "download_finished_at": now_iso(),
        },
    )


@app.post("/files/{file_id}/queue-robot")
def queue_robot(file_id: str, _: bool = Depends(require_api_key)):
    f = get_row("autodoc_files", file_id)

    if not f:
        raise HTTPException(404, "Arquivo não encontrado")

    job = insert_row(
        "autodoc_robot_queue",
        {
            "file_id": file_id,
            "status": "PENDING",
            "payload": f,
        },
    )

    update_row(
        "autodoc_files",
        file_id,
        {
            "status": st.FILE_AGUARDANDO_DOWNLOAD,
            "needs_autodoc_robot": True,
            "robot_queue_id": job.get("id"),
        },
    )

    return {
        "ok": True,
        "job": job,
    }


@app.post("/files/{file_id}/upload-hml")
def file_upload_hml(file_id: str, _: bool = Depends(require_api_key)):
    try:
        return upload_hml(file_id)
    except Exception as e:
        insert_error("SHAREPOINT_UPLOAD_ERROR", str(e), file_id=file_id)
        update_row(
            "autodoc_files",
            file_id,
            {
                "status": st.FILE_ERRO,
                "error_message": str(e),
            },
        )
        raise HTTPException(500, str(e))


# =========================================================
# AutoDoc robô
# =========================================================

@app.post("/autodoc/login/check")
def autodoc_login_check(_: bool = Depends(require_api_key)):
    return check_logged_in()


@app.post("/autodoc/login/open")
def autodoc_login_open(_: bool = Depends(require_api_key)):
    return open_login_browser()


@app.post("/autodoc/download")
def autodoc_download_endpoint(
    payload: Dict[str, Any],
    _: bool = Depends(require_api_key),
):
    file_id = payload.get("file_id")
    f = get_row("autodoc_files", file_id) if file_id else None

    project = payload.get("project") or (f or {}).get("project_detected", "")
    path = payload.get("autodoc_path") or (f or {}).get("autodoc_path", "")
    name = payload.get("file_name") or (f or {}).get("file_name", "")

    try:
        res = autodoc_download(project, path, name)

        if f:
            update_row(
                "autodoc_files",
                file_id,
                {
                    "local_path": res["local_path"],
                    "status": st.FILE_BAIXADO,
                    "download_finished_at": now_iso(),
                },
            )

        return res

    except Exception as e:
        if f:
            insert_error("AUTODOC_DOWNLOAD_ERROR", str(e), file_id=file_id)
        raise HTTPException(500, str(e))
