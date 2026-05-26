from __future__ import annotations

"""
MIA AUTODOC — Email Intelligence v2

Responsabilidade:
- Extrair links/botões, URLs em texto, arquivos, caminhos e pistas de conta/projeto.
- Classificar e ranquear links mesmo quando o e-mail tem 20+ links.
- Gerar download_plan seguro para o worker/robô.

Este módulo NÃO acessa AutoDoc, NÃO baixa arquivo e NÃO altera status.
Ele apenas transforma e-mail em plano de ação.
"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, unquote
import hashlib
import json
import re

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None

try:
    from .normalizer import normalize_text, ext_of
except Exception:  # permite teste isolado
    def normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "").strip()).upper()

    def ext_of(value: str) -> str:
        return (value.rsplit('.', 1)[-1].lower() if value and '.' in value else '')


SUPPORTED_EXTENSIONS = [
    "pdf", "dwg", "dxf", "ifc", "rvt", "rfa", "nwc", "nwd",
    "zip", "rar", "7z", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "png", "jpg", "jpeg", "xml", "txt",
]

FILE_RE = re.compile(
    r"(?P<file>[A-Za-z0-9À-ÿ_\-\.\s\(\)]+\.(:?" + "|".join(SUPPORTED_EXTENSIONS) + r"))",
    re.IGNORECASE,
)
# Corrige grupo não capturante para versões antigas do regex acima
FILE_RE = re.compile(
    r"(?P<file>[A-Za-z0-9À-ÿ_\-\.\s\(\)]+\.(?:" + "|".join(SUPPORTED_EXTENSIONS) + r"))",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

USELESS_URL_TOKENS = [
    "unsubscribe", "descadastrar", "privacidade", "privacy", "terms", "termos",
    "central-de-ajuda", "help", "suporte", "facebook", "instagram", "linkedin",
    "youtube", "logo", "pixel", "tracking", "open?", "utm_source=newsletter",
]

DOWNLOAD_TEXT_TOKENS = ["download", "baixar", "baixe", "arquivo", "documento", "anexo"]
ACCESS_TEXT_TOKENS = ["acessar", "abrir", "visualizar", "ver arquivo", "consultar"]
FOLDER_TOKENS = ["folder", "directories", "diretorios", "diretórios", "pasta"]
REPORT_TOKENS = ["report", "reports", "relatorios", "relatórios"]
FILE_PAGE_TOKENS = ["file", "files", "document", "documents", "arquivo", "documento"]
AUTODOC_DOMAIN_TOKENS = ["autodoc", "plataforma.autodoc"]
OBSOLETE_TOKENS = ["obsoleto", "obsoletos", "obsolete"]

DISCIPLINE_ALIASES = {
    "ARQ": "ARQUITETURA",
    "ARQUITETURA": "ARQUITETURA",
    "ASC": "ACESSIBILIDADE",
    "ACESSIBILIDADE": "ACESSIBILIDADE",
    "ACU": "ACÚSTICA",
    "ACUSTICA": "ACÚSTICA",
    "EST": "ESTRUTURA",
    "ESTRUTURA": "ESTRUTURA",
    "ELE": "ELÉTRICA",
    "ELET": "ELÉTRICA",
    "ELÉTRICA": "ELÉTRICA",
    "HID": "HIDRÁULICA",
    "HIDRÁULICA": "HIDRÁULICA",
    "BOM": "BOMBEIRO",
    "BOMBEIRO": "BOMBEIRO",
    "COO": "COORDENAÇÃO",
    "COORD": "COORDENAÇÃO",
    "COORDENAÇÃO": "COORDENAÇÃO",
    "ANCORAGEM": "ANCORAGEM",
    "PDF": "PDF",
    "DWG": "DWG",
    "IFC": "IFC",
}

PHASE_RE = re.compile(r"\b(?:0?[1-9]|1[0-9])\s*[-–]\s*[^\n\r/|]{3,80}", re.IGNORECASE)
REV_RE = re.compile(r"\b(?:R|REV)[\s_\-]?(\d{1,3})\b|\bR(\d{2,3})\b", re.IGNORECASE)


def _sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _clean_url(url: str) -> str:
    value = (url or '').strip().replace('&amp;', '&')
    value = value.rstrip(').,;]}>')
    return value


def _is_url(value: str) -> bool:
    try:
        p = urlparse(value or '')
        return p.scheme in {'http', 'https'} and bool(p.netloc)
    except Exception:
        return False


def _is_useful_url(url: str) -> bool:
    url = _clean_url(url)
    if not _is_url(url):
        return False
    lowered = url.lower()
    return not any(tok in lowered for tok in USELESS_URL_TOKENS)


def _html_text(html: str) -> str:
    if not html:
        return ''
    if BeautifulSoup is None:
        return re.sub(r'<[^>]+>', ' ', html)
    soup = BeautifulSoup(html, 'lxml')
    return soup.get_text('\n', strip=True)


def _visible_text(node: Any) -> str:
    try:
        return ' '.join((node.get_text(' ', strip=True) or '').split())
    except Exception:
        return ''


def extract_links(html: str = '', text: str = '') -> List[Dict[str, Any]]:
    """Extrai links do HTML e texto, preservando contexto perto do botão/link."""
    links: List[Dict[str, Any]] = []
    seen = set()

    if BeautifulSoup is not None and html:
        soup = BeautifulSoup(html, 'lxml')
        for a in soup.find_all('a', href=True):
            url = _clean_url(a.get('href') or '')
            if not _is_useful_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            parent_text = ''
            try:
                parent_text = _visible_text(a.parent)[:700]
            except Exception:
                pass
            links.append({
                'url': url,
                'text': _visible_text(a),
                'title': a.get('title') or '',
                'context': parent_text,
                'source': 'html_href',
            })

    combined_text = text or _html_text(html)
    for match in URL_RE.finditer(combined_text or ''):
        url = _clean_url(match.group(0))
        if not _is_useful_url(url) or url in seen:
            continue
        seen.add(url)
        start = max(0, match.start() - 260)
        end = min(len(combined_text), match.end() + 260)
        links.append({
            'url': url,
            'text': '',
            'title': '',
            'context': combined_text[start:end],
            'source': 'text_url',
        })

    return links


def classify_link(url: str, text: str = '', context: str = '') -> str:
    joined = normalize_text(' '.join([url or '', text or '', context or ''])).lower()
    url_l = (url or '').lower()

    if any(tok in joined for tok in DOWNLOAD_TEXT_TOKENS) or any(tok in url_l for tok in ['download', 'baixar']):
        return 'DIRECT_DOWNLOAD'
    if any(tok in url_l for tok in FOLDER_TOKENS):
        return 'FOLDER_PAGE'
    if any(tok in url_l for tok in REPORT_TOKENS):
        return 'REPORT_PAGE'
    if any(tok in url_l for tok in FILE_PAGE_TOKENS):
        return 'FILE_PAGE'
    if any(tok in joined for tok in ACCESS_TEXT_TOKENS):
        return 'FILE_PAGE'
    if any(tok in url_l for tok in ['login', 'signin', 'account']):
        return 'LOGIN_PAGE'
    if any(tok in url_l for tok in AUTODOC_DOMAIN_TOKENS):
        return 'AUTODOC_PAGE'
    return 'UNKNOWN'


def _file_tokens(file_name: str) -> List[str]:
    fn = (file_name or '').strip()
    if not fn:
        return []
    stem = fn.rsplit('.', 1)[0] if '.' in fn else fn
    pieces = [fn, stem, stem.replace('-', ' '), stem.replace('_', ' ')]
    return [p.lower() for p in pieces if p]


def score_link_for_file(link: Dict[str, Any], file_name: str = '') -> int:
    url = link.get('url') or ''
    text = link.get('text') or ''
    title = link.get('title') or ''
    context = link.get('context') or ''
    joined = normalize_text(' '.join([url, text, title, context])).lower()
    url_l = url.lower()

    score = 0
    if any(tok in url_l for tok in AUTODOC_DOMAIN_TOKENS):
        score += 30
    if any(tok in joined for tok in DOWNLOAD_TEXT_TOKENS):
        score += 100
    if any(tok in joined for tok in ACCESS_TEXT_TOKENS):
        score += 70
    if any(tok in url_l for tok in ['download', 'baixar']):
        score += 90
    if any(tok in url_l for tok in FILE_PAGE_TOKENS):
        score += 55
    if any(tok in url_l for tok in FOLDER_TOKENS):
        score += 45
    if any(tok in url_l for tok in REPORT_TOKENS):
        score += 40

    for token in _file_tokens(file_name):
        if token and token in joined:
            score += 90 if '.' in token else 55
            break

    if any(tok in joined for tok in USELESS_URL_TOKENS):
        score -= 120
    if 'login' in url_l or 'signin' in url_l:
        score -= 25

    return max(score, -100)


def rank_links(links: List[Dict[str, Any]], file_name: str = '', max_links: int = 50) -> List[Dict[str, Any]]:
    ranked = []
    for link in links:
        type_guess = classify_link(link.get('url') or '', link.get('text') or '', link.get('context') or '')
        score = score_link_for_file(link, file_name)
        if score < 0:
            continue
        ranked.append({
            **link,
            'type_guess': type_guess,
            'score': score,
        })
    ranked.sort(key=lambda item: item.get('score', 0), reverse=True)
    return ranked[:max_links]


def extract_files_from_text(value: str) -> List[Dict[str, Any]]:
    files: List[Dict[str, Any]] = []
    seen = set()
    for match in FILE_RE.finditer(value or ''):
        fn = re.sub(r'\s+', ' ', match.group('file')).strip(' .;:-|')
        key = fn.lower()
        if not fn or key in seen:
            continue
        seen.add(key)
        files.append(build_file_stub(fn, source='email_intelligence_regex'))
    return files


def guess_revision(file_name: str) -> str:
    m = REV_RE.search(file_name or '')
    if not m:
        return ''
    return 'R' + (m.group(1) or m.group(2) or '').zfill(2)


def build_file_stub(file_name: str, source: str = 'email_intelligence') -> Dict[str, Any]:
    stem = file_name.rsplit('.', 1)[0] if '.' in file_name else file_name
    return {
        'file_name': file_name,
        'extension': ext_of(file_name),
        'code': stem,
        'revision': guess_revision(file_name),
        'search_terms': [x for x in [file_name, stem, stem.replace('-', ' '), stem.replace('_', ' ')] if x],
        'source': source,
    }


def _find_labeled_value(text: str, labels: Iterable[str]) -> str:
    for label in labels:
        pattern = re.compile(rf"{re.escape(label)}\s*[:\-]\s*([^\n\r|]+)", re.IGNORECASE)
        m = pattern.search(text or '')
        if m:
            return m.group(1).strip()[:180]
    return ''


def extract_context_hints(subject: str = '', text: str = '', html: str = '') -> Dict[str, Any]:
    plain = text or _html_text(html)
    joined = '\n'.join([subject or '', plain or ''])
    norm = normalize_text(joined)

    account_hint = _find_labeled_value(joined, ['Conta', 'Account', 'Cliente'])
    project_hint = _find_labeled_value(joined, ['Projeto', 'Obra', 'Empreendimento'])
    discipline_hint = _find_labeled_value(joined, ['Disciplina', 'Especialidade'])
    phase_hint = _find_labeled_value(joined, ['Fase', 'Etapa'])
    folder_hint = _find_labeled_value(joined, ['Pasta', 'Caminho', 'Diretório', 'Diretorio'])

    # Heurística por assunto: "SAE - GUANÁS", "GRAAL", etc.
    if not account_hint:
        m = re.search(r"\b(GRAAL|YOU|CASAINC|CAPTA|INTEGRA\s+URBANO|BRICKS|BARBARA)\b", joined, re.IGNORECASE)
        account_hint = m.group(1).strip() if m else ''

    if not project_hint:
        m = re.search(r"\b(?:SAE|GRAAL|YOU|CASAINC|CAPTA)\s*[-–]\s*[^\n\r|]+", joined, re.IGNORECASE)
        project_hint = m.group(0).strip() if m else ''

    # Disciplina por token
    if not discipline_hint:
        for token, name in DISCIPLINE_ALIASES.items():
            if re.search(rf"\b{re.escape(token)}\b", norm, re.IGNORECASE):
                discipline_hint = name
                break

    if not phase_hint:
        m = PHASE_RE.search(joined)
        phase_hint = m.group(0).strip() if m else ''

    path_candidates: List[str] = []
    for line in (joined or '').splitlines():
        if '/' in line or '\\' in line:
            cleaned = re.sub(r'\s+', ' ', line).strip()
            if 3 <= len(cleaned) <= 260:
                path_candidates.append(cleaned)

    allow_obsolete = any(tok in norm for tok in OBSOLETE_TOKENS)

    return {
        'account_hint': account_hint,
        'project_hint': project_hint,
        'discipline_hint': discipline_hint,
        'phase_hint': phase_hint,
        'folder_hint': folder_hint,
        'path_candidates': path_candidates[:10],
        'allow_obsolete': allow_obsolete,
    }


def _strategy_order_for_file(file_links: List[Dict[str, Any]], has_file_name: bool, has_path: bool, allow_obsolete: bool) -> List[str]:
    strategies: List[str] = []
    types = [l.get('type_guess') for l in file_links[:5]]
    if 'DIRECT_DOWNLOAD' in types:
        strategies.append('EMAIL_DIRECT_DOWNLOAD')
    if any(t in types for t in ['FILE_PAGE', 'AUTODOC_PAGE']):
        strategies.append('EMAIL_FILE_PAGE')
    if 'FOLDER_PAGE' in types:
        strategies.append('EMAIL_FOLDER_PAGE')
    if 'REPORT_PAGE' in types:
        strategies.append('EMAIL_REPORT_PAGE')
    if has_file_name:
        strategies.append('REPORT_SEARCH')
    if has_path:
        strategies.append('DIRECTORY_SEARCH')
    if allow_obsolete:
        strategies.append('OBSOLETE_SEARCH')
    strategies.append('ACCOUNT_SCAN')
    strategies.append('MANUAL_REVIEW')

    dedup: List[str] = []
    for s in strategies:
        if s not in dedup:
            dedup.append(s)
    return dedup


def build_download_plan(
    subject: str = '',
    html: str = '',
    text: str = '',
    attachments: Optional[List[Dict[str, Any]]] = None,
    base_files: Optional[List[Dict[str, Any]]] = None,
    links: Optional[List[Dict[str, Any]]] = None,
    project_hint: str = '',
) -> Dict[str, Any]:
    """Gera plano de download blindado para 1, 5, 20+ links ou só caminho/nome."""
    attachments = attachments or []
    plain = text or _html_text(html)
    all_links = links if links is not None else extract_links(html, plain)
    hints = extract_context_hints(subject=subject, text=plain, html=html)
    if project_hint and not hints.get('project_hint'):
        hints['project_hint'] = project_hint

    files: List[Dict[str, Any]] = []
    seen = set()

    for item in base_files or []:
        fn = item.get('file_name') or item.get('filename') or item.get('name') or ''
        if not fn:
            continue
        key = fn.lower()
        if key in seen:
            continue
        seen.add(key)
        merged = {**build_file_stub(fn, source=item.get('source') or 'base_file'), **item}
        files.append(merged)

    for item in extract_files_from_text(plain):
        key = (item.get('file_name') or '').lower()
        if key and key not in seen:
            seen.add(key)
            files.append(item)

    for att in attachments:
        fn = att.get('fileName') or att.get('filename') or att.get('name') or ''
        if fn and fn.lower() not in seen:
            seen.add(fn.lower())
            files.append({**build_file_stub(fn, source='attachment'), 'has_attachment': True, 'raw': att})

    global_ranked_links = rank_links(all_links, '', max_links=80)

    planned_files: List[Dict[str, Any]] = []
    for f in files:
        fn = f.get('file_name') or ''
        ranked = rank_links(all_links, fn, max_links=20)
        if not ranked:
            ranked = global_ranked_links[:5]

        has_path = bool(f.get('autodoc_path') or hints.get('folder_hint') or hints.get('path_candidates'))
        strategy_order = _strategy_order_for_file(
            ranked,
            has_file_name=bool(fn),
            has_path=has_path,
            allow_obsolete=bool(hints.get('allow_obsolete')),
        )

        top_download_url = ''
        top_file_page_url = ''
        for l in ranked:
            if not top_download_url and l.get('type_guess') == 'DIRECT_DOWNLOAD':
                top_download_url = l.get('url') or ''
            if not top_file_page_url and l.get('type_guess') in {'FILE_PAGE', 'AUTODOC_PAGE', 'FOLDER_PAGE', 'REPORT_PAGE'}:
                top_file_page_url = l.get('url') or ''

        planned = {
            **f,
            'account_hint': f.get('account_hint') or hints.get('account_hint') or '',
            'project_hint': f.get('project_hint') or f.get('project_detected') or hints.get('project_hint') or '',
            'discipline_hint': f.get('discipline_hint') or f.get('discipline_detected') or hints.get('discipline_hint') or '',
            'phase_hint': f.get('phase_hint') or hints.get('phase_hint') or '',
            'folder_hint': f.get('folder_hint') or hints.get('folder_hint') or '',
            'path_candidates': hints.get('path_candidates') or [],
            'ranked_links': ranked[:20],
            'links': ranked[:10],
            'download_url': f.get('download_url') or top_download_url,
            'file_page_url': f.get('file_page_url') or top_file_page_url,
            'autodoc_urls': [x.get('url') for x in ranked[:10] if x.get('url')],
            'download_plan': {
                'version': '2.0',
                'strategy_order': strategy_order,
                'max_links_to_try': 3,
                'max_accounts_to_scan': 5,
                'min_auto_download_score': 120,
                'manual_review_score': 80,
                'allow_obsolete': bool(hints.get('allow_obsolete')),
                'source_hash': _sha({'file_name': fn, 'subject': subject, 'top_links': [x.get('url') for x in ranked[:3]]}),
            },
        }
        planned_files.append(planned)

    # Se não houver arquivo, ainda devolve diagnóstico de links/caminhos para revisão manual.
    return {
        'email_intelligence_version': '2.0',
        'has_links': bool(all_links),
        'links_count': len(all_links),
        'ignored_links_count': max(0, len(all_links) - len(global_ranked_links)),
        'ranked_links': global_ranked_links[:30],
        'links': all_links[:100],
        **hints,
        'files': planned_files,
        'files_count': len(planned_files),
        'strategy_note': 'email_link_first_then_report_then_directory_then_manual_review',
    }
