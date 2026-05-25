from bs4 import BeautifulSoup
import re
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from .normalizer import ext_of, normalize_text

FILE_RE = re.compile(r'([A-Za-z0-9_\-\.\s\(\)À-ÿ]+\.(?:pdf|dwg|docx?|xlsx?|zip|rvt|ifc|png|jpg|jpeg))', re.I)
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)

STATUS_LABELS = [
    'STATUS',
    'SITUAÇÃO',
    'SITUACAO',
    'REVISAO',
    'REVISÃO',
]

DISCIPLINE_HINTS = [
    ('ARQ', 'ARQUITETURA'),
    ('ARQUITETURA', 'ARQUITETURA'),
    ('EST', 'ESTRUTURA'),
    ('ESTRUTURA', 'ESTRUTURA'),
    ('ELE', 'ELÉTRICA'),
    ('ELET', 'ELÉTRICA'),
    ('ELÉTRICA', 'ELÉTRICA'),
    ('HID', 'HIDRÁULICA'),
    ('HIDRÁULICA', 'HIDRÁULICA'),
    ('COORD', 'COORDENAÇÃO'),
    ('COORDENAÇÃO', 'COORDENAÇÃO'),
    ('COMAER', 'COMAER'),
    ('EVTL', 'EVTL'),
]

DOWNLOAD_WORDS = [
    'download',
    'baixar',
    'baixe',
    'arquivo',
    'documento',
    'anexo',
    'visualizar',
    'abrir',
    'acessar',
]

AUTODOC_DOMAIN_HINTS = [
    'autodoc',
    'plataforma.autodoc',
]


def make_autodoc_path_safe(value: str) -> str:
    value = normalize_text(value or '')
    value = value.replace('\\', '/')
    value = re.sub(r'\s*/\s*', '/', value)
    value = re.sub(r'\s+', ' ', value)
    return value.strip('/ ').upper()


def detect_email_type(subject: str, text: str = '') -> str:
    s = normalize_text((subject or '') + ' ' + (text or '')[:1000])
    if 'RESUMO INSTANTANEO' in s and 'UPLOAD' in s:
        return 'RESUMO_INSTANTANEO_UPLOAD'
    if 'RELATORIO DIARIO' in s and 'UPLOAD' in s:
        return 'RELATORIO_DIARIO_UPLOAD'
    if 'RELATORIO DIARIO' in s and 'DOWNLOAD' in s:
        return 'RELATORIO_DIARIO_DOWNLOAD'
    if 'RVE' in s:
        return 'STATUS_RVE'
    if 'SHAREPOINT' in s:
        return 'EMAIL_SHAREPOINT'
    return 'EMAIL_DESCONHECIDO'


def _cell_text(td) -> str:
    return ' '.join(td.get_text(' ', strip=True).split())


def _first_present(d: Dict[str, str], aliases: List[str]) -> str:
    for a in aliases:
        key = normalize_text(a)
        if key in d and str(d[key]).strip():
            return str(d[key]).strip()
    return ''


def _extract_project(subject: str, text: str) -> str:
    candidates = []

    for label in ['Projeto', 'Obra', 'Empreendimento']:
        m = re.search(label + r'\s*[:\-]\s*(.+)', text or '', re.I)
        if m:
            candidates.append(m.group(1).split('\n')[0].strip())

    m = re.search(r'\b(?:SAE|CASAINC|GRAAL|TENDA|ADMINISTRATIVO|COMERCIAL|RMSP)\s*[-–]\s*[^\n\r\|]+', subject or '', re.I)
    if m:
        candidates.append(m.group(0).strip())

    return candidates[0] if candidates else ''


def _guess_discipline(file_name: str, autodoc_path: str = '', row_text: str = '') -> str:
    haystack = normalize_text(' '.join([file_name or '', autodoc_path or '', row_text or ''])).upper()
    for token, discipline in DISCIPLINE_HINTS:
        if token in haystack:
            return discipline

    parts = [p.strip() for p in re.split(r'[\\/]', autodoc_path or '') if p.strip()]
    if parts:
        return parts[-1] if len(parts) == 1 else parts[-2]

    return ''


