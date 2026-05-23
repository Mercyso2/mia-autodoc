import re, unicodedata
from slugify import slugify

def normalize_text(value: str | None) -> str:
    if not value:
        return ''
    s = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode('ascii')
    s = s.upper().strip()
    s = re.sub(r'[^A-Z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()

def safe_path_part(value: str | None) -> str:
    if not value:
        return ''
    s = str(value).strip().replace('\\','/').replace('/','-')
    s = re.sub(r'[<>:"|?*]', '-', s)
    return re.sub(r'\s+', ' ', s).strip().strip('.')

def ext_of(file_name: str) -> str:
    return (file_name.rsplit('.',1)[1].lower() if file_name and '.' in file_name else '')
