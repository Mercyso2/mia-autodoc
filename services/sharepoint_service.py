from __future__ import annotations
from typing import Any, Dict, List, Optional
from pathlib import Path
import requests
from core.config import settings
from services.normalizer import safe_path_part

GRAPH='https://graph.microsoft.com/v1.0'
TOKEN='https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token'

class SharePointService:
    def __init__(self):
        self._token: Optional[str] = None

    def token(self) -> str:
        if self._token: return self._token
        if not (settings.microsoft_tenant_id and settings.microsoft_client_id and settings.microsoft_client_secret):
            raise RuntimeError('Credenciais Microsoft incompletas no .env')
        r = requests.post(TOKEN.format(tenant=settings.microsoft_tenant_id), data={
            'client_id': settings.microsoft_client_id,
            'client_secret': settings.microsoft_client_secret,
            'scope': 'https://graph.microsoft.com/.default',
            'grant_type': 'client_credentials'
        }, timeout=30)
        if r.status_code >= 400: raise RuntimeError(f'Erro token Graph: {r.status_code} {r.text}')
        self._token = r.json()['access_token']
        return self._token

    def headers(self): return {'Authorization': f'Bearer {self.token()}'}

    def get(self, url):
        r=requests.get(url, headers=self.headers(), timeout=60)
        if r.status_code>=400: raise RuntimeError(f'Graph GET {r.status_code}: {r.text}')
        return r.json()

    def post(self, url, body=None):
        r=requests.post(url, headers={**self.headers(),'Content-Type':'application/json'}, json=body or {}, timeout=60)
        if r.status_code>=400: raise RuntimeError(f'Graph POST {r.status_code}: {r.text}')
        return r.json() if r.text else {}

    def put_bytes(self, url, data: bytes):
        r=requests.put(url, headers={**self.headers(),'Content-Type':'application/octet-stream'}, data=data, timeout=180)
        if r.status_code>=400: raise RuntimeError(f'Graph PUT {r.status_code}: {r.text}')
        return r.json()

    def patch(self, url, body=None):
        r=requests.patch(url, headers={**self.headers(),'Content-Type':'application/json'}, json=body or {}, timeout=60)
        if r.status_code>=400: raise RuntimeError(f'Graph PATCH {r.status_code}: {r.text}')
        return r.json() if r.text else {}

    def site_by_url(self, hostname: str, site_path: str) -> Dict[str,Any]:
        site_path = site_path if site_path.startswith('/') else '/' + site_path
        return self.get(f'{GRAPH}/sites/{hostname}:{site_path}')

    def drives(self, site_id: str) -> List[Dict[str,Any]]:
        return self.get(f'{GRAPH}/sites/{site_id}/drives').get('value', [])

    def find_drive(self, site_id: str, library_name: str | None=None) -> Dict[str,Any]:
        library_name = library_name or settings.sharepoint_default_library_name
        drives = self.drives(site_id)
        for d in drives:
            if d.get('name','').lower() == library_name.lower(): return d
        if drives: return drives[0]
        raise RuntimeError(f'Nenhum drive encontrado para site {site_id}')

    def children(self, drive_id: str, folder_path: str='') -> List[Dict[str,Any]]:
        path = folder_path.strip('/ ')
        if path:
            url = f'{GRAPH}/drives/{drive_id}/root:/{path}:/children'
        else:
            url = f'{GRAPH}/drives/{drive_id}/root/children'
        return self.get(url).get('value', [])

    def item_by_path(self, drive_id: str, path: str) -> Optional[Dict[str,Any]]:
        path = path.strip('/ ')
        try:
            return self.get(f'{GRAPH}/drives/{drive_id}/root:/{path}')
        except RuntimeError as e:
            if '404' in str(e): return None
            raise

    def ensure_folder(self, drive_id: str, folder_path: str) -> Dict[str,Any]:
        current = ''
        last = None
        for part in [safe_path_part(p) for p in folder_path.strip('/').split('/') if p.strip()]:
            parent = current
            current = f'{current}/{part}'.strip('/')
            existing = self.item_by_path(drive_id, current)
            if existing:
                last = existing; continue
            body = {'name': part, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'}
            if parent:
                last = self.post(f'{GRAPH}/drives/{drive_id}/root:/{parent}:/children', body)
            else:
                last = self.post(f'{GRAPH}/drives/{drive_id}/root/children', body)
        return last or self.get(f'{GRAPH}/drives/{drive_id}/root')

    def upload_small(self, drive_id: str, folder_path: str, file_name: str, local_path: str) -> Dict[str,Any]:
        folder_path = folder_path.strip('/ ')
        self.ensure_folder(drive_id, folder_path)
        file_name = safe_path_part(file_name)
        full_path = f'{folder_path}/{file_name}'.strip('/')
        data = Path(local_path).read_bytes()
        return self.put_bytes(f'{GRAPH}/drives/{drive_id}/root:/{full_path}:/content', data)

    def move_item(self, drive_id: str, source_path: str, target_folder_path: str, new_name: str) -> Dict[str,Any]:
        item = self.item_by_path(drive_id, source_path)
        if not item: raise RuntimeError(f'Arquivo origem não encontrado: {source_path}')
        parent = self.ensure_folder(drive_id, target_folder_path)
        body = {'parentReference': {'id': parent['id']}, 'name': safe_path_part(new_name)}
        return self.patch(f'{GRAPH}/drives/{drive_id}/items/{item["id"]}', body)

sharepoint = SharePointService()
