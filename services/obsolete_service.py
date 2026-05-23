from datetime import datetime
from pathlib import Path
from services.sharepoint_service import sharepoint
from services.normalizer import safe_path_part
from core.config import settings

def move_existing_to_obsolete(drive_id: str, target_folder: str, file_name: str) -> dict | None:
    target = f'{target_folder.strip("/")}/{safe_path_part(file_name)}'
    existing = sharepoint.item_by_path(drive_id, target)
    if not existing: return None
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    obsolete_folder = f'{settings.sharepoint_hml_root}/{settings.sharepoint_obsolete_folder}/{target_folder.replace(settings.sharepoint_hml_root, "").strip("/")}'.strip('/')
    new_name = f'{stamp}_{safe_path_part(file_name)}'
    return sharepoint.move_item(drive_id, target, obsolete_folder, new_name)
