from __future__ import annotations

from typing import Any, Dict
import re


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).upper()


def score_candidate(expected: Dict[str, Any], candidate: Dict[str, Any]) -> int:
    """Pontua candidato para evitar baixar arquivo errado."""
    expected_name = norm(expected.get('file_name'))
    expected_code = norm(expected.get('code') or (expected.get('file_name') or '').rsplit('.', 1)[0])
    expected_ext = norm(expected.get('extension'))
    expected_rev = norm(expected.get('revision'))
    expected_project = norm(expected.get('project_hint') or expected.get('project_detected'))
    expected_disc = norm(expected.get('discipline_hint') or expected.get('discipline_detected'))
    expected_path = norm(expected.get('folder_hint') or expected.get('autodoc_path'))

    hay = norm(' '.join(str(candidate.get(k) or '') for k in ['file_name','code','title','path','folder','project','discipline','status','revision','extension']))
    score = 0
    if expected_name and expected_name in hay:
        score += 100
    if expected_code and expected_code in hay:
        score += 80
    if expected_rev and expected_rev in hay:
        score += 40
    if expected_ext and expected_ext in hay:
        score += 30
    if expected_project and expected_project in hay:
        score += 30
    if expected_disc and expected_disc in hay:
        score += 25
    if expected_path and expected_path in hay:
        score += 25
    if any(x in hay for x in ['APROVADO','LIBERADO','VIGENTE']):
        score += 15
    if any(x in hay for x in ['OBSOLETO','OBSOLETOS']) and not expected.get('allow_obsolete'):
        score -= 80
    if 'BLOQUEADO' in hay:
        score -= 20
    return score


def decision(score: int, min_auto: int = 120, manual_review: int = 80) -> str:
    if score >= min_auto:
        return 'AUTO_DOWNLOAD_ALLOWED'
    if score >= manual_review:
        return 'MANUAL_REVIEW_SCORE_RANGE'
    return 'MANUAL_REVIEW_LOW_SCORE'
