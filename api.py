from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from pathlib import Path
import shutil
import hashlib
import json
import os
import time
import hmac
import base64

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


app = FastAPI(title="MIA AUTODOC — Painel e API")

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


class RobotNextRequest(BaseModel):
    worker_id: str = "manual-test-worker"


class RobotCompleteRequest(BaseModel):
    file_id: Optional[str] = None
    local_path: Optional[str] = None
    local_sha256: Optional[str] = None
    local_size_bytes: Optional[int] = None
    sharepoint_item_id: Optional[str] = None
    sharepoint_web_url: Optional[str] = None
    sharepoint_final_path: Optional[str] = None
    saved_environment: Optional[str] = None
    result: Dict[str, Any] = {}


class RobotFailRequest(BaseModel):
    file_id: Optional[str] = None
    error_type: str = "ROBOT_ERROR"
    message: str = ""
    details: Dict[str, Any] = {}
    retryable: bool = True
    retry_delay_minutes: int = 5


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
# Painel seguro / Cliente final
# =========================================================

PANEL_COOKIE_NAME = "autodoc_panel_session"
PANEL_SESSION_TTL_SECONDS = 60 * 60 * 8


def _read_env_file_value(key: str) -> str:
    """
    Fallback para ambientes como EasyPanel, Docker e serviços que criam
    arquivo .env em vez de expor tudo diretamente em os.environ.

    Não lança erro. Retorna string vazia quando não encontrar.
    """
    candidates = [
        Path(".env"),
        Path("/app/.env"),
        Path("/code/.env"),
        Path("/workspace/.env"),
    ]

    for path in candidates:
        try:
            if not path.exists():
                continue

            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()

                if not line or line.startswith("#") or "=" not in line:
                    continue

                k, v = line.split("=", 1)

                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
        except Exception:
            pass

    return ""


def _read_panel_value(*keys: str) -> str:
    """
    Lê configuração do painel em múltiplas origens:
    1. Variável de ambiente real
    2. Arquivo .env
    3. settings do core.config
    """
    for key in keys:
        value = os.getenv(key)
        if value:
            return value.strip()

    for key in keys:
        value = _read_env_file_value(key)
        if value:
            return value.strip()

    for key in keys:
        attr = key.lower()
        value = getattr(settings, attr, None)
        if value:
            return str(value).strip()

    return ""



def _panel_secret() -> str:
    """
    Segredo usado para assinar a sessão do painel.

    Configure no EasyPanel:
    PANEL_SECRET_KEY=uma-string-grande-aleatoria

    Fallbacks:
    - AUTODOC_PANEL_SECRET_KEY
    - AUTODOC_API_KEY
    - API_KEY
    """
    return (
        _read_panel_value("PANEL_SECRET_KEY", "AUTODOC_PANEL_SECRET_KEY")
        or _read_panel_value("AUTODOC_API_KEY", "API_KEY")
        or "CHANGE_ME_PANEL_SECRET"
    )


def _panel_password() -> str:
    """
    Senha do painel.

    Configure no EasyPanel:
    PANEL_PASSWORD=sua-senha-forte

    Fallback:
    AUTODOC_PANEL_PASSWORD
    """
    return _read_panel_value("PANEL_PASSWORD", "AUTODOC_PANEL_PASSWORD")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("utf-8"))


