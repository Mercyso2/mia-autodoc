from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
from pathlib import Path
import shutil

from core.config import settings
from core.database import *
from core import statuses as st
from services.email_parser import parse_email
from services.sharepoint_mapper import discover_site_from_url, sync_site_folders, resolve_folder, target_path, bootstrap_hml_structure, generate_folder_map, prepare_site_hml
from services.scoring_service import score_file
from services.file_service import enrich_file_destination, upload_hml
from services.sharepoint_service import sharepoint
from robot.login_manager import check_logged_in, open_login_browser
from robot.downloader import download as autodoc_download

app = FastAPI(title='Autodoc Center V4 Multi-site SharePoint')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

class EmailIngest(BaseModel):
    source: str='n8n'
    environment: str='HML'
    message_id: str
    folder: str='Autodoc'
    subject: str=''
    sender: str=''
    received_at: Optional[str]=None
    html: str=''
    text: str=''
    attachments: List[Dict[str,Any]]=[]

class SiteDiscover(BaseModel):
    project_name: str
    site_url: str
    aliases: str=''
    library_name: str='Documentos'

class CorrectPath(BaseModel):
    sharepoint_site_ref: Optional[str]=None
    sharepoint_drive_id: Optional[str]=None
    sharepoint_suggested_path: str

@app.get('/health')
def health():
    return {'status':'ok','service':settings.app_name,'environment':settings.app_env,'production_enabled':settings.is_prod_enabled,'multi_site_sharepoint': True}

@app.get('/db/health')
def db_health():
    try: return health_check()
    except Exception as e: raise HTTPException(500, str(e))

@app.get('/sharepoint/health')
def sp_health():
    try:
        sharepoint.token()
        return {'ok': True, 'hostname': settings.sharepoint_hostname, 'mode': 'multi-site'}
    except Exception as e: raise HTTPException(500, str(e))

@app.post('/sharepoint/sites/discover')
def sp_discover(payload: SiteDiscover):
    try: return discover_site_from_url(payload.project_name, payload.site_url, payload.aliases, payload.library_name)
    except Exception as e: raise HTTPException(500, str(e))

@app.get('/sharepoint/sites')
def sp_sites():
    return list_rows('autodoc_sharepoint_sites', limit=1000)

@app.post('/sharepoint/sites/{site_row_id}/sync-folders')
def sp_sync_folders(site_row_id: str, base_folder: str=''):
    try: return {'ok': True, 'folders': sync_site_folders(site_row_id, base_folder)}
    except Exception as e: raise HTTPException(500, str(e))


@app.post('/sharepoint/sites/{site_row_id}/bootstrap-hml')
def sp_bootstrap_hml(site_row_id: str, base_folder: str=''):
    """Cria automaticamente _AUTODOC_HOMOLOGACAO e subpastas no SharePoint."""
    try: return bootstrap_hml_structure(site_row_id, base_folder)
    except Exception as e: raise HTTPException(500, str(e))

@app.post('/sharepoint/sites/{site_row_id}/generate-folder-map')
def sp_generate_folder_map(site_row_id: str, file_extensions: str='pdf,dwg'):
    """Gera automaticamente autodoc_folder_map a partir das pastas sincronizadas."""
    try:
        exts = [x.strip() for x in file_extensions.split(',') if x.strip()]
        return generate_folder_map(site_row_id, exts)
    except Exception as e: raise HTTPException(500, str(e))

@app.post('/sharepoint/sites/{site_row_id}/prepare-hml')
def sp_prepare_hml(site_row_id: str, base_folder: str='', file_extensions: str='pdf,dwg'):
    """Executa tudo: sincroniza pastas, cria estrutura HML e gera mapa de destino."""
    try:
        exts = [x.strip() for x in file_extensions.split(',') if x.strip()]
        return prepare_site_hml(site_row_id, base_folder, exts)
    except Exception as e: raise HTTPException(500, str(e))

