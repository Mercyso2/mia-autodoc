from bs4 import BeautifulSoup
import re
from typing import Any, Dict, List
from .normalizer import ext_of, normalize_text

FILE_RE = re.compile(r'([A-Za-z0-9_\-\.\s]+\.(?:pdf|dwg|docx?|xlsx?|zip|rvt|ifc|png|jpg|jpeg))', re.I)

def detect_email_type(subject: str, text: str='') -> str:
    s = normalize_text((subject or '') + ' ' + (text or '')[:1000])
    if 'RESUMO INSTANTANEO' in s and 'UPLOAD' in s: return 'RESUMO_INSTANTANEO_UPLOAD'
    if 'RELATORIO DIARIO' in s and 'UPLOAD' in s: return 'RELATORIO_DIARIO_UPLOAD'
    if 'RELATORIO DIARIO' in s and 'DOWNLOAD' in s: return 'RELATORIO_DIARIO_DOWNLOAD'
    if 'RVE' in s: return 'STATUS_RVE'
    if 'SHAREPOINT' in s: return 'EMAIL_SHAREPOINT'
    return 'EMAIL_DESCONHECIDO'

def _cell_text(td) -> str:
    return ' '.join(td.get_text(' ', strip=True).split())

def _extract_project(subject: str, text: str) -> str:
    candidates = []
    for label in ['Projeto', 'Obra', 'Empreendimento']:
        m = re.search(label + r'\s*[:\-]\s*(.+)', text or '', re.I)
        if m: candidates.append(m.group(1).split('\n')[0].strip())
    m = re.search(r'\b(?:SAE|CASAINC|GRAAL|TENDA|ADMINISTRATIVO|COMERCIAL)\s*[-–]\s*[^\n\r\|]+', subject or '', re.I)
    if m: candidates.append(m.group(0).strip())
    return candidates[0] if candidates else ''

def parse_email(subject: str='', html: str='', text: str='', attachments: List[Dict[str,Any]] | None=None) -> Dict[str, Any]:
    attachments = attachments or []
    soup = BeautifulSoup(html or '', 'lxml')
    plain = text or soup.get_text('\n', strip=True)
    project = _extract_project(subject, plain)
    files: List[Dict[str,Any]] = []

    # Tabelas HTML do Autodoc
    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        if not rows: continue
        headers = [_cell_text(c) for c in rows[0].find_all(['th','td'])]
        headers_norm = [normalize_text(h) for h in headers]
        for row in rows[1:]:
            cells = [_cell_text(c) for c in row.find_all(['td','th'])]
            if len(cells) < 1: continue
            d = {headers_norm[i] if i < len(headers_norm) else f'COL_{i}': cells[i] for i in range(len(cells))}
            joined = ' | '.join(cells)
            m = FILE_RE.search(joined)
            if not m: continue
            file_name = m.group(1).strip()
            discipline = d.get('DISCIPLINA') or d.get('DISCIPLINE') or ''
            autodoc_path = d.get('PASTA') or d.get('CAMINHO') or d.get('FOLDER') or ''
            if not discipline and autodoc_path:
                parts = [p for p in re.split(r'[\\/]', autodoc_path) if p.strip()]
                discipline = parts[1] if len(parts) > 1 else (parts[0] if parts else '')
            files.append({'file_name': file_name, 'extension': ext_of(file_name), 'project_detected': project, 'discipline_detected': discipline, 'autodoc_path': autodoc_path, 'source': 'html_table', 'raw': d})

    # Arquivos encontrados no texto, se a tabela falhar
    known = {f['file_name'].lower() for f in files}
    for m in FILE_RE.finditer(plain or ''):
        fn = m.group(1).strip()
        if fn.lower() not in known:
            files.append({'file_name': fn, 'extension': ext_of(fn), 'project_detected': project, 'discipline_detected': '', 'autodoc_path': '', 'source': 'text_regex', 'raw': {}})
            known.add(fn.lower())

    # Anexos do Outlook/n8n
    for a in attachments:
        fn = a.get('fileName') or a.get('filename') or a.get('name')
        if fn and fn.lower() not in known:
            files.append({'file_name': fn, 'extension': ext_of(fn), 'project_detected': project, 'discipline_detected': '', 'autodoc_path': '', 'source': 'attachment', 'raw': a, 'has_attachment': True})

    return {'email_type': detect_email_type(subject, plain), 'project': project, 'files': files, 'raw_text': plain[:10000]}