def _sign_panel_payload(payload: str) -> str:
    digest = hmac.new(
        _panel_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64url(digest)


def _create_panel_token(username: str = "cliente") -> str:
    exp = int(time.time()) + PANEL_SESSION_TTL_SECONDS
    payload = json.dumps(
        {
            "sub": username,
            "exp": exp,
            "scope": "panel",
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )
    payload_b64 = _b64url(payload.encode("utf-8"))
    sig = _sign_panel_payload(payload_b64)
    return f"{payload_b64}.{sig}"


def _verify_panel_token(token: str) -> Dict[str, Any]:
    if not token or "." not in token:
        raise HTTPException(401, "Sessão ausente")

    payload_b64, sig = token.rsplit(".", 1)
    expected = _sign_panel_payload(payload_b64)

    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, "Sessão inválida")

    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        raise HTTPException(401, "Sessão inválida")

    if int(payload.get("exp") or 0) < int(time.time()):
        raise HTTPException(401, "Sessão expirada")

    if payload.get("scope") != "panel":
        raise HTTPException(401, "Escopo inválido")

    return payload


def require_panel_session(request: Request) -> Dict[str, Any]:
    token = request.cookies.get(PANEL_COOKIE_NAME)
    return _verify_panel_token(token or "")


class PanelLogin(BaseModel):
    password: str
    username: str = "cliente"


def _panel_html() -> str:
    return r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <meta name="theme-color" content="#050505" />
  <title>MIA AUTODOC — Painel do Cliente</title>

  <style>
    :root{
      --bg:#050505;
      --surface:#0c0c0e;
      --surface2:#111114;
      --surface3:#18181d;
      --line:rgba(255,255,255,.075);
      --line2:rgba(169,121,139,.32);
      --text:#faf7f8;
      --muted:#b9afb4;
      --muted2:#80767b;
      --brand:#A9798B;
      --brand2:#d2a4b7;
      --brand3:#7f5b68;
      --ok:#22c55e;
      --warn:#f59e0b;
      --danger:#ef4444;
      --info:#60a5fa;
      --shadow:0 34px 110px rgba(0,0,0,.60);
      --radius:26px;
      --sidebar:304px;
    }

    *{box-sizing:border-box}
    html,body{margin:0;min-height:100%}
    body{
      color:var(--text);
      font-family:Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
      background:
        radial-gradient(circle at 5% -10%, rgba(169,121,139,.26), transparent 26%),
        radial-gradient(circle at 95% 0%, rgba(169,121,139,.14), transparent 22%),
        linear-gradient(180deg,#080809 0%,#050505 48%,#030303 100%);
      overflow-x:hidden;
    }

    body::before{
      content:"";
      position:fixed;
      inset:0;
      z-index:-1;
      pointer-events:none;
      background-image:
        linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px),
        linear-gradient(90deg,rgba(255,255,255,.018) 1px, transparent 1px);
      background-size:44px 44px;
      mask-image:linear-gradient(to bottom,rgba(0,0,0,.55),transparent 76%);
    }

    button,input,select{font:inherit}
    button{cursor:pointer}
    .hidden{display:none!important}
    ::selection{background:rgba(169,121,139,.45)}
    ::-webkit-scrollbar{width:10px;height:10px}
    ::-webkit-scrollbar-track{background:#050505}
    ::-webkit-scrollbar-thumb{background:rgba(169,121,139,.55);border-radius:999px}

    .login{
      min-height:100vh;
      display:grid;
      place-items:center;
      padding:28px;
    }

    .login-shell{
      width:min(1080px,100%);
      min-height:650px;
      display:grid;
      grid-template-columns:1.12fr .88fr;
      overflow:hidden;
      border:1px solid var(--line);
      border-radius:40px;
      background:rgba(10,10,12,.86);
      box-shadow:var(--shadow);
      backdrop-filter:blur(18px);
    }

    .login-left{
      position:relative;
      padding:46px;
      border-right:1px solid var(--line);
      overflow:hidden;
      background:
        radial-gradient(circle at 20% 20%, rgba(169,121,139,.34), transparent 25%),
        radial-gradient(circle at 76% 62%, rgba(169,121,139,.16), transparent 30%),
        linear-gradient(135deg, rgba(255,255,255,.05), rgba(255,255,255,.012));
    }

    .login-left::after{
      content:"";
      position:absolute;
      right:-190px;
      bottom:-190px;
      width:430px;
      height:430px;
      border-radius:50%;
      border:1px solid rgba(169,121,139,.30);
      box-shadow:0 0 0 74px rgba(169,121,139,.045);
    }

    .brand-row{display:flex;align-items:center;gap:16px;position:relative;z-index:1}
    .logo{
      width:64px;height:64px;
      display:grid;place-items:center;
      border-radius:22px;
      background:linear-gradient(135deg,var(--brand2),var(--brand));
      color:#fff;
      font-weight:950;
      letter-spacing:.04em;
      box-shadow:0 22px 50px rgba(169,121,139,.30);
      user-select:none;
    }

    .brand-copy b{display:block;font-size:31px;line-height:.95;letter-spacing:-.06em}
    .brand-copy b span{color:var(--brand2)}
    .brand-copy small{display:block;margin-top:8px;color:var(--muted);font-size:11px;letter-spacing:.18em;text-transform:uppercase}

    .login-hero{position:relative;z-index:1;margin-top:82px;max-width:540px}
    .eyebrow{
      color:var(--brand2);
      text-transform:uppercase;
      letter-spacing:.18em;
      font-size:11px;
      font-weight:850;
      margin-bottom:12px;
    }
    .login-hero h1{
      margin:0;
      font-size:56px;
      line-height:.98;
      letter-spacing:-.08em;
    }
    .login-hero p{
      margin:24px 0 0;
      color:var(--muted);
      font-size:16px;
      line-height:1.75;
    }

    .login-points{display:flex;flex-wrap:wrap;gap:10px;margin-top:34px}
    .tag{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:9px 12px;
      border-radius:999px;
      border:1px solid rgba(169,121,139,.22);
      background:rgba(169,121,139,.08);
      color:#f1dfe7;
      font-size:12px;
      font-weight:800;
    }

    .login-right{
      display:flex;
      flex-direction:column;
      justify-content:center;
      padding:46px;
      background:rgba(5,5,5,.46);
    }
    .login-right h2{margin:0;font-size:34px;letter-spacing:-.06em}
    .login-right p{margin:12px 0 0;color:var(--muted);line-height:1.7}

    .field{margin-top:26px}
    .label{display:block;margin-bottom:9px;color:#e8dde2;font-size:13px;font-weight:800}
    .input,select{
      width:100%;
      border:1px solid var(--line);
      background:#08080a;
      color:var(--text);
      outline:none;
      border-radius:18px;
      padding:15px 16px;
      transition:.2s ease;
    }
    .input:focus,select:focus{
      border-color:rgba(169,121,139,.72);
      box-shadow:0 0 0 4px rgba(169,121,139,.13);
      background:#0c0c0f;
    }

    .btn{
      border:0;
      border-radius:18px;
      padding:13px 18px;
      color:white;
      background:linear-gradient(135deg,var(--brand),var(--brand3));
      font-weight:900;
      box-shadow:0 18px 42px rgba(169,121,139,.20);
      transition:.18s ease;
      white-space:nowrap;
    }
    .btn:hover{filter:brightness(1.07);transform:translateY(-1px)}
    .btn.secondary{background:rgba(255,255,255,.045);border:1px solid var(--line);box-shadow:none;color:var(--text)}
    .btn.ghost{background:transparent;border:1px solid rgba(169,121,139,.30);box-shadow:none}
    .btn.danger{background:rgba(239,68,68,.10);border:1px solid rgba(239,68,68,.28);box-shadow:none;color:#fecaca}
    .btn.ok{background:rgba(34,197,94,.10);border:1px solid rgba(34,197,94,.28);box-shadow:none;color:#bbf7d0}
    .btn.small{padding:9px 12px;border-radius:14px;font-size:13px}

    .app{
      display:none;
      min-height:100vh;
      grid-template-columns:var(--sidebar) minmax(0,1fr);
    }

    aside{
      position:sticky;
      top:0;
      height:100vh;
      z-index:10;
      display:flex;
      flex-direction:column;
      gap:18px;
      padding:22px 18px;
      background:rgba(5,5,6,.90);
      border-right:1px solid var(--line);
      backdrop-filter:blur(18px);
    }

    .sidebar-brand{
      display:flex;
      align-items:center;
      gap:14px;
      padding:12px 10px 18px;
      border-bottom:1px solid var(--line);
    }
    .sidebar-brand .logo{width:52px;height:52px;border-radius:18px;font-size:13px}
    .side-title b{display:block;font-size:20px;letter-spacing:-.04em}
    .side-title b span{color:var(--brand2)}
    .side-title small{display:block;color:var(--muted);font-size:12px;margin-top:5px}

    .nav{display:grid;gap:7px}
    .nav button{
      display:flex;
      align-items:center;
      gap:12px;
      width:100%;
      padding:13px 14px;
      border:1px solid transparent;
      border-radius:18px;
      background:transparent;
      color:var(--muted);
      text-align:left;
      transition:.18s ease;
    }
    .nav button:hover,.nav button.active{
      color:#fff;
      border-color:rgba(169,121,139,.32);
      background:linear-gradient(135deg,rgba(169,121,139,.16),rgba(169,121,139,.055));
      transform:translateX(2px);
    }
    .ico{
      width:28px;height:28px;
      display:grid;place-items:center;
      border-radius:10px;
      background:rgba(255,255,255,.045);
      border:1px solid rgba(255,255,255,.055);
      font-size:13px;
    }
    .nav button.active .ico{background:rgba(169,121,139,.18);border-color:rgba(169,121,139,.28)}

    .sidebar-footer{margin-top:auto;display:grid;gap:10px}
    .mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .mini-card{padding:13px;border:1px solid var(--line);border-radius:20px;background:rgba(255,255,255,.035)}
    .mini-label{color:var(--muted2);font-size:11px;letter-spacing:.12em;text-transform:uppercase}
    .mini-value{margin-top:9px;font-size:24px;font-weight:950;letter-spacing:-.05em}

    main{min-width:0;padding:30px}
    .topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:24px}
    .topbar h2{margin:0;font-size:44px;line-height:.98;letter-spacing:-.075em}
    .topbar p{margin:12px 0 0;color:var(--muted);max-width:780px;line-height:1.65}
    .actions{display:flex;gap:10px;flex-wrap:wrap}

    .view{display:none}
    .view.active{display:block}

    .hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(330px,.65fr);gap:18px;margin-bottom:18px}
    .card,.panel,.hero-card{
      border:1px solid var(--line);
      border-radius:var(--radius);
      background:linear-gradient(180deg,rgba(255,255,255,.042),rgba(255,255,255,.018));
      box-shadow:0 18px 60px rgba(0,0,0,.38);
    }
    .hero-card{position:relative;overflow:hidden;min-height:245px;padding:26px}
    .hero-card::after{
      content:"";
      position:absolute;
      right:-120px;top:-120px;
      width:290px;height:290px;border-radius:50%;
      background:radial-gradient(circle,rgba(169,121,139,.22),transparent 68%);
      pointer-events:none;
    }
    .hero-card h3{margin:0;max-width:760px;font-size:34px;line-height:1.08;letter-spacing:-.06em}
    .hero-card p{margin:16px 0 0;max-width:800px;color:var(--muted);line-height:1.75}
    .hero-tags{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}

    .stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:18px}
    .stat{position:relative;overflow:hidden;padding:19px}
    .stat::before{
      content:"";
      position:absolute;inset:0;
      background:radial-gradient(circle at top right,rgba(169,121,139,.13),transparent 44%);
      pointer-events:none;
    }
    .stat-label{position:relative;color:var(--muted);font-size:13px}
    .stat-value{position:relative;margin-top:12px;font-size:40px;font-weight:950;letter-spacing:-.06em}
    .stat-meta{position:relative;margin-top:10px;color:var(--muted2);font-size:12px}

    .split{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(340px,.85fr);gap:18px;margin-bottom:18px}
    .panel{overflow:hidden}
    .panel-head{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:18px 20px;border-bottom:1px solid var(--line)}
    .panel-head h3{margin:0;font-size:19px;letter-spacing:-.04em}
    .panel-head p{margin:7px 0 0;color:var(--muted);font-size:13px;line-height:1.45}
    .panel-body{padding:18px 20px}

    .filters{display:flex;flex-wrap:wrap;gap:10px;padding:16px 20px;border-bottom:1px solid var(--line)}
    .filters .input,.filters select{width:auto;min-width:220px}
    .filters .input{padding:13px 14px}
    .filters select{background:#08080a}

    .table-wrap{overflow:auto}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th,td{padding:14px 16px;border-bottom:1px solid rgba(255,255,255,.06);white-space:nowrap;vertical-align:top}
    th{text-align:left;color:#d4c5cc;background:rgba(255,255,255,.012);font-size:11px;text-transform:uppercase;letter-spacing:.11em}
    td.wrap,th.wrap{white-space:normal;min-width:260px}
    tr:hover td{background:rgba(255,255,255,.018)}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:#d8d8dc;font-size:12px}

    .pill{
      display:inline-flex;align-items:center;justify-content:center;
      padding:6px 10px;border-radius:999px;font-size:12px;font-weight:850;
      border:1px solid var(--line);background:rgba(255,255,255,.04);
    }
    .pill.ok{color:#86efac;border-color:rgba(34,197,94,.26);background:rgba(34,197,94,.10)}
    .pill.warn{color:#fcd34d;border-color:rgba(245,158,11,.28);background:rgba(245,158,11,.10)}
    .pill.err{color:#fca5a5;border-color:rgba(239,68,68,.28);background:rgba(239,68,68,.10)}
    .pill.info{color:#bfdbfe;border-color:rgba(96,165,250,.28);background:rgba(96,165,250,.10)}
    .pill.muted{color:#d4d4d8;border-color:rgba(212,212,216,.14);background:rgba(212,212,216,.06)}

    .priority-list{display:grid;gap:12px}
    .priority-item{padding:14px 15px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.026)}
    .priority-item h4{margin:0;font-size:14px}
    .priority-item p{margin:8px 0 0;color:var(--muted);font-size:13px;line-height:1.55}
    .priority-meta{display:flex;gap:8px;flex-wrap:wrap;margin-top:11px}
    .empty{padding:36px 18px;text-align:center;color:var(--muted)}

    pre{background:#070708;border:1px solid var(--line);color:#f5f5f5;padding:16px;border-radius:18px;max-height:520px;overflow:auto;white-space:pre-wrap;word-break:break-word}
    .modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;justify-content:center;z-index:80;padding:22px;backdrop-filter:blur(10px)}
    .modal{width:min(980px,96vw);max-height:90vh;overflow:auto;background:rgba(15,15,18,.98);border:1px solid var(--line);border-radius:28px;box-shadow:var(--shadow)}
    .modal-body{padding:20px}
    .toast{position:fixed;right:20px;bottom:20px;z-index:90;max-width:440px;background:#101012;border:1px solid var(--line);border-radius:20px;padding:14px 16px;color:var(--text);box-shadow:0 18px 60px rgba(0,0,0,.38)}

    @media(max-width:1220px){
      .app{grid-template-columns:1fr}
      aside{position:relative;height:auto}
      .sidebar-footer{display:none}
      .nav{grid-template-columns:repeat(3,minmax(0,1fr));display:grid}
      .hero,.split{grid-template-columns:1fr}
      .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
    }
    @media(max-width:820px){
      main{padding:18px}
      .login-shell{grid-template-columns:1fr;min-height:auto}
      .login-left{display:none}
      .login-right{padding:30px}
      .topbar{flex-direction:column}
      .topbar h2{font-size:34px}
      .stats{grid-template-columns:1fr}
      .nav{grid-template-columns:repeat(2,minmax(0,1fr))}
      .filters{flex-direction:column}
      .filters .input,.filters select{width:100%;min-width:0}
    }
    @media(max-width:540px){
      .login{padding:16px}
      .login-right{padding:24px}
      .brand-copy b{font-size:26px}
      .nav{grid-template-columns:1fr}
      aside{padding:16px}
      main{padding:14px}
      .hero-card,.panel-body,.panel-head{padding:16px}
      .hero-card h3{font-size:27px}
      th,td{padding:12px}
    }
  </style>
</head>

<body>
  <section id="login" class="login">
    <div class="login-shell">
      <div class="login-left">
        <div class="brand-row">
          <div class="logo">MIA</div>
          <div class="brand-copy">
            <b>MIA <span>AUTODOC</span></b>
            <small>Operação documental inteligente</small>
          </div>
        </div>

        <div class="login-hero">
          <div class="eyebrow">Painel premium do cliente</div>
          <h1>Controle elegante para arquivos, robôs e SharePoint.</h1>
          <p>Uma experiência executiva, moderna e segura para acompanhar a operação AutoDoc sem expor chaves, senhas ou dados técnicos sensíveis.</p>
          <div class="login-points">
            <span class="tag">API Key protegida</span>
            <span class="tag">Dashboard executivo</span>
            <span class="tag">Área técnica separada</span>
          </div>
        </div>
      </div>

      <div class="login-right">
        <div class="brand-row" style="margin-bottom:28px">
          <div class="logo">MIA</div>
          <div class="brand-copy">
            <b>MIA <span>AUTODOC</span></b>
            <small>Acesso seguro</small>
          </div>
        </div>

        <h2>Entrar no painel</h2>
        <p>Use a senha definida para o cliente. A sessão é protegida por cookie seguro e expira automaticamente.</p>

        <div class="field">
          <label class="label">Senha do painel</label>
          <input id="password" class="input" type="password" placeholder="Digite a senha" autocomplete="current-password" onkeydown="if(event.key==='Enter') login()" />
        </div>

        <button class="btn" style="width:100%;margin-top:10px" onclick="login()">Entrar</button>
        <p id="loginError" style="color:#fca5a5;margin-top:16px"></p>
      </div>
    </div>
  </section>

  <section id="app" class="app">
    <aside>
      <div class="sidebar-brand">
        <div class="logo">MIA</div>
        <div class="side-title">
          <b>MIA <span>AUTODOC</span></b>
          <small>Painel do Cliente</small>
        </div>
      </div>

      <nav class="nav">
        <button class="active" data-view="dashboard"><span class="ico">◆</span> Dashboard</button>
        <button data-view="files"><span class="ico">▣</span> Arquivos</button>
        <button data-view="emails"><span class="ico">✉</span> E-mails</button>
        <button data-view="robot"><span class="ico">⚙</span> Fila do Robô</button>
        <button data-view="sharepoint"><span class="ico">⌘</span> SharePoint</button>
        <button data-view="system"><span class="ico">◌</span> Sistema</button>
      </nav>

      <div class="sidebar-footer">
        <div class="mini-grid">
          <div class="mini-card"><div class="mini-label">Salvos</div><div id="sideSaved" class="mini-value">0</div></div>
          <div class="mini-card"><div class="mini-label">Pendentes</div><div id="sidePending" class="mini-value">0</div></div>
        </div>
        <button class="btn danger" onclick="logout()">Sair</button>
      </div>
    </aside>

    <main>
      <header class="topbar">
        <div>
          <div id="pageEyebrow" class="eyebrow">Visão executiva</div>
          <h2 id="pageTitle">Dashboard</h2>
          <p id="pageSubtitle">Resumo claro da operação, com foco em resultados, pendências e pontos de atenção.</p>
        </div>
        <div class="actions">
          <button class="btn secondary small" onclick="refreshCurrent()">Atualizar</button>
        </div>
      </header>

      <section id="view-dashboard" class="view active">
        <div class="hero">
          <div class="hero-card">
            <div class="eyebrow">MIA AUTODOC</div>
            <h3>Operação documental automatizada com uma experiência premium.</h3>
            <p>O dashboard inicial mostra somente o que importa para tomada de decisão. Indicadores técnicos, integrações e diagnósticos ficam separados na área Sistema.</p>
            <div class="hero-tags">
              <span class="tag">Dashboard executivo</span>
              <span class="tag">Preto premium</span>
              <span class="tag">Destaque #A9798B</span>
              <span class="tag">Responsivo</span>
            </div>
          </div>

          <div class="hero-card">
            <div class="eyebrow">Prioridades</div>
            <div id="priorityList" class="priority-list"></div>
          </div>
        </div>

        <div class="stats">
          <div class="card stat"><div class="stat-label">Arquivos totais</div><div id="kFiles" class="stat-value">--</div><div class="stat-meta">Volume detectado na operação</div></div>
          <div class="card stat"><div class="stat-label">Aguardando download</div><div id="kPending" class="stat-value">--</div><div class="stat-meta">Itens prontos para automação</div></div>
          <div class="card stat"><div class="stat-label">Salvos</div><div id="kSaved" class="stat-value">--</div><div class="stat-meta">Arquivos concluídos</div></div>
          <div class="card stat"><div class="stat-label">Com atenção</div><div id="kErrors" class="stat-value">--</div><div class="stat-meta">Erros ou exceções</div></div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <div><h3>Arquivos recentes</h3><p>Últimos itens detectados e processados pelo sistema.</p></div>
            <button class="btn secondary small" onclick="nav('files')">Ver todos</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Arquivo</th><th>Projeto</th><th>Disciplina</th><th>Status</th><th>Score</th><th class="wrap">Destino</th></tr></thead>
              <tbody id="recentFiles"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-files" class="view">
        <div class="panel">
          <div class="panel-head"><div><h3>Arquivos</h3><p>Controle dos arquivos extraídos, destinos e ações.</p></div></div>
          <div class="filters">
            <input id="fileSearch" class="input" placeholder="Buscar por arquivo, projeto, disciplina ou caminho" oninput="renderFiles()" />
            <select id="fileStatus" onchange="renderFiles()"><option value="">Todos status</option></select>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Arquivo</th><th>Projeto</th><th>Disciplina</th><th>AutoDoc</th><th class="wrap">SharePoint</th><th>Status</th><th>Score</th><th>Ações</th></tr></thead>
              <tbody id="filesRows"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-emails" class="view">
        <div class="panel">
          <div class="panel-head"><div><h3>E-mails</h3><p>Entradas recebidas pelo sistema e origem dos arquivos detectados.</p></div></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Data</th><th class="wrap">Assunto</th><th>Remetente</th><th>Status</th><th>Tipo</th><th>Ações</th></tr></thead>
              <tbody id="emailsRows"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-robot" class="view">
        <div class="panel">
          <div class="panel-head">
            <div><h3>Fila do Robô</h3><p>Status dos jobs, tentativas e reprocessamentos automáticos.</p></div>
            <button class="btn secondary small" onclick="claimNext()">Pegar próximo job</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Job</th><th>Arquivo</th><th>Status</th><th>Tentativas</th><th>Worker</th><th>Próxima</th><th class="wrap">Erro</th><th>Ações</th></tr></thead>
              <tbody id="jobsRows"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-sharepoint" class="view">
        <div class="panel">
          <div class="panel-head"><div><h3>SharePoint</h3><p>Sites, bibliotecas e mapeamentos conectados à operação.</p></div></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Projeto</th><th class="wrap">URL</th><th>Biblioteca</th><th>Ativo</th><th>HML</th><th>Mapa</th><th>Prod</th><th>Ações</th></tr></thead>
              <tbody id="sitesRows"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="view-system" class="view">
        <div class="split">
          <div class="panel">
            <div class="panel-head">
              <div><h3>Status dos serviços</h3><p>Diagnóstico técnico separado do dashboard executivo.</p></div>
              <button class="btn secondary small" onclick="loadHealth()">Testar</button>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Serviço</th><th>Status</th><th>Detalhes</th></tr></thead>
                <tbody id="healthRows"></tbody>
              </table>
            </div>
          </div>

          <div class="panel">
            <div class="panel-head"><div><h3>Erros recentes</h3><p>Ocorrências técnicas e operacionais registradas.</p></div></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Tipo</th><th class="wrap">Mensagem</th><th>Status</th><th>Ações</th></tr></thead>
                <tbody id="errorsRows"></tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head"><div><h3>Configurações carregadas</h3><p>Resumo seguro da configuração do ambiente, sem exibir segredos.</p></div></div>
          <div class="panel-body"><pre id="settingsJson">{}</pre></div>
        </div>
      </section>
    </main>
  </section>

  <div id="modal" class="modal-backdrop" onclick="if(event.target.id==='modal') closeModal()">
    <div class="modal">
      <div class="panel-head"><div><h3 id="modalTitle">Detalhes</h3></div><button class="btn secondary small" onclick="closeModal()">Fechar</button></div>
      <div class="modal-body"><pre id="modalBody"></pre></div>
    </div>
  </div>

  <script>
    const state = { current:'dashboard', summary:null, files:[], emails:[], jobs:[], sites:[], errors:[] };

    const meta = {
      dashboard:['Visão executiva','Dashboard','Resumo claro da operação, com foco em resultados, pendências e pontos de atenção.'],
      files:['Operação documental','Arquivos','Controle dos arquivos extraídos, destinos e ações operacionais.'],
      emails:['Origem dos dados','E-mails','Entradas recebidas pelo sistema e origem dos arquivos detectados.'],
      robot:['Automação','Fila do Robô','Status dos jobs, tentativas e reprocessamentos automáticos.'],
      sharepoint:['Infraestrutura documental','SharePoint','Sites, bibliotecas e mapeamentos conectados à operação.'],
      system:['Área técnica','Sistema','Status dos serviços, erros e configurações técnicas separadas do dashboard.']
    };

    async function call(path, options={}){
      const response = await fetch(path, {
        credentials:'include',
        headers:{'Content-Type':'application/json', ...(options.headers || {})},
        ...options
      });

      const text = await response.text();
      let data;
      try { data = text ? JSON.parse(text) : {}; } catch { data = {raw:text}; }

      if(response.status === 401){
        showLogin();
        throw new Error('Sessão expirada. Entre novamente.');
      }

      if(!response.ok) throw new Error(data.detail || data.message || JSON.stringify(data));
      return data;
    }

    function showLogin(){ document.getElementById('login').style.display='grid'; document.getElementById('app').style.display='none'; }
    function showApp(){ document.getElementById('login').style.display='none'; document.getElementById('app').style.display='grid'; }

    async function login(){
      const error = document.getElementById('loginError');
      error.textContent = '';
      try{
        await call('/panel/login', { method:'POST', body: JSON.stringify({password: document.getElementById('password').value}) });
        document.getElementById('password').value = '';
        await initApp();
      }catch(e){ error.textContent = e.message; }
    }

    async function logout(){ await call('/panel/logout', {method:'POST'}).catch(()=>{}); showLogin(); }
    async function check(){ try{ await call('/panel/api/summary'); await initApp(); }catch{ showLogin(); } }

    async function initApp(){
      showApp();
      document.querySelectorAll('.nav button').forEach(button => button.onclick = () => nav(button.dataset.view));
      await loadCore();
      nav(state.current || 'dashboard');
    }

    function nav(view){
      state.current = view;
      document.querySelectorAll('.view').forEach(section => section.classList.toggle('active', section.id === 'view-' + view));
      document.querySelectorAll('.nav button').forEach(button => button.classList.toggle('active', button.dataset.view === view));
      const [eyebrow,title,subtitle] = meta[view] || meta.dashboard;
      document.getElementById('pageEyebrow').textContent = eyebrow;
      document.getElementById('pageTitle').textContent = title;
      document.getElementById('pageSubtitle').textContent = subtitle;
      loadView(view).catch(e => toast(e.message));
    }

    async function refreshCurrent(){ await loadView(state.current); toast('Painel atualizado.'); }
    async function loadCore(){ await Promise.all([loadSummary(), loadFiles()]); renderDashboard(); }

    async function loadView(view){
      if(view === 'dashboard') await loadCore();
      else if(view === 'files'){ if(!state.files.length) await loadFiles(); renderFiles(); }
      else if(view === 'emails') await loadEmails();
      else if(view === 'robot') await loadJobs();
      else if(view === 'sharepoint') await loadSites();
      else if(view === 'system') await Promise.all([loadHealth(), loadErrors(), loadSettings()]);
    }

    async function loadSummary(){
      const summary = await call('/panel/api/summary');
      state.summary = summary;
      document.getElementById('kFiles').textContent = summary.files_total ?? 0;
      document.getElementById('kPending').textContent = summary.files_waiting_download ?? 0;
      document.getElementById('kSaved').textContent = summary.files_saved ?? 0;
      document.getElementById('kErrors').textContent = (summary.files_error ?? 0) + (summary.errors_total ?? 0);
      document.getElementById('sideSaved').textContent = summary.files_saved ?? 0;
      document.getElementById('sidePending').textContent = summary.files_waiting_download ?? 0;
    }

    async function loadFiles(){
      const data = await call('/panel/api/files?limit=500');
      state.files = data.files || [];
      fillFileStatus();
      renderFiles();
      renderDashboard();
    }

    function fillFileStatus(){
      const select = document.getElementById('fileStatus');
      if(!select) return;
      const current = select.value;
      const statuses = [...new Set(state.files.map(f => f.status).filter(Boolean))].sort();
      select.innerHTML = '<option value="">Todos status</option>' + statuses.map(s => `<option>${escapeHtml(s)}</option>`).join('');
      select.value = current;
    }

    function renderDashboard(){ renderRecentFiles(); renderPriorities(); }

    function renderRecentFiles(){
      const rows = document.getElementById('recentFiles');
      if(!rows) return;
      rows.innerHTML = state.files.slice(0,8).map(f => `
        <tr>
          <td><b>${escapeHtml(f.file_name)}</b></td>
          <td>${escapeHtml(f.project_detected)}</td>
          <td>${escapeHtml(f.discipline_detected)}</td>
          <td>${statusPill(f.status)}</td>
          <td>${scorePill(f.confidence_score)}</td>
          <td class="wrap">${escapeHtml(f.sharepoint_final_path || f.sharepoint_suggested_path || f.autodoc_path)}</td>
        </tr>
      `).join('') || emptyRow(6, 'Nenhum arquivo recente encontrado.');
    }

    function renderPriorities(){
      const box = document.getElementById('priorityList');
      if(!box) return;

      const important = state.files
        .filter(f => String(f.status || '').toUpperCase().includes('AGUARDANDO') || String(f.status || '').toUpperCase().includes('ERRO') || f.error_message)
        .slice(0,5);

      box.innerHTML = important.map(f => `
        <div class="priority-item">
          <h4>${escapeHtml(f.file_name || 'Arquivo sem nome')}</h4>
          <p>Projeto: <b>${escapeHtml(f.project_detected || 'Não identificado')}</b> · Disciplina: <b>${escapeHtml(f.discipline_detected || 'Não identificada')}</b></p>
          <div class="priority-meta">
            ${statusPill(f.status)}
            ${scorePill(f.confidence_score)}
            <span class="pill muted">${escapeHtml(f.autodoc_path || 'Sem caminho')}</span>
          </div>
        </div>
      `).join('') || '<div class="empty">Nenhuma prioridade no momento.</div>';
    }

    function renderFiles(){
      const query = (document.getElementById('fileSearch')?.value || '').toLowerCase();
      const status = document.getElementById('fileStatus')?.value || '';
      const rows = document.getElementById('filesRows');
      if(!rows) return;

      const filtered = state.files.filter(f => {
        const haystack = [f.file_name, f.project_detected, f.discipline_detected, f.autodoc_path, f.sharepoint_suggested_path, f.status].join(' ').toLowerCase();
        if(status && f.status !== status) return false;
        if(query && !haystack.includes(query)) return false;
        return true;
      });

      rows.innerHTML = filtered.map(f => `
        <tr>
          <td><b>${escapeHtml(f.file_name)}</b><br><span class="mono">${shortId(f.id)}</span></td>
          <td>${escapeHtml(f.project_detected)}</td>
          <td>${escapeHtml(f.discipline_detected)}</td>
          <td>${escapeHtml(f.autodoc_path)}</td>
          <td class="wrap">${escapeHtml(f.sharepoint_final_path || f.sharepoint_suggested_path)}</td>
          <td>${statusPill(f.status)}</td>
          <td>${scorePill(f.confidence_score)}</td>
          <td>
            <div class="actions">
              <button class="btn secondary small" onclick='showJson("Arquivo", ${jsonAttr(f)})'>Ver</button>
              <button class="btn ok small" onclick="fileAction('/panel/api/files/${f.id}/approve')">Aprovar</button>
              <button class="btn small" onclick="fileAction('/panel/api/files/${f.id}/queue')">Fila</button>
              <button class="btn danger small" onclick="fileAction('/panel/api/files/${f.id}/ignore')">Ignorar</button>
            </div>
          </td>
        </tr>
      `).join('') || emptyRow(8, 'Nenhum arquivo encontrado.');
    }

    async function loadEmails(){
      const data = await call('/panel/api/emails?limit=300');
      state.emails = data.emails || [];
      document.getElementById('emailsRows').innerHTML = state.emails.map(e => `
        <tr>
          <td>${formatDate(e.created_at || e.received_at)}</td>
          <td class="wrap">${escapeHtml(e.subject)}</td>
          <td>${escapeHtml(e.sender)}</td>
          <td>${statusPill(e.status)}</td>
          <td>${escapeHtml(e.email_type)}</td>
          <td><button class="btn secondary small" onclick='showJson("E-mail", ${jsonAttr(e)})'>Ver</button></td>
        </tr>
      `).join('') || emptyRow(6, 'Nenhum e-mail encontrado.');
    }

    async function loadJobs(){
      const data = await call('/panel/api/robot/jobs?limit=300');
      state.jobs = data.jobs || [];
      document.getElementById('jobsRows').innerHTML = state.jobs.map(j => `
        <tr>
          <td><span class="mono">${shortId(j.id)}</span></td>
          <td><span class="mono">${shortId(j.file_id)}</span></td>
          <td>${statusPill(j.status)}</td>
          <td>${j.attempts || 0}/${j.max_attempts || 3}</td>
          <td>${escapeHtml(j.locked_by)}</td>
          <td>${formatDate(j.next_attempt_at)}</td>
          <td class="wrap">${escapeHtml(shortText(j.last_error))}</td>
          <td><button class="btn secondary small" onclick='showJson("Job", ${jsonAttr(j)})'>Ver</button></td>
        </tr>
      `).join('') || emptyRow(8, 'Nenhum job encontrado.');
    }

    async function claimNext(){
      const data = await call('/panel/api/robot/jobs/next', {method:'POST'});
      showJson('Próximo job', data);
      await loadJobs();
    }

    async function loadSites(){
      const data = await call('/panel/api/sites');
      state.sites = data.sites || [];
      document.getElementById('sitesRows').innerHTML = state.sites.map(s => `
        <tr>
          <td><b>${escapeHtml(s.project_name || s.project_normalized)}</b><br><span class="mono">${shortId(s.id)}</span></td>
          <td class="wrap">${escapeHtml(s.site_url)}</td>
          <td>${escapeHtml(s.library_name)}</td>
          <td>${boolPill(s.active)}</td>
          <td>${boolPill(s.hml_ready)}</td>
          <td>${boolPill(s.folder_map_ready)}</td>
          <td>${boolPill(s.prod_ready)}</td>
          <td><button class="btn secondary small" onclick='showJson("Site", ${jsonAttr(s)})'>Ver</button></td>
        </tr>
      `).join('') || emptyRow(8, 'Nenhum site cadastrado.');
    }

    async function loadHealth(){
      const data = await call('/panel/api/health');
      document.getElementById('healthRows').innerHTML = Object.entries(data).map(([name, value]) => `
        <tr>
          <td>${escapeHtml(name)}</td>
          <td>${pill(value.ok !== false ? 'OK' : 'ERRO', value.ok !== false ? 'ok' : 'err')}</td>
          <td><button class="btn secondary small" onclick='showJson("${escapeHtml(name)}", ${jsonAttr(value)})'>Ver</button></td>
        </tr>
      `).join('') || emptyRow(3, 'Sem dados técnicos.');
    }

    async function loadErrors(){
      const data = await call('/panel/api/errors?limit=300');
      state.errors = data.errors || [];
      document.getElementById('errorsRows').innerHTML = state.errors.map(e => `
        <tr>
          <td>${escapeHtml(e.error_type)}</td>
          <td class="wrap">${escapeHtml(e.message)}</td>
          <td>${statusPill(e.status)}</td>
          <td><button class="btn secondary small" onclick='showJson("Erro", ${jsonAttr(e)})'>Ver</button></td>
        </tr>
      `).join('') || emptyRow(4, 'Nenhum erro registrado.');
    }

    async function loadSettings(){
      const data = await call('/panel/api/settings');
      document.getElementById('settingsJson').textContent = JSON.stringify(data, null, 2);
    }

    async function fileAction(path){
      try{
        const data = await call(path, {method:'POST'});
        showJson('Resultado', data);
        await loadCore();
      }catch(e){ toast(e.message); }
    }

    function statusPill(value){
      const text = value || '—';
      const up = String(text).toUpperCase();
      let cls = 'muted';
      if(up.includes('DONE') || up.includes('OK') || up.includes('SALVO')) cls='ok';
      else if(up.includes('PENDING') || up.includes('AGUARDANDO') || up.includes('RUNNING')) cls='warn';
      else if(up.includes('ERRO') || up.includes('ERROR') || up.includes('FAIL')) cls='err';
      else if(up.includes('PRONTO') || up.includes('RECEBIDO')) cls='info';
      return pill(text, cls);
    }

    function scorePill(value){
      const n = Number(value || 0);
      const cls = n >= 90 ? 'ok' : n >= 60 ? 'warn' : 'err';
      return pill(Number.isFinite(n) ? n : 0, cls);
    }

    function boolPill(value){ return pill(value ? 'Sim' : 'Não', value ? 'ok' : 'muted'); }
    function pill(text, cls){ return `<span class="pill ${cls}">${escapeHtml(text)}</span>`; }
    function emptyRow(cols, message){ return `<tr><td colspan="${cols}" class="empty">${escapeHtml(message)}</td></tr>`; }

    function escapeHtml(value){
      return String(value ?? '').replace(/[&<>"']/g, char => ({
        '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#039;'
      }[char]));
    }

    function shortId(value){
      const id = String(value || '');
      return id.length > 12 ? id.slice(0,8) + '…' + id.slice(-4) : id;
    }

    function shortText(value){
      const text = typeof value === 'string' ? value : JSON.stringify(value || '');
      return text.length > 130 ? text.slice(0,130) + '…' : text;
    }

    function formatDate(value){
      if(!value) return '';
      try{ return new Date(value).toLocaleString('pt-BR'); }catch{ return value; }
    }

    function jsonAttr(value){ return encodeURIComponent(JSON.stringify(value ?? {}, null, 2)); }

    function showJson(title, encoded){
      let content = encoded;
      if(typeof encoded === 'string'){
        try{ content = decodeURIComponent(encoded); }catch{}
      }
      document.getElementById('modalTitle').textContent = title;
      document.getElementById('modalBody').textContent = typeof content === 'string' ? content : JSON.stringify(content, null, 2);
      document.getElementById('modal').style.display = 'flex';
    }

    function closeModal(){ document.getElementById('modal').style.display = 'none'; }

    function toast(message){
      const el = document.createElement('div');
      el.className = 'toast';
      el.textContent = message;
      document.body.appendChild(el);
      setTimeout(() => el.remove(), 4200);
    }

    window.addEventListener('keydown', event => {
      if(event.key === 'Escape') closeModal();
    });

    check();
  </script>
</body>
</html>"""



@app.get("/panel", response_class=HTMLResponse)
def panel_page():
    return HTMLResponse(_panel_html())



@app.get("/panel/env-check")
def panel_env_check():
    """
    Diagnóstico seguro do painel.
    Não exibe senhas/chaves, apenas informa se a API conseguiu ler as variáveis.
    """
    return {
        "ok": True,
        "panel_password_from_os": bool(os.getenv("PANEL_PASSWORD") or os.getenv("AUTODOC_PANEL_PASSWORD")),
        "panel_password_from_env_file": bool(_read_env_file_value("PANEL_PASSWORD") or _read_env_file_value("AUTODOC_PANEL_PASSWORD")),
        "panel_password_configured": bool(_panel_password()),
        "panel_secret_from_os": bool(os.getenv("PANEL_SECRET_KEY") or os.getenv("AUTODOC_PANEL_SECRET_KEY")),
        "panel_secret_from_env_file": bool(_read_env_file_value("PANEL_SECRET_KEY") or _read_env_file_value("AUTODOC_PANEL_SECRET_KEY")),
        "panel_secret_configured": bool(_panel_secret()),
        "cookie_secure": str(os.getenv("PANEL_COOKIE_SECURE", "true")).lower() not in {"0", "false", "no", "nao", "não"},
    }


@app.post("/panel/login")
def panel_login(payload: PanelLogin, response: Response):
    configured = _panel_password()

    if not configured:
        raise HTTPException(500, "PANEL_PASSWORD não configurado no servidor")

    if not hmac.compare_digest(payload.password or "", configured):
        raise HTTPException(401, "Senha inválida")

    token = _create_panel_token(payload.username or "cliente")

    response.set_cookie(
        key=PANEL_COOKIE_NAME,
        value=token,
        httponly=True,
        # Em produção/EasyPanel use HTTPS. Em localhost, permita cookie sem secure.
        secure=str(os.getenv("PANEL_COOKIE_SECURE", "true")).lower() not in {"0", "false", "no", "nao", "não"},
        samesite="lax",
        max_age=PANEL_SESSION_TTL_SECONDS,
        path="/",
    )

    return {"ok": True, "message": "Login realizado"}


@app.post("/panel/logout")
def panel_logout(response: Response):
    response.delete_cookie(PANEL_COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/panel/api/summary")
def panel_summary(_: Dict[str, Any] = Depends(require_panel_session)):
    files = list_rows("autodoc_files", limit=5000)
    errors = list_rows("autodoc_errors", limit=5000)

    def has_status(row, *parts):
        s = str(row.get("status") or "").upper()
        return any(p.upper() in s for p in parts)

    return {
        "ok": True,
        "files_total": len(files),
        "files_waiting_download": len([f for f in files if has_status(f, "AGUARDANDO_DOWNLOAD")]),
        "files_saved": len([f for f in files if has_status(f, "SALVO", "SHAREPOINT")]),
        "files_error": len([f for f in files if has_status(f, "ERRO", "ERROR") or f.get("error_message")]),
        "errors_total": len(errors),
    }


@app.get("/panel/api/health")
def panel_health(_: Dict[str, Any] = Depends(require_panel_session)):
    out: Dict[str, Any] = {}

    try:
        out["api"] = health()
    except Exception as e:
        out["api"] = {"ok": False, "error": str(e)}

    try:
        out["security"] = security_status()
    except Exception as e:
        out["security"] = {"ok": False, "error": str(e)}

    try:
        out["db"] = health_check()
    except Exception as e:
        out["db"] = {"ok": False, "error": str(e)}

    try:
        sharepoint.token()
        out["sharepoint"] = {"ok": True, "hostname": settings.sharepoint_hostname}
    except Exception as e:
        out["sharepoint"] = {"ok": False, "error": str(e)}

    return out


@app.get("/panel/api/settings")
def panel_settings(_: Dict[str, Any] = Depends(require_panel_session)):
    return {
        "ok": True,
        "app_name": settings.app_name,
        "app_env": settings.app_env,
        "production_enabled": settings.is_prod_enabled,
        "multi_site_sharepoint": True,
        "sharepoint_hostname": settings.sharepoint_hostname,
        "autodoc_url_configured": bool(getattr(settings, "autodoc_url", "")),
        "autodoc_login_url_configured": bool(getattr(settings, "autodoc_login_url", "")),
        "autodoc_user_configured": bool(getattr(settings, "autodoc_user", "")),
        "autodoc_headless": getattr(settings, "autodoc_headless", None),
        "panel_password_configured": bool(_panel_password()),
        "panel_secret_configured": bool(_panel_secret()),
    }


@app.get("/panel/api/files")
def panel_files(
    status: str = "",
    limit: int = 500,
    _: Dict[str, Any] = Depends(require_panel_session),
):
    return {
        "ok": True,
        "files": list_rows(
            "autodoc_files",
            limit=limit,
            filters={"status": status} if status else None,
        ),
    }


@app.get("/panel/api/emails")
def panel_emails(
    limit: int = 300,
    _: Dict[str, Any] = Depends(require_panel_session),
):
    return {
        "ok": True,
        "emails": list_rows("autodoc_emails", limit=limit),
    }


@app.get("/panel/api/robot/jobs")
def panel_robot_jobs(
    status: str = "",
    limit: int = 300,
    _: Dict[str, Any] = Depends(require_panel_session),
):
    return {
        "ok": True,
        "jobs": list_rows(
            "autodoc_robot_queue",
            limit=limit,
            filters={"status": status} if status else None,
        ),
    }


@app.post("/panel/api/robot/jobs/next")
def panel_robot_next(_: Dict[str, Any] = Depends(require_panel_session)):
    job = claim_next_robot_job("dashboard-manual")

    if not job:
        return {
            "ok": False,
            "message": "Nenhum job PENDING disponível",
            "job": None,
        }

    file_id = job.get("file_id") or (job.get("payload") or {}).get("id")
    file_row = get_row("autodoc_files", file_id) if file_id else None

    return {
        "ok": True,
        "job": job,
        "file": file_row,
    }


@app.get("/panel/api/sites")
def panel_sites(_: Dict[str, Any] = Depends(require_panel_session)):
    return {
        "ok": True,
        "sites": list_rows("autodoc_sharepoint_sites", limit=1000),
    }


@app.get("/panel/api/errors")
def panel_errors(
    limit: int = 300,
    _: Dict[str, Any] = Depends(require_panel_session),
):
    return {
        "ok": True,
        "errors": list_rows("autodoc_errors", limit=limit),
    }


@app.post("/panel/api/files/{file_id}/approve")
def panel_file_approve(file_id: str, _: Dict[str, Any] = Depends(require_panel_session)):
    return file_approve(file_id, True)


@app.post("/panel/api/files/{file_id}/ignore")
def panel_file_ignore(file_id: str, _: Dict[str, Any] = Depends(require_panel_session)):
    return file_ignore(file_id, True)


@app.post("/panel/api/files/{file_id}/queue")
def panel_file_queue(file_id: str, _: Dict[str, Any] = Depends(require_panel_session)):
    return queue_robot(file_id, True)


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
# Robot queue / Worker — Fase 6
# =========================================================

@app.post("/robot/jobs/next")
def robot_jobs_next(
    payload: RobotNextRequest,
    _: bool = Depends(require_api_key),
):
    """
    Busca o próximo job PENDING e bloqueia para o worker informado.
    """
    try:
        job = claim_next_robot_job(payload.worker_id)

        if not job:
            return {
                "ok": False,
                "message": "Nenhum job PENDING disponível",
                "job": None,
            }

        file_id = job.get("file_id") or (job.get("payload") or {}).get("id")
        file_row = get_row("autodoc_files", file_id) if file_id else None

        return {
            "ok": True,
            "job": job,
            "file": file_row,
        }

    except Exception as e:
        insert_error(
            "ROBOT_NEXT_JOB_ERROR",
            str(e),
            payload=payload.model_dump(),
        )
        raise HTTPException(500, str(e))


@app.get("/robot/jobs/{job_id}")
def robot_job_get(
    job_id: str,
    _: bool = Depends(require_api_key),
):
    job = get_robot_job(job_id)

    if not job:
        raise HTTPException(404, "Job não encontrado")

    file_id = job.get("file_id") or (job.get("payload") or {}).get("id")
    file_row = get_row("autodoc_files", file_id) if file_id else None

    return {
        "ok": True,
        "job": job,
        "file": file_row,
    }


@app.post("/robot/jobs/{job_id}/complete")
def robot_job_complete(
    job_id: str,
    payload: RobotCompleteRequest,
    _: bool = Depends(require_api_key),
):
    """
    Marca o job como DONE e atualiza autodoc_files como salvo no SharePoint.
    """
    job = get_robot_job(job_id)

    if not job:
        raise HTTPException(404, "Job não encontrado")

    file_id = payload.file_id or job.get("file_id") or (job.get("payload") or {}).get("id")

    if not file_id:
        raise HTTPException(400, "file_id ausente no job/payload")

    now = now_iso()

    result = {
        **(payload.result or {}),
        "file_id": file_id,
        "local_path": payload.local_path,
        "local_sha256": payload.local_sha256,
        "local_size_bytes": payload.local_size_bytes,
        "sharepoint_item_id": payload.sharepoint_item_id,
        "sharepoint_web_url": payload.sharepoint_web_url,
        "sharepoint_final_path": payload.sharepoint_final_path,
        "saved_environment": payload.saved_environment or settings.app_env,
        "completed_at": now,
    }

    try:
        file_update = _clean_dict(
            {
                "status": _safe_get_status("FILE_SALVO_SHAREPOINT", "SALVO_SHAREPOINT"),
                "local_path": payload.local_path,
                "local_sha256": payload.local_sha256,
                "local_size_bytes": payload.local_size_bytes,
                "sharepoint_item_id": payload.sharepoint_item_id,
                "sharepoint_web_url": payload.sharepoint_web_url,
                "sharepoint_final_path": payload.sharepoint_final_path,
                "saved_environment": payload.saved_environment or settings.app_env,
                "download_finished_at": now,
                "upload_finished_at": now,
                "error_message": None,
                "error_type": None,
            }
        )

        file_row = update_row("autodoc_files", file_id, file_update)
        job_row = complete_robot_job(job_id, result)

        insert_history(
            email_id=file_row.get("email_id"),
            file_id=file_id,
            action="ROBOT_JOB_DONE",
            file_name=file_row.get("file_name"),
            to_path=file_row.get("sharepoint_final_path") or file_row.get("sharepoint_suggested_path"),
            environment=settings.app_env,
            status="DONE",
            message="Robô finalizou job e marcou arquivo como salvo no SharePoint",
            payload={
                "job_id": job_id,
                "result": result,
            },
        )

        return {
            "ok": True,
            "status": "DONE",
            "job": job_row,
            "file": file_row,
        }

    except Exception as e:
        insert_error(
            "ROBOT_COMPLETE_JOB_ERROR",
            str(e),
            file_id=file_id,
            payload={
                "job_id": job_id,
                "payload": payload.model_dump(),
            },
        )
        raise HTTPException(500, str(e))


@app.post("/robot/jobs/{job_id}/fail")
def robot_job_fail(
    job_id: str,
    payload: RobotFailRequest,
    _: bool = Depends(require_api_key),
):
    """
    Registra falha do job e decide retry ou ERROR.
    """
    job = get_robot_job(job_id)

    if not job:
        raise HTTPException(404, "Job não encontrado")

    file_id = payload.file_id or job.get("file_id") or (job.get("payload") or {}).get("id")
    attempts = int(job.get("attempts") or 0)
    max_attempts = int(job.get("max_attempts") or 3)

    error_payload = {
        "error_type": payload.error_type,
        "message": payload.message,
        "details": payload.details or {},
        "attempts": attempts,
        "max_attempts": max_attempts,
        "retryable": payload.retryable,
        "failed_at": now_iso(),
    }

    try:
        job_row = fail_robot_job(
            job_id,
            error_payload,
            retryable=payload.retryable,
            retry_delay_minutes=payload.retry_delay_minutes,
        )

        final_error = job_row.get("status") == "ERROR"

        if file_id:
            file_status = "ERRO_ROBO" if final_error else _safe_get_status("FILE_AGUARDANDO_DOWNLOAD", "AGUARDANDO_DOWNLOAD")

            update_row(
                "autodoc_files",
                file_id,
                {
                    "status": file_status,
                    "error_type": payload.error_type,
                    "error_message": payload.message,
                    "retry_count": attempts,
                    "last_processed_at": now_iso(),
                },
            )

            # Não podemos deixar falha de auditoria derrubar o endpoint /fail.
            # Algumas instalações têm CHECK constraint em autodoc_errors.status
            # e não aceitam valores como RETRY. Por isso usamos sempre ABERTO.
            try:
                insert_error(
                    payload.error_type,
                    payload.message,
                    file_id=file_id,
                    payload={
                        "job_id": job_id,
                        "details": payload.details,
                        "job_status": job_row.get("status"),
                        "retryable": payload.retryable,
                    },
                    status="ABERTO",
                )
            except Exception:
                # A falha principal do job já foi gravada em autodoc_robot_queue.last_error.
                # Não quebrar o endpoint apenas por erro de log/auditoria.
                pass

        return {
            "ok": True,
            "status": job_row.get("status"),
            "retry_scheduled": job_row.get("status") == "PENDING",
            "job": job_row,
        }

    except Exception as e:
        try:
            insert_error(
                "ROBOT_FAIL_JOB_ERROR",
                str(e),
                file_id=file_id,
                payload={
                    "job_id": job_id,
                    "payload": payload.model_dump(),
                },
                status="ABERTO",
            )
        except Exception:
            pass
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
        try:
            res = autodoc_download(project, path, name, context=f or payload)
        except TypeError:
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