@app.get('/sharepoint/sites/{site_row_id}/children')
def sp_children(site_row_id: str, folder_path: str=''):
    site = get_row('autodoc_sharepoint_sites', site_row_id)
    if not site: raise HTTPException(404, 'Site não encontrado')
    try: return sharepoint.children(site['drive_id'], folder_path)
    except Exception as e: raise HTTPException(500, str(e))

@app.post('/emails/ingest')
def ingest(payload: EmailIngest):
    try:
        email = upsert_row('autodoc_emails', {
            'message_id': payload.message_id, 'source': payload.source, 'environment': payload.environment,
            'folder': payload.folder, 'subject': payload.subject, 'sender': payload.sender,
            'received_at': payload.received_at, 'html': payload.html, 'text': payload.text,
            'attachments': payload.attachments, 'status': st.EMAIL_RECEBIDO
        }, on_conflict='message_id')
        insert_history(email_id=email.get('id'), action=st.ACTION_EMAIL_RECEBIDO, environment=payload.environment, status='OK')
        return {'ok': True, 'email_id': email.get('id'), 'status': st.EMAIL_RECEBIDO}
    except Exception as e:
        insert_error('EMAIL_INGEST_ERROR', str(e), payload=payload.model_dump())
        raise HTTPException(500, str(e))

@app.post('/emails/{email_id}/parse')
def parse_saved_email(email_id: str, force: bool=False):
    email = get_row('autodoc_emails', email_id)
    if not email: raise HTTPException(404, 'E-mail não encontrado')
    try:
        parsed = parse_email(email.get('subject',''), email.get('html',''), email.get('text',''), email.get('attachments') or [])
        created=[]
        for fd in parsed['files']:
            mapping = resolve_folder(fd.get('project_detected','') or parsed.get('project',''), fd.get('discipline_detected',''), fd.get('autodoc_path',''), fd.get('extension',''))
            scored = score_file(fd, mapping)
            row = {
                'email_id': email_id, 'file_name': fd['file_name'], 'extension': fd.get('extension'),
                'project_detected': fd.get('project_detected') or parsed.get('project'),
                'discipline_detected': fd.get('discipline_detected'), 'autodoc_path': fd.get('autodoc_path'),
                'needs_autodoc_robot': not fd.get('has_attachment', False), 'parser_payload': fd,
                **scored
            }
            if mapping:
                site = mapping['_site']; row.update({'sharepoint_site_ref': site['id'], 'sharepoint_drive_id': site['drive_id'], 'sharepoint_suggested_path': target_path(mapping, settings.app_env), 'sharepoint_folder_map_ref': mapping.get('id')})
            f = upsert_row('autodoc_files', row, on_conflict='email_id,file_name,autodoc_path_safe')
            created.append(f)
            insert_history(email_id=email_id, file_id=f.get('id'), action=st.ACTION_ARQUIVO_DETECTADO, file_name=f.get('file_name'), to_path=f.get('sharepoint_suggested_path'), environment=settings.app_env, status=f.get('status'))
        update_row('autodoc_emails', email_id, {'status': st.EMAIL_PROCESSADO, 'email_type': parsed['email_type']})
        return {'ok': True, 'email_id': email_id, 'email_type': parsed['email_type'], 'files_created': len(created), 'files': created}
    except Exception as e:
        insert_error('PARSER_ERROR', str(e), email_id=email_id)
        update_row('autodoc_emails', email_id, {'status': st.EMAIL_ERRO})
        raise HTTPException(500, str(e))

@app.post('/emails/parse-latest')
def parse_latest():
    emails=list_rows('autodoc_emails',limit=1,filters={'status':st.EMAIL_RECEBIDO})
    if not emails: return {'ok': False, 'message':'Nenhum e-mail recebido pendente'}
    return parse_saved_email(emails[0]['id'])

@app.get('/files')
def files(status: str='', limit: int=100):
    return list_rows('autodoc_files', limit=limit, filters={'status': status} if status else None)

@app.get('/files/ready')
def files_ready(limit: int=100): return list_rows('autodoc_files', limit=limit, filters={'status': st.FILE_PRONTO})

@app.get('/files/{file_id}')
def file_get(file_id: str):
    f=get_row('autodoc_files',file_id)
    if not f: raise HTTPException(404,'Arquivo não encontrado')
    return f