def _extract_title(d: Dict[str, str], row_text: str) -> str:
    title = _first_present(d, ['Titulo', 'Título', 'Title', 'Descricao', 'Descrição', 'Description'])
    if title:
        return title

    cleaned = FILE_RE.sub('', row_text or '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' |;-')
    return cleaned[:180]


def _extract_autodoc_user(d: Dict[str, str]) -> str:
    return _first_present(d, ['Usuario', 'Usuário', 'User', 'Enviado por', 'Publicado por'])


def _extract_autodoc_datetime(d: Dict[str, str]) -> str:
    return _first_present(d, ['Data', 'Data/Hora', 'Date', 'Criado em', 'Publicado em', 'Atualizado em'])


def _clean_url(url: str) -> str:
    url = (url or '').strip()
    url = url.replace('&amp;', '&')
    url = url.rstrip(').,;]')
    return url


def _is_useful_url(url: str) -> bool:
    url = _clean_url(url)
    if not url:
        return False

    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'}:
        return False

    lowered = url.lower()
    if lowered.startswith('mailto:'):
        return False

    # Evita links meramente decorativos comuns em e-mails.
    if any(x in lowered for x in ['unsubscribe', 'descadastrar', 'privacy', 'politica-de-privacidade']):
        return False

    return True


def _link_score_for_file(link: Dict[str, Any], file_name: str) -> int:
    url = (link.get('url') or '').lower()
    text = normalize_text(link.get('text') or '').lower()
    title = normalize_text(link.get('title') or '').lower()
    context = normalize_text(link.get('context') or '').lower()
    joined = ' '.join([url, text, title, context])

    score = 0
    fn = (file_name or '').lower()

    if fn and fn in joined:
        score += 100

    stem = fn.rsplit('.', 1)[0] if '.' in fn else fn
    if stem and stem in joined:
        score += 40

    if any(word in joined for word in DOWNLOAD_WORDS):
        score += 25

    if any(domain in url for domain in AUTODOC_DOMAIN_HINTS):
        score += 20

    if any(word in url for word in ['download', 'baixar', 'arquivo', 'document', 'file']):
        score += 30

    if any(word in url for word in ['login', 'account', 'conta']):
        score -= 15

    return score


def _extract_links_from_html(html: str = '', plain: str = '') -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html or '', 'lxml')
    links: List[Dict[str, Any]] = []
    seen = set()

    for a in soup.find_all('a', href=True):
        url = _clean_url(a.get('href') or '')
        if not _is_useful_url(url):
            continue

        text = _cell_text(a)
        title = a.get('title') or ''
        parent_text = ''
        try:
            parent_text = ' '.join((a.parent.get_text(' ', strip=True) or '').split())[:500]
        except Exception:
            parent_text = ''

        key = url
        if key in seen:
            continue
        seen.add(key)

        links.append({
            'url': url,
            'text': text,
            'title': title,
            'context': parent_text,
            'source': 'html_href',
        })

    for m in URL_RE.finditer(plain or ''):
        url = _clean_url(m.group(0))
        if not _is_useful_url(url) or url in seen:
            continue

        start = max(0, m.start() - 180)
        end = min(len(plain), m.end() + 180)
        links.append({
            'url': url,
            'text': '',
            'title': '',
            'context': plain[start:end],
            'source': 'text_url',
        })
        seen.add(url)

    return links


def _best_links_for_file(all_links: List[Dict[str, Any]], file_name: str) -> Dict[str, Any]:
    scored = []
    for link in all_links:
        score = _link_score_for_file(link, file_name)
        if score > 0:
            scored.append({**link, 'score': score})

    scored.sort(key=lambda x: x.get('score', 0), reverse=True)

    download_url = ''
    file_page_url = ''

    for link in scored:
        url = link.get('url') or ''
        lowered = url.lower()
        joined = ' '.join([
            lowered,
            normalize_text(link.get('text') or '').lower(),
            normalize_text(link.get('context') or '').lower(),
        ])

        if not download_url and any(w in joined for w in ['download', 'baixar', 'arquivo', 'document', 'file']):
            download_url = url

        if not file_page_url and any(w in lowered for w in AUTODOC_DOMAIN_HINTS):
            file_page_url = url

        if download_url and file_page_url:
            break

    if not file_page_url and scored:
        file_page_url = scored[0].get('url') or ''

    if not download_url and scored and scored[0].get('score', 0) >= 80:
        download_url = scored[0].get('url') or ''

    return {
        'links': scored[:10],
        'download_url': download_url,
        'file_page_url': file_page_url,
    }


