from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from core.config import settings


class AutodocRobot:
    """
    Robô AutoDoc — Fase 6.5

    Responsável por:
    1. Abrir perfil persistente do Chrome/Playwright.
    2. Garantir login.
    3. Se cair em "Meus Produtos", entrar em "PROJETOS".
    4. Buscar o arquivo.
    5. Clicar no botão/link de download.
    """

    def __init__(self) -> None:
        self.download_dir = Path("storage/downloads")
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.keep_open_on_error = str(os.getenv("AUTODOC_KEEP_OPEN_ON_ERROR", "false")).lower() in {
            "1",
            "true",
            "yes",
            "sim",
            "on",
        }

    # =========================================================
    # Helpers
    # =========================================================

    def _login_url(self) -> str:
        url = (getattr(settings, "autodoc_login_url", "") or getattr(settings, "autodoc_url", "") or "").strip()
        if not url:
            raise RuntimeError("AUTODOC_LOGIN_URL/AUTODOC_URL ausente no .env")
        return url

    def _wait(self, page, ms: int = 1000) -> None:
        page.wait_for_timeout(ms)

    def _safe_text(self, page) -> str:
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    def _contains_any(self, text: str, values: Iterable[str]) -> bool:
        hay = (text or "").lower()
        return any((v or "").strip().lower() in hay for v in values if (v or "").strip())

    def _click_first_visible(self, page, selectors: Iterable[str], timeout: int = 5000) -> bool:
        last_err = None

        for selector in selectors:
            try:
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.click(timeout=timeout)
                return True
            except Exception as exc:
                last_err = exc

        return False

    def _fill_first_visible(self, page, selectors: Iterable[str], value: str, timeout: int = 5000) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.fill(value, timeout=timeout)
                return True
            except Exception:
                pass

        return False

    # =========================================================
    # Login / navegação inicial
    # =========================================================

    def _is_logged_in(self, page) -> bool:
        text = self._safe_text(page)

        success_tokens = []
        raw_success = getattr(settings, "autodoc_login_success_text", "") or ""
        success_tokens.extend([x.strip() for x in raw_success.split(",") if x.strip()])
        success_tokens.extend(["Meus Produtos", "Projetos", "Sair", "Logout"])

        return self._contains_any(text, success_tokens)

    def _perform_login_if_needed(self, page) -> None:
        """
        Login em duas etapas:
        - e-mail + Continuar
        - senha + Entrar/Continuar
        Também aceita sessão já aberta via perfil persistente.
        """
        page.goto(self._login_url(), wait_until="load", timeout=60000)
        self._wait(page, 1500)

        if self._is_logged_in(page):
            return

        user = (getattr(settings, "autodoc_user", "") or "").strip()
        password = (getattr(settings, "autodoc_password", "") or "").strip()

        if not user:
            raise RuntimeError("AUTODOC_USER ausente no .env")

        email_filled = self._fill_first_visible(
            page,
            [
                "input[type='email']",
                "input[name='email']",
                "input[autocomplete='username']",
                "input[placeholder*='email' i]",
                "input[placeholder*='e-mail' i]",
                "input",
            ],
            user,
            timeout=10000,
        )

        if email_filled:
            clicked = self._click_first_visible(
                page,
                [
                    "button:has-text('Continuar')",
                    "button:has-text('Avançar')",
                    "button:has-text('Próximo')",
                    "button:has-text('Entrar')",
                    "input[type='submit']",
                ],
                timeout=6000,
            )

            if not clicked:
                page.keyboard.press("Enter")

            self._wait(page, 2500)

        if self._is_logged_in(page):
            return

        if not password:
            raise RuntimeError("AUTODOC_PASSWORD ausente no .env ou login manual necessário")

        password_filled = self._fill_first_visible(
            page,
            [
                "input[type='password']",
                "input[name='password']",
                "input[autocomplete='current-password']",
                "input[placeholder*='senha' i]",
            ],
            password,
            timeout=15000,
        )

        if not password_filled:
            raise RuntimeError("Campo de senha não encontrado na tela de login AutoDoc")

        clicked = self._click_first_visible(
            page,
            [
                "button:has-text('Entrar')",
                "button:has-text('Continuar')",
                "button:has-text('Acessar')",
                "input[type='submit']",
            ],
            timeout=8000,
        )

        if not clicked:
            page.keyboard.press("Enter")

        # Aguarda redirecionamento para produtos/projetos.
        for _ in range(30):
            self._wait(page, 1000)
            if self._is_logged_in(page):
                return

        raise RuntimeError("Login AutoDoc não confirmado após preencher credenciais")

    def _enter_projects_product_if_needed(self, page) -> None:
        """
        Quando o login cai na tela 'Meus Produtos', entra no card 'PROJETOS'.
        """
        text = self._safe_text(page)

        if not self._contains_any(text, ["Meus Produtos", "GD4", "Diário de Obras", "PROJETOS"]):
            return

        # Primeiro tenta clicar no link/botão Acessar dentro de um card que contém PROJETOS.
        try:
            card = page.locator("div, section, article").filter(has_text="PROJETOS").first
            card.locator("text=Acessar").first.click(timeout=10000)
            page.wait_for_load_state("load", timeout=60000)
            self._wait(page, 3000)
            return
        except Exception:
            pass

        # Fallback: como normalmente há 3 "Acessar", o terceiro é Projetos.
        try:
            page.get_by_text("Acessar", exact=True).nth(2).click(timeout=10000)
            page.wait_for_load_state("load", timeout=60000)
            self._wait(page, 3000)
            return
        except Exception:
            pass

        # Fallback final: clicar no texto PROJETOS ou no primeiro elemento relacionado.
        try:
            page.get_by_text("PROJETOS", exact=False).first.click(timeout=10000)
            page.wait_for_load_state("load", timeout=60000)
            self._wait(page, 3000)
        except Exception as exc:
            raise RuntimeError("Não consegui acessar o produto PROJETOS na tela Meus Produtos") from exc

    # =========================================================
    # Busca e download
    # =========================================================

    def _search_file(self, page, project: str, autodoc_path: str, file_name: str) -> None:
        """
        Busca genérica. Primeiro pelo nome do arquivo.
        Se não encontrar campo configurado, tenta seletores comuns.
        """
        search_selectors = []

        configured = (getattr(settings, "autodoc_search_selector", "") or "").strip()
        if configured:
            search_selectors.append(configured)

        search_selectors.extend(
            [
                "input[type='search']",
                "input[placeholder*='buscar' i]",
                "input[placeholder*='pesquisar' i]",
                "input[placeholder*='search' i]",
                "input",
            ]
        )

        filled = self._fill_first_visible(page, search_selectors, file_name, timeout=15000)

        if not filled:
            raise RuntimeError("Campo de busca não encontrado após entrar no AutoDoc Projetos")

        page.keyboard.press("Enter")
        self._wait(page, 5000)

    def _download_current_result(self, page, file_name: str) -> Path:
        selectors = []

        configured = (getattr(settings, "autodoc_download_selector", "") or "").strip()
        if configured:
            selectors.extend([s.strip() for s in configured.split(",") if s.strip()])

        selectors.extend(
            [
                "button:has-text('Download')",
                "a:has-text('Download')",
                "button:has-text('Baixar')",
                "a:has-text('Baixar')",
                "[title*='Download' i]",
                "[aria-label*='Download' i]",
                "[title*='Baixar' i]",
                "[aria-label*='Baixar' i]",
            ]
        )

        last_err = None

        for selector in selectors:
            try:
                with page.expect_download(timeout=45000) as download_info:
                    page.locator(selector).first.click(timeout=15000)

                download = download_info.value
                final = self.download_dir / file_name
                download.save_as(str(final))
                return final
            except Exception as exc:
                last_err = exc

        raise RuntimeError(f"Não conseguiu baixar no AutoDoc. Ajuste seletores. Último erro: {last_err}")

    def download_file(self, project: str, autodoc_path: str, file_name: str) -> dict:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(settings.autodoc_profile_dir),
                headless=settings.autodoc_headless,
                accept_downloads=True,
            )

            page = ctx.new_page()

            try:
                self._perform_login_if_needed(page)
                self._enter_projects_product_if_needed(page)

                page.wait_for_load_state("load", timeout=60000)
                self._wait(page, 2000)

                self._search_file(page, project, autodoc_path, file_name)
                final = self._download_current_result(page, file_name)

                ctx.close()

                return {
                    "ok": True,
                    "local_path": str(final),
                    "status": "BAIXADO",
                    "project": project,
                    "autodoc_path": autodoc_path,
                    "file_name": file_name,
                }

            except Exception:
                if self.keep_open_on_error and not settings.autodoc_headless:
                    print("AUTODOC_KEEP_OPEN_ON_ERROR=true: mantendo navegador aberto por 120s para inspeção...")
                    page.wait_for_timeout(120000)

                ctx.close()
                raise
