from __future__ import annotations

"""
Robot v2 shadow engine.

Este engine ainda NÃO faz navegação real. Ele consome parser_payload.download_plan,
seleciona a estratégia segura e gera evidência. Para não quebrar HML, ele cria um
arquivo local JSON quando AUTODOC_ROBOT_ENGINE=v2 ou AUTODOC_ROBOT_SHADOW=true.
"""

from pathlib import Path
from typing import Any, Dict, List
import hashlib
import json
import os

from .evidence import save_json, now_iso


def _first_file_plan(file_row: Dict[str, Any]) -> Dict[str, Any]:
    parser_payload = file_row.get('parser_payload') if isinstance(file_row.get('parser_payload'), dict) else {}
    if parser_payload.get('download_plan'):
        return parser_payload
    if parser_payload.get('files') and isinstance(parser_payload.get('files'), list):
        for item in parser_payload['files']:
            if (item.get('file_name') or '').lower() == (file_row.get('file_name') or '').lower():
                return item
        return parser_payload['files'][0]
    return file_row


def _ranked_links(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    links = plan.get('ranked_links') or plan.get('links') or []
    if isinstance(links, list):
        return [x for x in links if isinstance(x, dict)]
    return []


def run_shadow_download_plan(project: str, autodoc_path: str, file_name: str, context: Dict[str, Any]) -> Dict[str, Any]:
    file_id = context.get('id') or context.get('file_id') or ''
    job_id = context.get('job_id') or context.get('robot_job_id') or ''
    plan = _first_file_plan(context)
    download_plan = plan.get('download_plan') if isinstance(plan.get('download_plan'), dict) else {}
    strategies = download_plan.get('strategy_order') or ['EMAIL_FILE_PAGE', 'REPORT_SEARCH', 'DIRECTORY_SEARCH', 'MANUAL_REVIEW']
    links = _ranked_links(plan)
    top_links = links[: int(download_plan.get('max_links_to_try') or 3)]

    result = {
        'ok': True,
        'engine': 'autodoc_v2_shadow',
        'shadow': True,
        'status': 'SHADOW_PLAN_OK',
        'file_id': file_id,
        'job_id': job_id,
        'file_name': file_name,
        'project': project,
        'autodoc_path': autodoc_path,
        'account_hint': plan.get('account_hint') or context.get('account_hint'),
        'project_hint': plan.get('project_hint') or context.get('project_detected'),
        'discipline_hint': plan.get('discipline_hint') or context.get('discipline_detected'),
        'folder_hint': plan.get('folder_hint') or context.get('autodoc_path'),
        'strategy_order': strategies,
        'top_links': top_links,
        'links_count': len(links),
        'selected_strategy': strategies[0] if strategies else 'MANUAL_REVIEW',
        'manual_review_required': (not file_name) or (not links and 'REPORT_SEARCH' not in strategies and 'DIRECTORY_SEARCH' not in strategies),
        'created_at': now_iso(),
    }

    evidence = save_json('autodoc_v2_shadow_plan.json', result, job_id=job_id, file_id=file_id)

    # Cria arquivo local para o worker completar o job sem download real.
    downloads_dir = Path(os.getenv('WORKER_DOWNLOADS_DIR') or 'storage/worker_downloads')
    downloads_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (file_name or 'autodoc_v2_shadow').replace('/', '_').replace('\\', '_')
    local = downloads_dir / f'{safe_name}.shadow.json'
    local.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    data = local.read_bytes()

    return {
        **result,
        'local_path': str(local),
        'local_sha256': hashlib.sha256(data).hexdigest(),
        'local_size_bytes': len(data),
        'download_mode': 'autodoc_v2_shadow_plan',
        'evidence_path': str(evidence),
    }