def _merge_file_links(file_data: Dict[str, Any], all_links: List[Dict[str, Any]]) -> Dict[str, Any]:
    picked = _best_links_for_file(all_links, file_data.get('file_name') or '')
    return {
        **file_data,
        'links': picked.get('links') or [],
        'download_url': picked.get('download_url') or '',
        'file_page_url': picked.get('file_page_url') or '',
        'autodoc_urls': [x.get('url') for x in picked.get('links') or [] if x.get('url')],
    }


def parse_email(subject: str = '', html: str = '', text: str = '', attachments: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    attachments = attachments or []
    soup = BeautifulSoup(html or '', 'lxml')
    plain = text or soup.get_text('\n', strip=True)
    email_links = _extract_links_from_html(html, plain)
    project = _extract_project(subject, plain)
    files: List[Dict[str, Any]] = []

    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        if not rows:
            continue

        headers = [_cell_text(c) for c in rows[0].find_all(['th', 'td'])]
        headers_norm = [normalize_text(h) for h in headers]

        for row in rows[1:]:
            cells = [_cell_text(c) for c in row.find_all(['td', 'th'])]
            if len(cells) < 1:
                continue

            d = {headers_norm[i] if i < len(headers_norm) else f'COL_{i}': cells[i] for i in range(len(cells))}
            joined = ' | '.join(cells)
            m = FILE_RE.search(joined)
            if not m:
                continue

            file_name = m.group(1).strip()
            autodoc_path = _first_present(d, ['Pasta', 'Caminho', 'Folder', 'Path', 'Local'])
            discipline = _first_present(d, ['Disciplina', 'Discipline', 'Especialidade'])
            if not discipline:
                discipline = _guess_discipline(file_name, autodoc_path, joined)

            status = _first_present(d, STATUS_LABELS)

            files.append(_merge_file_links({
                'file_name': file_name,
                'extension': ext_of(file_name),
                'project_detected': project,
                'discipline_detected': discipline,
                'autodoc_path': autodoc_path,
                'autodoc_path_safe': make_autodoc_path_safe(autodoc_path),
                'title': _extract_title(d, joined),
                'file_status_autodoc': status,
                'autodoc_user': _extract_autodoc_user(d),
                'autodoc_datetime': _extract_autodoc_datetime(d),
                'source': 'html_table',
                'raw': d,
            }, email_links))

    known = {f['file_name'].lower() for f in files}

    for m in FILE_RE.finditer(plain or ''):
        fn = m.group(1).strip()
        if fn.lower() not in known:
            discipline = _guess_discipline(fn, '', plain[max(0, m.start() - 250):m.end() + 250])
            files.append(_merge_file_links({
                'file_name': fn,
                'extension': ext_of(fn),
                'project_detected': project,
                'discipline_detected': discipline,
                'autodoc_path': '',
                'autodoc_path_safe': '',
                'title': '',
                'file_status_autodoc': '',
                'autodoc_user': '',
                'autodoc_datetime': '',
                'source': 'text_regex',
                'raw': {},
            }, email_links))
            known.add(fn.lower())

    for a in attachments:
        fn = a.get('fileName') or a.get('filename') or a.get('name')
        if fn and fn.lower() not in known:
            files.append(_merge_file_links({
                'file_name': fn,
                'extension': ext_of(fn),
                'project_detected': project,
                'discipline_detected': _guess_discipline(fn),
                'autodoc_path': '',
                'autodoc_path_safe': '',
                'title': a.get('title') or '',
                'file_status_autodoc': '',
                'autodoc_user': '',
                'autodoc_datetime': '',
                'source': 'attachment',
                'raw': a,
                'has_attachment': True,
            }, email_links))
            known.add(fn.lower())

    return {
        'email_type': detect_email_type(subject, plain),
        'project': project,
        'files': files,
        'files_count': len(files),
        'links': email_links[:50],
        'raw_text': plain[:10000],
    }
