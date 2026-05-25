from pathlib import Path
from typing import Any, Dict
from core.database import get_row, update_row, insert_history, insert_error, now_iso
from core.config import settings
from core import statuses as st
from services.sharepoint_mapper import resolve_folder, target_path
from services.scoring_service import score_file
from services.obsolete_service import move_existing_to_obsolete
from services.sharepoint_service import sharepoint

def enrich_file_destination(file_id: str) -> Dict[str, Any]:
    f = get_row('autodoc_files', file_id)
    if not f:
        raise RuntimeError('Arquivo não encontrado')

    mapping = resolve_folder(
        f.get('project_detected', ''),
        f.get('discipline_detected', ''),
        f.get('autodoc_path', ''),
        f.get('extension', ''),
    )
    scored = score_file(f, mapping)
    update = {**scored, 'last_processed_at': now_iso()}

    if mapping:
        site = mapping['_site']
        folder_path = target_path(mapping, settings.app_env)
        update.update({
            'sharepoint_site_ref': site['id'],
            'sharepoint_drive_id': site['drive_id'],
            'sharepoint_suggested_path': folder_path,
            'sharepoint_folder_map_ref': mapping.get('id'),
            'sharepoint_final_path': folder_path,
        })

    updated = update_row('autodoc_files', file_id, update)
    insert_history(
        email_id=f.get('email_id'),
        file_id=file_id,
        action='DESTINO_ENRIQUECIDO',
        file_name=f.get('file_name'),
        to_path=updated.get('sharepoint_suggested_path'),
        environment=settings.app_env,
        status=updated.get('status'),
        message='Destino SharePoint enriquecido a partir do mapa de pastas.',
    )
    return updated

def upload_hml(file_id: str) -> Dict[str, Any]:
    if not settings.enable_sharepoint_upload:
        raise RuntimeError('Upload SharePoint desativado no .env')

    f = get_row('autodoc_files', file_id)
    if not f:
        raise RuntimeError('Arquivo não encontrado')

    if not f.get('sharepoint_drive_id') or not f.get('sharepoint_suggested_path'):
        f = enrich_file_destination(file_id)

    local_path = f.get('local_path')
    if not local_path or not Path(local_path).exists():
        raise RuntimeError('local_path não encontrado. Baixe anexo ou use upload-local antes.')

    drive_id = f['sharepoint_drive_id']
    target_folder = f['sharepoint_suggested_path']
    file_name = f['file_name']

    update_row('autodoc_files', file_id, {'upload_started_at': now_iso()})

    moved = None
    if settings.enable_obsolete_rule:
        moved = move_existing_to_obsolete(drive_id, target_folder, file_name)
        if moved:
            insert_history(
                email_id=f.get('email_id'),
                file_id=file_id,
                action=st.ACTION_MOVIDO_OBSOLETO,
                file_name=file_name,
                from_path=f'{target_folder}/{file_name}',
                to_path=moved.get('webUrl'),
                obsolete_path=moved.get('webUrl'),
                environment='HML',
                status='OK',
            )

    item = sharepoint.upload_small(drive_id, target_folder, file_name, local_path)

    updated = update_row('autodoc_files', file_id, {
        'status': st.FILE_SALVO_HML,
        'sharepoint_item_id': item.get('id'),
        'sharepoint_web_url': item.get('webUrl'),
        'sharepoint_final_path': target_folder,
        'upload_finished_at': now_iso(),
        'error_message': None,
    })

    insert_history(
        email_id=f.get('email_id'),
        file_id=file_id,
        action=st.ACTION_UPLOAD_HML,
        file_name=file_name,
        to_path=item.get('webUrl') or f'{target_folder}/{file_name}',
        environment='HML',
        status='OK',
        message='Arquivo enviado para HML no SharePoint.',
    )
    return {'file': updated, 'uploaded': item, 'obsolete': moved}
