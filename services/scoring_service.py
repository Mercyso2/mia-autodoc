from typing import Dict, Any
from core.config import settings
from .normalizer import ext_of

def score_file(file_data: Dict[str,Any], mapping: Dict[str,Any] | None) -> Dict[str,Any]:
    score = 0
    reasons = []
    if mapping:
        score += 30; reasons.append('Projeto encontrado no mapa')
        if mapping.get('discipline_normalized'): score += 25; reasons.append('Disciplina encontrada')
        if mapping.get('sharepoint_folder_path'): score += 20; reasons.append('Pasta SharePoint definida')
    fn = file_data.get('file_name') or ''
    if fn and '.' in fn and len(fn) >= 5: score += 15; reasons.append('Nome de arquivo válido')
    ext = (file_data.get('extension') or ext_of(fn)).lower()
    allowed = (mapping or {}).get('file_extensions') or []
    if isinstance(allowed, str): allowed = [x.strip().lower() for x in allowed.split(',') if x.strip()]
    if ext and (not allowed or ext in allowed): score += 10; reasons.append('Extensão permitida')
    if score >= settings.min_auto_score: status = 'PRONTO_PARA_SHAREPOINT'
    elif score >= settings.min_review_score: status = 'AGUARDANDO_APROVACAO'
    else: status = 'NAO_IDENTIFICADO'
    return {'confidence_score': min(score,100), 'status': status, 'score_reasons': reasons}
