from pathlib import Path
from core.config import settings

class AutodocRobot:
    def download_file(self, project: str, autodoc_path: str, file_name: str) -> dict:
        # Estrutura base. Os seletores reais do Autodoc serão ajustados no .env após teste visual.
        from playwright.sync_api import sync_playwright
        download_dir = Path('storage/downloads'); download_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(settings.autodoc_profile_dir, headless=settings.autodoc_headless, accept_downloads=True)
            page = ctx.new_page()
            page.goto(settings.autodoc_url)
            page.wait_for_load_state('networkidle', timeout=60000)
            if settings.autodoc_search_selector:
                try:
                    page.fill(settings.autodoc_search_selector, file_name, timeout=10000)
                    page.keyboard.press('Enter')
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
            selectors = [s.strip() for s in settings.autodoc_download_selector.split(',') if s.strip()]
            last_err = None
            for sel in selectors:
                try:
                    with page.expect_download(timeout=30000) as d:
                        page.locator(sel).first.click(timeout=10000)
                    download = d.value
                    final = download_dir / file_name
                    download.save_as(str(final))
                    ctx.close()
                    return {'ok': True, 'local_path': str(final), 'status': 'BAIXADO'}
                except Exception as e:
                    last_err = e
            ctx.close()
            raise RuntimeError(f'Não conseguiu baixar no Autodoc. Ajuste seletores. Último erro: {last_err}')
