from core.config import settings

def check_logged_in() -> dict:
    # Validação real ocorre no autodoc_robot; aqui mantemos contrato para API/n8n.
    return {'ok': True, 'status': 'UNKNOWN', 'message': 'Use /autodoc/login/open para abrir sessão visível e validar seletores.'}

def open_login_browser() -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(settings.autodoc_profile_dir, headless=settings.autodoc_headless)
        page = browser.new_page()
        page.goto(settings.autodoc_url or 'about:blank')
        page.wait_for_timeout(3000)
        # Mantém perfil salvo; fecha para não travar o endpoint.
        browser.close()
    return {'ok': True, 'status': 'OPENED'}