@app.post('/files/{file_id}/enrich-destination')
def file_enrich(file_id: str):
    try: return enrich_file_destination(file_id)
    except Exception as e: raise HTTPException(500,str(e))

@app.post('/files/{file_id}/approve')
def file_approve(file_id: str):
    f=update_row('autodoc_files',file_id,{'status':st.FILE_PRONTO,'approved':True})
    insert_history(email_id=f.get('email_id'),file_id=file_id,action=st.ACTION_APROVADO,file_name=f.get('file_name'),environment=settings.app_env,status='OK')
    return f

@app.post('/files/{file_id}/correct-path')
def file_correct(file_id: str, payload: CorrectPath):
    data={'sharepoint_suggested_path':payload.sharepoint_suggested_path,'status':st.FILE_AGUARDANDO_APROVACAO}
    if payload.sharepoint_site_ref: data['sharepoint_site_ref']=payload.sharepoint_site_ref
    if payload.sharepoint_drive_id: data['sharepoint_drive_id']=payload.sharepoint_drive_id
    f=update_row('autodoc_files',file_id,data)
    insert_history(email_id=f.get('email_id'),file_id=file_id,action=st.ACTION_CORRIGIDO,file_name=f.get('file_name'),to_path=payload.sharepoint_suggested_path,environment=settings.app_env,status='OK')
    return f

@app.post('/files/{file_id}/ignore')
def file_ignore(file_id: str):
    f=update_row('autodoc_files',file_id,{'status':st.FILE_IGNORADO})
    insert_history(email_id=f.get('email_id'),file_id=file_id,action=st.ACTION_IGNORADO,file_name=f.get('file_name'),environment=settings.app_env,status='OK')
    return f

@app.post('/files/{file_id}/upload-local')
def upload_local(file_id: str, file: UploadFile=File(...)):
    f=get_row('autodoc_files', file_id)
    if not f: raise HTTPException(404,'Arquivo não encontrado')
    dest=Path('storage/downloads') / f['file_name']
    with dest.open('wb') as out: shutil.copyfileobj(file.file, out)
    return update_row('autodoc_files',file_id,{'local_path':str(dest),'status':st.FILE_BAIXADO})

@app.post('/files/{file_id}/queue-robot')
def queue_robot(file_id: str):
    f=get_row('autodoc_files', file_id)
    if not f: raise HTTPException(404,'Arquivo não encontrado')
    job=insert_row('autodoc_robot_queue',{'file_id':file_id,'status':'PENDING','payload':f})
    update_row('autodoc_files', file_id, {'status': st.FILE_AGUARDANDO_DOWNLOAD, 'needs_autodoc_robot': True})
    return {'ok': True, 'job': job}

@app.post('/files/{file_id}/upload-hml')
def file_upload_hml(file_id: str):
    try: return upload_hml(file_id)
    except Exception as e:
        insert_error('SHAREPOINT_UPLOAD_ERROR', str(e), file_id=file_id)
        update_row('autodoc_files', file_id, {'status': st.FILE_ERRO, 'error_message': str(e)})
        raise HTTPException(500, str(e))

@app.post('/autodoc/login/check')
def autodoc_login_check(): return check_logged_in()

@app.post('/autodoc/login/open')
def autodoc_login_open(): return open_login_browser()

@app.post('/autodoc/download')
def autodoc_download_endpoint(payload: Dict[str,Any]):
    file_id=payload.get('file_id')
    f=get_row('autodoc_files', file_id) if file_id else None
    project=payload.get('project') or (f or {}).get('project_detected','')
    path=payload.get('autodoc_path') or (f or {}).get('autodoc_path','')
    name=payload.get('file_name') or (f or {}).get('file_name','')
    try:
        res=autodoc_download(project,path,name)
        if f: update_row('autodoc_files',file_id,{'local_path':res['local_path'],'status':st.FILE_BAIXADO})
        return res
    except Exception as e:
        if f: insert_error('AUTODOC_DOWNLOAD_ERROR', str(e), file_id=file_id)
        raise HTTPException(500,str(e))
