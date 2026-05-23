import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path('logs')
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger('autodoc_center')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fmt = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s')
    file_handler = RotatingFileHandler(LOG_DIR / 'autodoc_center.log', maxBytes=5_000_000, backupCount=5, encoding='utf-8')
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
