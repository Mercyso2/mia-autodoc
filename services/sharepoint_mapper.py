from __future__ import annotations
from typing import Any, Dict, List, Optional
from rapidfuzz import fuzz
from core.database import list_rows, get_row, insert_row, update_row, upsert_row
from core.config import settings
from services.normalizer import normalize_text, safe_path_part
from services.sharepoint_service import sharepoint

MIN_MATCH = 82
CONTROL_FOLDERS = [settings.sharepoint_obsolete_folder, settings.sharepoint_pending_folder, settings.sharepoint_error_folder]


def _json_array(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [x.strip() for x in value.split(',') if x.strip()]
    return []


def _is_root_folder(folder_path: str) -> bool:
    return bool(folder_path) and '/' not in folder_path.strip('/ ')


def _is_business_folder(folder_name: str) -> bool:
    name = (folder_name or '').strip()
    if not name:
        return False
    if name.startswith('_'):
        return False
    if normalize_text(name) in {normalize_text(settings.sharepoint_hml_root), normalize_text(settings.sharepoint_obsolete_folder), normalize_text(settings.sharepoint_pending_folder), normalize_text(settings.sharepoint_error_folder)}:
        return False
    return True


def discover_site_from_url(project_name: str, site_url: str, aliases: str='', library_name: str | None=None) -> Dict[str,Any]:
    # Ex: https://netorg11988711.sharepoint.com/sites/SAE-GUANS
    import urllib.parse
    u = urllib.parse.urlparse(site_url)
    hostname = u.netloc
    site_path = u.path.rstrip('/')
    site = sharepoint.site_by_url(hostname, site_path)
    drive = sharepoint.find_drive(site['id'], library_name or settings.sharepoint_default_library_name)
    payload = {
        'project_name': project_name,
        'project_normalized': normalize_text(project_name),
        'aliases': _json_array(aliases),
        'hostname': hostname,
        'site_path': site_path,
        'site_url': site_url,
        'site_id': site['id'],
        'drive_id': drive['id'],
        'library_name': drive.get('name') or library_name or settings.sharepoint_default_library_name,
        'active': True,
        'raw': {'site': site, 'drive': drive}
    }
    return upsert_row('autodoc_sharepoint_sites', payload, on_conflict='site_id')


def resolve_project_site(project_detected: str) -> Optional[Dict[str,Any]]:
    q = normalize_text(project_detected)
    if not q: return None
    sites = list_rows('autodoc_sharepoint_sites', limit=1000, filters={'active': True})
    best, best_score = None, 0
    for s in sites:
        candidates = [s.get('project_name',''), s.get('project_normalized','')] + (s.get('aliases') or [])
        for c in candidates:
            sc = max(fuzz.token_set_ratio(q, normalize_text(c)), fuzz.partial_ratio(q, normalize_text(c)))
            if sc > best_score:
                best, best_score = s, sc
    if best and best_score >= MIN_MATCH:
        best['_match_score'] = best_score
        return best
    return None


def resolve_folder(project_detected: str, discipline_detected: str='', autodoc_path: str='', extension: str='') -> Optional[Dict[str,Any]]:
    site = resolve_project_site(project_detected)
    if not site: return None
    maps = list_rows('autodoc_folder_map', limit=2000, filters={'sharepoint_site_ref': site['id'], 'active': True})
    dn = normalize_text(discipline_detected)
    pathn = normalize_text(autodoc_path)
    best, score = None, 0
    for m in maps:
        candidates = [m.get('discipline_autodoc',''), m.get('discipline_normalized',''), m.get('sharepoint_folder_path',''), m.get('sharepoint_path','')] + (m.get('aliases') or [])
        local = 0
        for c in candidates:
            local = max(local, fuzz.token_set_ratio(dn, normalize_text(c)), fuzz.partial_ratio(pathn, normalize_text(c)))
        exts = m.get('file_extensions') or []
        if isinstance(exts, str): exts = [x.strip().lower() for x in exts.split(',') if x.strip()]
        if extension and exts and extension.lower().lstrip('.') in [x.lower().lstrip('.') for x in exts]: local += 5
        if local > score:
            best, score = m, local
    if not best or score < 65:
        # Fallback seguro: cria destino em HML/NAO_IDENTIFICADO ou HML/<disciplina>, mas score ficará baixo.
        folder = safe_path_part(discipline_detected or 'NAO_IDENTIFICADO')
        best = {
            'sharepoint_site_ref': site['id'],
            'sharepoint_folder_path': folder,
            'discipline_normalized': normalize_text(discipline_detected),
            'file_extensions': [],
            '_fallback': True,
        }
    best['_site'] = site
    best['_match_score'] = score
    return best


def target_path(mapping: Dict[str,Any], environment: str='HML') -> str:
    root = settings.sharepoint_hml_root if environment.upper() == 'HML' else (settings.sharepoint_prod_root or '')
    folder = mapping.get('sharepoint_folder_path') or mapping.get('sharepoint_path') or 'NAO_IDENTIFICADO'
    # Se a pasta já vier com o root HML, não duplica.
    folder = str(folder).strip('/ ')
    if root and normalize_text(folder).startswith(normalize_text(root)):
        return folder
    return '/'.join([p.strip('/ ') for p in [root, folder] if p and p.strip('/ ')])


def sync_site_folders(site_row_id: str, base_folder: str='') -> List[Dict[str,Any]]:
    site = get_row('autodoc_sharepoint_sites', site_row_id)
    if not site: raise RuntimeError('Site não encontrado no Supabase')
    drive_id = site['drive_id']
    children = sharepoint.children(drive_id, base_folder)
    saved=[]
    for item in children:
        if 'folder' not in item: continue
        payload = {
            'sharepoint_site_ref': site_row_id,
            'item_type': 'folder',
            'folder_name': item['name'],
            'folder_path': f'{base_folder}/{item["name"]}'.strip('/'),
            'item_id': item['id'],
            'web_url': item.get('webUrl'),
            'active': True,
            'raw': item,
        }
        saved.append(upsert_row('autodoc_sharepoint_inventory', payload, on_conflict='sharepoint_site_ref,folder_path'))
    return saved


def bootstrap_hml_structure(site_row_id: str, base_folder: str='') -> Dict[str,Any]:
    """Cria automaticamente a estrutura HML no site selecionado.

    Não altera pastas de produção. É idempotente: pode rodar várias vezes sem duplicar.
    Usa o inventário já sincronizado para replicar as pastas principais dentro de _AUTODOC_HOMOLOGACAO.
    """
    site = get_row('autodoc_sharepoint_sites', site_row_id)
    if not site: raise RuntimeError('Site não encontrado no Supabase')
    drive_id = site['drive_id']
    hml_root = settings.sharepoint_hml_root.strip('/ ')

    # Garante inventário de raiz. Se ainda não existir, sincroniza agora.
    inventory = list_rows('autodoc_sharepoint_inventory', limit=2000, filters={'sharepoint_site_ref': site_row_id, 'active': True})
    if not inventory:
        inventory = sync_site_folders(site_row_id, base_folder)

    root_folders = [
        i for i in inventory
        if i.get('item_type') == 'folder'
        and _is_root_folder(i.get('folder_path',''))
        and _is_business_folder(i.get('folder_name',''))
    ]

    ensured = []

    def ensure_and_record(path: str, category: str) -> Dict[str,Any]:
        item = sharepoint.ensure_folder(drive_id, path)
        row = upsert_row('autodoc_sharepoint_inventory', {
            'sharepoint_site_ref': site_row_id,
            'item_type': category,
            'folder_name': path.strip('/').split('/')[-1],
            'folder_path': path.strip('/'),
            'item_id': item.get('id'),
            'web_url': item.get('webUrl'),
            'active': True,
            'raw': item,
        }, on_conflict='sharepoint_site_ref,folder_path')
        ensured.append(row)
        return row

    ensure_and_record(hml_root, 'hml_root')
    for control in CONTROL_FOLDERS:
        ensure_and_record(f'{hml_root}/{control}', 'hml_control')
    for folder in root_folders:
        ensure_and_record(f'{hml_root}/{safe_path_part(folder["folder_name"])}', 'hml_business_folder')

    update_row('autodoc_sharepoint_sites', site_row_id, {'hml_ready': True, 'hml_ready_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), 'raw': {**(site.get('raw') or {}), 'hml_bootstrap': {'root': hml_root, 'folders_created_or_found': len(ensured)}}})
    return {
        'ok': True,
        'site_id': site_row_id,
        'project_name': site.get('project_name'),
        'hml_root': hml_root,
        'business_folders': [f.get('folder_name') for f in root_folders],
        'ensured_count': len(ensured),
        'ensured': ensured,
    }


def generate_folder_map(site_row_id: str, file_extensions: Optional[List[str]]=None, auto_approve_min_score: int=90) -> Dict[str,Any]:
    """Gera mapa Projeto + Disciplina -> pasta HML a partir do inventário do SharePoint."""
    site = get_row('autodoc_sharepoint_sites', site_row_id)
    if not site: raise RuntimeError('Site não encontrado no Supabase')
    file_extensions = file_extensions or ['pdf', 'dwg']
    inventory = list_rows('autodoc_sharepoint_inventory', limit=2000, filters={'sharepoint_site_ref': site_row_id, 'active': True})
    if not inventory:
        inventory = sync_site_folders(site_row_id, '')

    root_folders = [
        i for i in inventory
        if i.get('item_type') == 'folder'
        and _is_root_folder(i.get('folder_path',''))
        and _is_business_folder(i.get('folder_name',''))
    ]

    maps = []
    aliases_project = site.get('aliases') or []
    for folder in root_folders:
        folder_name = folder['folder_name']
        folder_path = safe_path_part(folder_name)
        hml_path = target_path({'sharepoint_folder_path': folder_path}, 'HML')
        payload = {
            'sharepoint_site_ref': site_row_id,
            'project_autodoc': site.get('project_name') or '',
            'project_aliases': aliases_project,
            'discipline_autodoc': folder_name,
            'discipline_normalized': normalize_text(folder_name),
            'sharepoint_path': hml_path,
            'sharepoint_folder_path': folder_path,
            'file_extensions': [x.lower().lstrip('.') for x in file_extensions],
            'auto_approve_min_score': auto_approve_min_score,
            'active': True,
            'environment': 'HML',
            'aliases': [folder_name, normalize_text(folder_name)],
            'notes': 'Gerado automaticamente pelo bootstrap HML V4.2',
        }
        maps.append(upsert_row('autodoc_folder_map', payload, on_conflict='project_autodoc,discipline_normalized,sharepoint_path'))

    update_row('autodoc_sharepoint_sites', site_row_id, {'folder_map_ready': True, 'folder_map_ready_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()})
    return {
        'ok': True,
        'site_id': site_row_id,
        'project_name': site.get('project_name'),
        'maps_created_or_updated': len(maps),
        'maps': maps,
    }


def prepare_site_hml(site_row_id: str, base_folder: str='', file_extensions: Optional[List[str]]=None) -> Dict[str,Any]:
    """Executa a preparação completa do site: sincroniza, cria HML e gera mapas."""
    synced = sync_site_folders(site_row_id, base_folder)
    boot = bootstrap_hml_structure(site_row_id, base_folder)
    fmap = generate_folder_map(site_row_id, file_extensions=file_extensions)
    return {'ok': True, 'synced_folders': synced, 'bootstrap': boot, 'folder_map': fmap}
