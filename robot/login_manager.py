from core.config import settings


def check_logged_in() -> dict:
    """
    Validação leve. A validação real acontece no AutodocRobot durante o download.
    """
    return {
        "ok": True,
        "status": "UNKNOWN",
        "message": "Login é validado pelo worker/autodoc_robot durante o download.",
        "autodoc_url_configured": bool(settings.autodoc_url),
        "autodoc_login_url_configured": bool(settings.autodoc_login_url),
        "autodoc_user_configured": bool(settings.autodoc_user),
        "headless": settings.autodoc_headless,
    }


def open_login_browser() -> dict:
    from playwright.sync_api import sync_playwright

    url = settings.autodoc_login_url or settings.autodoc_url or "about:blank"

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            str(settings.autodoc_profile_dir),
            headless=settings.autodoc_headless,
        )
        page = browser.new_page()
        page.goto(url)
        page.wait_for_timeout(10000)
        browser.close()

    return {
        "ok": True,
        "status": "OPENED",
        "url": url,
    }
