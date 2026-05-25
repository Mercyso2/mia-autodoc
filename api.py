from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from pathlib import Path
import shutil
import hashlib
import json
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


def _panel_secret() -> str:
    """
    Segredo usado para assinar a sessão do painel.
    Configure no EasyPanel:
    PANEL_SECRET_KEY=uma-string-grande-aleatoria
    """
    return (
        getattr(settings, "panel_secret_key", None)
        or getattr(settings, "autodoc_api_key", None)
        or getattr(settings, "api_key", None)
        or "CHANGE_ME_PANEL_SECRET"
    )


def _panel_password() -> str:
    """
    Senha do painel.
    Configure no EasyPanel:
    PANEL_PASSWORD=sua-senha-forte
    """
    return (
        getattr(settings, "panel_password", None)
        or getattr(settings, "autodoc_panel_password", None)
        or ""
    )


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
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AUTODOC CENTER — Painel Seguro</title>
  <style>
    :root{
      --bg:#0b1020;--panel:#121a2e;--line:#263653;--text:#eaf0ff;--muted:#91a0bd;
      --brand:#A9798B;--ok:#22c55e;--warn:#f59e0b;--danger:#ef4444;--violet:#a78bfa;
      --shadow:0 18px 50px rgba(0,0,0,.35);--radius:18px;
    }
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0%,rgba(169,121,139,.22),transparent 35%),radial-gradient(circle at 80% 10%,rgba(167,139,250,.16),transparent 30%),var(--bg);color:var(--text);font-family:Inter,Segoe UI,Roboto,Arial,sans-serif}
    .login{min-height:100vh;display:grid;place-items:center;padding:24px}.login-card{width:min(430px,100%);background:rgba(18,26,46,.88);border:1px solid var(--line);border-radius:24px;box-shadow:var(--shadow);padding:28px}.logo{width:52px;height:52px;border-radius:17px;background:linear-gradient(135deg,var(--brand),var(--violet));display:grid;place-items:center;font-weight:900;font-size:24px;margin-bottom:18px}h1{margin:0;font-size:28px;letter-spacing:-.04em}p{color:var(--muted);line-height:1.5}.field{margin:18px 0}label{display:block;color:var(--muted);font-size:13px;margin-bottom:7px}input,select{width:100%;background:#0b1222;border:1px solid var(--line);border-radius:13px;color:var(--text);padding:12px;outline:none}.btn{border:0;border-radius:13px;padding:11px 14px;font-weight:800;color:#fff;background:linear-gradient(135deg,var(--brand),#8F6475);cursor:pointer}.btn.secondary{background:#1f2d47;border:1px solid var(--line)}.btn.danger{background:rgba(239,68,68,.18);border:1px solid rgba(239,68,68,.35)}.btn.ok{background:rgba(34,197,94,.18);border:1px solid rgba(34,197,94,.35)}.btn:disabled{opacity:.5;cursor:not-allowed}
    .app{display:none;grid-template-columns:270px 1fr;min-height:100vh}aside{height:100vh;position:sticky;top:0;padding:22px 18px;background:rgba(12,18,34,.82);backdrop-filter:blur(14px);border-right:1px solid var(--line)}.brand{display:flex;gap:12px;align-items:center;margin-bottom:22px}.brand .logo{width:42px;height:42px;margin:0;font-size:18px}.brand b{display:block}.brand span{font-size:12px;color:var(--muted)}nav{display:flex;flex-direction:column;gap:8px}nav button{background:transparent;color:var(--muted);border:1px solid transparent;border-radius:13px;padding:12px;text-align:left;cursor:pointer}nav button.active,nav button:hover{background:rgba(255,255,255,.055);color:white;border-color:rgba(169,121,139,.28)}
    main{padding:26px}.topbar{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:22px}.topbar h2{margin:0;font-size:28px;letter-spacing:-.04em}.actions{display:flex;gap:10px;flex-wrap:wrap}.grid{display:grid;gap:16px}.cards{grid-template-columns:repeat(4,minmax(0,1fr));margin-bottom:18px}.card,.panel{background:rgba(18,26,46,.86);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow)}.card{padding:18px}.card .label{color:var(--muted);font-size:13px}.num{font-size:30px;font-weight:900;margin-top:8px}.panel{overflow:hidden;margin-bottom:18px}.head{display:flex;justify-content:space-between;align-items:center;gap:14px;padding:16px 18px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.03)}.head h3{margin:0;font-size:16px}.filters{display:flex;gap:10px;flex-wrap:wrap;padding:14px 18px;border-bottom:1px solid var(--line)}.filters input,.filters select{width:auto;min-width:190px}
    .table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:12px 14px;border-bottom:1px solid rgba(38,54,83,.65);white-space:nowrap;vertical-align:top}th{text-align:left;color:#b9c6df;background:rgba(255,255,255,.025)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}.pill{display:inline-flex;padding:5px 9px;border-radius:999px;font-size:12px;font-weight:800;border:1px solid var(--line);background:rgba(255,255,255,.045)}.pill.ok{color:#86efac;border-color:rgba(34,197,94,.35);background:rgba(34,197,94,.12)}.pill.warn{color:#fcd34d;border-color:rgba(245,158,11,.35);background:rgba(245,158,11,.12)}.pill.err{color:#fca5a5;border-color:rgba(239,68,68,.35);background:rgba(239,68,68,.12)}.pill.info{color:#93c5fd;border-color:rgba(59,130,246,.35);background:rgba(59,130,246,.12)}.pill.muted{color:#cbd5e1;border-color:rgba(148,163,184,.25);background:rgba(148,163,184,.08)}.hidden{display:none!important}.empty{padding:34px;text-align:center;color:var(--muted)}
    pre{background:#080d19;border:1px solid var(--line);color:#dbeafe;padding:14px;border-radius:14px;max-height:460px;overflow:auto;white-space:pre-wrap;word-break:break-word}.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.58);display:none;align-items:center;justify-content:center;z-index:50;padding:20px}.modal{width:min(960px,96vw);max-height:88vh;overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:22px;box-shadow:var(--shadow)}.modal .body{padding:18px}.toast{position:fixed;right:20px;bottom:20px;background:#101827;border:1px solid var(--line);border-radius:16px;padding:14px 16px;box-shadow:var(--shadow);z-index:60;max-width:440px}
    @media(max-width:1100px){.app{grid-template-columns:1fr}aside{height:auto;position:relative}.cards{grid-template-columns:repeat(2,1fr)}}@media(max-width:620px){main{padding:16px}.cards{grid-template-columns:1fr}.topbar{align-items:flex-start;flex-direction:column}}
  </style>
</head>
<body>
  <section id="login" class="login">
    <div class="login-card">
      <div class="logo">A</div>
      <h1>AUTODOC CENTER</h1>
      <p>Painel seguro do cliente. A API Key fica no servidor e nunca aparece no navegador.</p>
      <div class="field"><label>Senha do painel</label><input id="password" type="password" placeholder="Digite a senha" onkeydown="if(event.key==='Enter') login()"></div>
      <button class="btn" style="width:100%" onclick="login()">Entrar</button>
      <p id="loginError" style="color:#fca5a5"></p>
    </div>
  </section>

  <section id="app" class="app">
    <aside>
      <div class="brand"><div class="logo">A</div><div><b>AUTODOC CENTER</b><span>Painel Cliente</span></div></div>
      <nav>
        <button class="active" data-view="dashboard">📊 Dashboard</button>
        <button data-view="files">📁 Arquivos</button>
        <button data-view="emails">✉️ E-mails</button>
        <button data-view="robot">🤖 Fila do Robô</button>
        <button data-view="sharepoint">🟦 SharePoint</button>
        <button data-view="errors">⚠️ Erros</button>
        <button data-view="settings">⚙️ Status</button>
      </nav>
      <div style="margin-top:18px"><button class="btn danger" style="width:100%" onclick="logout()">Sair</button></div>
    </aside>
    <main>
      <div class="topbar"><div><h2 id="title">Dashboard</h2><p id="subtitle">Visão geral da operação.</p></div><div class="actions"><button class="btn secondary" onclick="refresh()">Atualizar</button></div></div>

      <section id="view-dashboard" class="view">
        <div class="grid cards">
          <div class="card"><div class="label">Arquivos</div><div class="num" id="kFiles">--</div></div>
          <div class="card"><div class="label">Aguardando download</div><div class="num" id="kPending">--</div></div>
          <div class="card"><div class="label">Salvos</div><div class="num" id="kSaved">--</div></div>
          <div class="card"><div class="label">Erros</div><div class="num" id="kErrors">--</div></div>
        </div>
        <div class="panel"><div class="head"><h3>Health</h3><button class="btn secondary" onclick="loadHealth()">Testar</button></div><div class="table-wrap"><table><thead><tr><th>Serviço</th><th>Status</th><th>Detalhes</th></tr></thead><tbody id="healthRows"></tbody></table></div></div>
        <div class="panel"><div class="head"><h3>Arquivos recentes</h3></div><div class="table-wrap"><table><thead><tr><th>Arquivo</th><th>Projeto</th><th>Disciplina</th><th>Status</th><th>Score</th><th>Destino</th></tr></thead><tbody id="recentFiles"></tbody></table></div></div>
      </section>

      <section id="view-files" class="view hidden">
        <div class="panel"><div class="head"><h3>Arquivos</h3><button class="btn secondary" onclick="loadFiles()">Atualizar</button></div><div class="filters"><input id="fileSearch" placeholder="Buscar..." oninput="renderFiles()"><select id="fileStatus" onchange="renderFiles()"><option value="">Todos status</option></select></div><div class="table-wrap"><table><thead><tr><th>Arquivo</th><th>Projeto</th><th>Disciplina</th><th>AutoDoc</th><th>SharePoint</th><th>Status</th><th>Score</th><th>Ações</th></tr></thead><tbody id="filesRows"></tbody></table></div></div>
      </section>

      <section id="view-emails" class="view hidden">
        <div class="panel"><div class="head"><h3>E-mails</h3><button class="btn secondary" onclick="loadEmails()">Atualizar</button></div><div class="table-wrap"><table><thead><tr><th>Data</th><th>Assunto</th><th>Remetente</th><th>Status</th><th>Tipo</th><th>Ações</th></tr></thead><tbody id="emailsRows"></tbody></table></div></div>
      </section>

      <section id="view-robot" class="view hidden">
        <div class="panel"><div class="head"><h3>Fila do Robô</h3><div class="actions"><button class="btn secondary" onclick="loadJobs()">Atualizar</button><button class="btn" onclick="claimNext()">Pegar próximo</button></div></div><div class="table-wrap"><table><thead><tr><th>Job</th><th>Arquivo</th><th>Status</th><th>Attempts</th><th>Worker</th><th>Próxima</th><th>Erro</th><th>Ações</th></tr></thead><tbody id="jobsRows"></tbody></table></div></div>
      </section>

      <section id="view-sharepoint" class="view hidden">
        <div class="panel"><div class="head"><h3>SharePoint</h3><button class="btn secondary" onclick="loadSites()">Atualizar</button></div><div class="table-wrap"><table><thead><tr><th>Projeto</th><th>URL</th><th>Library</th><th>Ativo</th><th>HML</th><th>Map</th><th>Prod</th><th>Ações</th></tr></thead><tbody id="sitesRows"></tbody></table></div></div>
      </section>

      <section id="view-errors" class="view hidden">
        <div class="panel"><div class="head"><h3>Erros</h3><button class="btn secondary" onclick="loadErrors()">Atualizar</button></div><div class="table-wrap"><table><thead><tr><th>Tipo</th><th>Mensagem</th><th>Arquivo</th><th>Status</th><th>Data</th><th>Ações</th></tr></thead><tbody id="errorsRows"></tbody></table></div></div>
      </section>

      <section id="view-settings" class="view hidden">
        <div class="panel"><div class="head"><h3>Status / Configuração</h3><button class="btn secondary" onclick="loadSettings()">Atualizar</button></div><div style="padding:18px"><pre id="settingsJson">{}</pre></div></div>
      </section>
    </main>
  </section>

  <div class="modal-backdrop" id="modal"><div class="modal"><div class="head"><h3 id="modalTitle">Detalhes</h3><button class="btn secondary" onclick="closeModal()">Fechar</button></div><div class="body"><pre id="modalBody"></pre></div></div></div>

  <script>
    const state={files:[],emails:[],jobs:[],sites:[],errors:[]};
    const meta={dashboard:['Dashboard','Visão geral da operação.'],files:['Arquivos','Controle de arquivos e destinos.'],emails:['E-mails','E-mails processados.'],robot:['Fila do Robô','Jobs de download/upload.'],sharepoint:['SharePoint','Sites e HML.'],errors:['Erros','Falhas e auditoria.'],settings:['Status','Ambiente e integrações.']};

    async function call(path,opt={}){const r=await fetch(path,{credentials:'include',headers:{'Content-Type':'application/json'},...opt});const t=await r.text();let d;try{d=t?JSON.parse(t):{}}catch{d={raw:t}};if(!r.ok)throw new Error(d.detail||JSON.stringify(d));return d}
    async function login(){try{await call('/panel/login',{method:'POST',body:JSON.stringify({password:document.getElementById('password').value})});document.getElementById('login').style.display='none';document.getElementById('app').style.display='grid';refresh()}catch(e){document.getElementById('loginError').textContent=e.message}}
    async function logout(){await call('/panel/logout',{method:'POST'}).catch(()=>{});location.reload()}
    async function check(){try{await call('/panel/api/summary');document.getElementById('login').style.display='none';document.getElementById('app').style.display='grid';refresh()}catch{}}
    function nav(v){document.querySelectorAll('.view').forEach(x=>x.classList.add('hidden'));document.getElementById('view-'+v).classList.remove('hidden');document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.view===v));document.getElementById('title').textContent=meta[v][0];document.getElementById('subtitle').textContent=meta[v][1];if(v==='files')loadFiles();if(v==='emails')loadEmails();if(v==='robot')loadJobs();if(v==='sharepoint')loadSites();if(v==='errors')loadErrors();if(v==='settings')loadSettings()}
    document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>nav(b.dataset.view));
    async function refresh(){await Promise.allSettled([loadSummary(),loadHealth(),loadFiles(),loadSites()])}
    async function loadSummary(){const s=await call('/panel/api/summary');document.getElementById('kFiles').textContent=s.files_total??0;document.getElementById('kPending').textContent=s.files_waiting_download??0;document.getElementById('kSaved').textContent=s.files_saved??0;document.getElementById('kErrors').textContent=s.errors_total??0}
    async function loadHealth(){const h=await call('/panel/api/health');document.getElementById('healthRows').innerHTML=Object.entries(h).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${pill(v.ok!==false?'OK':'ERRO',v.ok!==false?'ok':'err')}</td><td><button class="btn secondary" onclick='show("${k}",${attr(v)})'>Ver</button></td></tr>`).join('')}
    async function loadFiles(){const d=await call('/panel/api/files?limit=500');state.files=d.files||[];fillStatus();renderFiles();renderRecent()}
    function fillStatus(){const s=document.getElementById('fileStatus'),cur=s.value;const list=[...new Set(state.files.map(f=>f.status).filter(Boolean))].sort();s.innerHTML='<option value="">Todos status</option>'+list.map(x=>`<option>${esc(x)}</option>`).join('');s.value=cur}
    function renderFiles(){const q=(document.getElementById('fileSearch')?.value||'').toLowerCase(),st=(document.getElementById('fileStatus')?.value||'');let arr=state.files.filter(f=>(!st||f.status===st)&&(!q||[f.file_name,f.project_detected,f.discipline_detected,f.autodoc_path,f.sharepoint_suggested_path,f.status].join(' ').toLowerCase().includes(q)));document.getElementById('filesRows').innerHTML=arr.map(f=>`<tr><td><b>${esc(f.file_name)}</b><br><span class=mono>${short(f.id)}</span></td><td>${esc(f.project_detected)}</td><td>${esc(f.discipline_detected)}</td><td>${esc(f.autodoc_path)}</td><td>${esc(f.sharepoint_final_path||f.sharepoint_suggested_path)}</td><td>${status(f.status)}</td><td>${score(f.confidence_score)}</td><td><div class=actions><button class="btn secondary" onclick='show("Arquivo",${attr(f)})'>Ver</button><button class="btn ok" onclick="act('/panel/api/files/${f.id}/approve')">Aprovar</button><button class="btn" onclick="act('/panel/api/files/${f.id}/queue')">Fila</button><button class="btn danger" onclick="act('/panel/api/files/${f.id}/ignore')">Ignorar</button></div></td></tr>`).join('')||empty(8,'Nenhum arquivo.')}
    function renderRecent(){document.getElementById('recentFiles').innerHTML=state.files.slice(0,8).map(f=>`<tr><td>${esc(f.file_name)}</td><td>${esc(f.project_detected)}</td><td>${esc(f.discipline_detected)}</td><td>${status(f.status)}</td><td>${score(f.confidence_score)}</td><td>${esc(f.sharepoint_suggested_path)}</td></tr>`).join('')||empty(6,'Nenhum arquivo.')}
    async function loadEmails(){const d=await call('/panel/api/emails?limit=300');state.emails=d.emails||[];document.getElementById('emailsRows').innerHTML=state.emails.map(e=>`<tr><td>${date(e.created_at||e.received_at)}</td><td>${esc(e.subject)}</td><td>${esc(e.sender)}</td><td>${status(e.status)}</td><td>${esc(e.email_type)}</td><td><button class="btn secondary" onclick='show("E-mail",${attr(e)})'>Ver</button></td></tr>`).join('')||empty(6,'Nenhum e-mail.')}
    async function loadJobs(){const d=await call('/panel/api/robot/jobs?limit=300');state.jobs=d.jobs||[];document.getElementById('jobsRows').innerHTML=state.jobs.map(j=>`<tr><td><span class=mono>${short(j.id)}</span></td><td><span class=mono>${short(j.file_id)}</span></td><td>${status(j.status)}</td><td>${j.attempts||0}/${j.max_attempts||3}</td><td>${esc(j.locked_by)}</td><td>${date(j.next_attempt_at)}</td><td>${esc(shortText(j.last_error))}</td><td><button class="btn secondary" onclick='show("Job",${attr(j)})'>Ver</button></td></tr>`).join('')||empty(8,'Nenhum job.')}
    async function claimNext(){const d=await call('/panel/api/robot/jobs/next',{method:'POST'});show('Próximo job',d);loadJobs()}
    async function loadSites(){const d=await call('/panel/api/sites');state.sites=d.sites||[];document.getElementById('sitesRows').innerHTML=state.sites.map(s=>`<tr><td><b>${esc(s.project_name||s.project_normalized)}</b><br><span class=mono>${short(s.id)}</span></td><td>${esc(s.site_url)}</td><td>${esc(s.library_name)}</td><td>${bool(s.active)}</td><td>${bool(s.hml_ready)}</td><td>${bool(s.folder_map_ready)}</td><td>${bool(s.prod_ready)}</td><td><button class="btn secondary" onclick='show("Site",${attr(s)})'>Ver</button></td></tr>`).join('')||empty(8,'Nenhum site.')}
    async function loadErrors(){const d=await call('/panel/api/errors?limit=300');state.errors=d.errors||[];document.getElementById('errorsRows').innerHTML=state.errors.map(e=>`<tr><td>${esc(e.error_type)}</td><td>${esc(e.message)}</td><td><span class=mono>${short(e.file_id)}</span></td><td>${status(e.status)}</td><td>${date(e.created_at)}</td><td><button class="btn secondary" onclick='show("Erro",${attr(e)})'>Ver</button></td></tr>`).join('')||empty(6,'Nenhum erro.')}
    async function loadSettings(){const d=await call('/panel/api/settings');document.getElementById('settingsJson').textContent=JSON.stringify(d,null,2)}
    async function act(path){try{const d=await call(path,{method:'POST'});show('Resultado',d);loadFiles()}catch(e){toast(e.message)}}
    function status(s){s=s||'—';let u=String(s).toUpperCase(),c='muted';if(u.includes('DONE')||u.includes('OK')||u.includes('SALVO'))c='ok';else if(u.includes('PENDING')||u.includes('AGUARDANDO')||u.includes('RUNNING'))c='warn';else if(u.includes('ERRO')||u.includes('ERROR')||u.includes('FAIL'))c='err';else if(u.includes('PRONTO')||u.includes('RECEBIDO'))c='info';return pill(s,c)}
    function score(v){let n=Number(v||0),c=n>=90?'ok':n>=60?'warn':'err';return pill(isFinite(n)?n:0,c)}function bool(v){return pill(v?'Sim':'Não',v?'ok':'muted')}function pill(t,c){return `<span class="pill ${c}">${esc(t)}</span>`}function empty(c,m){return `<tr><td colspan="${c}" class=empty>${esc(m)}</td></tr>`}function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]))}function short(id){id=String(id||'');return id.length>12?id.slice(0,8)+'…'+id.slice(-4):id}function shortText(v){let s=typeof v==='string'?v:JSON.stringify(v||'');return s.length>100?s.slice(0,100)+'…':s}function date(v){if(!v)return'';try{return new Date(v).toLocaleString('pt-BR')}catch{return v}}function attr(o){return encodeURIComponent(JSON.stringify(o??{},null,2))}
    function show(t,v){let text=typeof v==='string'?decodeURIComponent(v):JSON.stringify(v,null,2);document.getElementById('modalTitle').textContent=t;document.getElementById('modalBody').textContent=text;document.getElementById('modal').style.display='flex'}function closeModal(){document.getElementById('modal').style.display='none'}function toast(m){const t=document.createElement('div');t.className='toast';t.textContent=m;document.body.appendChild(t);setTimeout(()=>t.remove(),4200)}
    addEventListener('keydown',e=>{if(e.key==='Escape')closeModal()});check();
  </script>
</body>
</html>"""


@app.get("/panel", response_class=HTMLResponse)
def panel_page():
    return HTMLResponse(_panel_html())


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
        secure=True,
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
