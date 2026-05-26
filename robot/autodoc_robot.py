from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from core.config import settings


class AutodocRobot:
    """
    Robô AutoDoc — Fase 6.5 — Inteligência nível 5/100

    Estratégia:
    1. Login/sessão AutoDoc.
    2. Tentar link do e-mail primeiro:
       - parser_payload.download_url
       - parser_payload.file_page_url
       - parser_payload.links/autodoc_urls
    3. Se não houver link ou não baixar:
       - busca simples pelo nome exato do arquivo
       - tenta botão/link Download/Baixar
    4. Se parar em seleção de conta/projeto:
       - falha com erro claro para retry/manual, sem adivinhar conta.
    """

    def __init__(self) -> None:
        self.download_dir = Path("storage/downloads")
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.keep_open_on_error = str(os.getenv("AUTODOC_KEEP_OPEN_ON_ERROR", "false")).lower() in {
            "1", "true", "yes", "sim", "on"
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

    def _is_url(self, value: str) -> bool:
        try:
            parsed = urlparse((value or "").strip())
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        except Exception:
            return False

    def _click_first_visible(self, page, selectors: Iterable[str], timeout: int = 5000) -> bool:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.click(timeout=timeout)
                return True
            except Exception:
                pass
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
    # Context / links
    # =========================================================

    def _candidate_urls_from_context(self, context: Dict[str, Any]) -> List[str]:
        urls: List[str] = []

        def add(value: Any) -> None:
            if isinstance(value, str) and self._is_url(value) and value not in urls:
                urls.append(value)

        parser_payload = context.get("parser_payload") if isinstance(context.get("parser_payload"), dict) else {}
        raw_payload = context.get("raw_payload") if isinstance(context.get("raw_payload"), dict) else {}
        raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}

        for source in [context, parser_payload, raw_payload, raw]:
            add(source.get("download_url"))
            add(source.get("file_page_url"))
            add(source.get("link"))
            add(source.get("url"))

            # Suporta parser v2: ranked_links e download_plan por arquivo.
            for key in ["autodoc_urls", "links", "ranked_links"]:
                value = source.get(key)
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, str):
                            add(item)
                        elif isinstance(item, dict):
                            add(item.get("url"))

            dp = source.get("download_plan")
            if isinstance(dp, dict):
                for key in ["ranked_links", "links"]:
                    value = dp.get(key)
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, str):
                                add(item)
                            elif isinstance(item, dict):
                                add(item.get("url"))

        # Quando parser_payload guarda a inteligência completa, encontra o arquivo correspondente.
        if isinstance(parser_payload.get("files"), list):
            current_name = (context.get("file_name") or "").lower()
            for file_plan in parser_payload.get("files") or []:
                if not isinstance(file_plan, dict):
                    continue
                if current_name and (file_plan.get("file_name") or "").lower() != current_name:
                    continue
                add(file_plan.get("download_url"))
                add(file_plan.get("file_page_url"))
                for key in ["autodoc_urls", "links", "ranked_links"]:
                    value = file_plan.get(key)
                    if isinstance(value, list):
                        for item in value:
                            if isinstance(item, str):
                                add(item)
                            elif isinstance(item, dict):
                                add(item.get("url"))

        # Prioriza URLs com download/arquivo antes de páginas genéricas.
        def score(url: str) -> int:
            lowered = url.lower()
            s = 0
            if "download" in lowered or "baixar" in lowered:
                s += 100
            if "arquivo" in lowered or "document" in lowered or "file" in lowered:
                s += 50
            if "autodoc" in lowered:
                s += 25
            return s

        urls.sort(key=score, reverse=True)
        return urls[:10]

    # =========================================================
    # Login
    # =========================================================

    def _is_logged_in(self, page) -> bool:
        text = self._safe_text(page)
        success_tokens = []
        raw_success = getattr(settings, "autodoc_login_success_text", "") or ""
        success_tokens.extend([x.strip() for x in raw_success.split(",") if x.strip()])
        success_tokens.extend(["Meus Produtos", "Projetos", "Sair", "Logout", "Conta: Selecione"])
        return self._contains_any(text, success_tokens)

    def _perform_login_if_needed(self, page) -> None:
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

        for _ in range(30):
            self._wait(page, 1000)
            if self._is_logged_in(page):
                return

        raise RuntimeError("Login AutoDoc não confirmado após preencher credenciais")

    # =========================================================
    # Navegação fallback nível 5
    # =========================================================

    def _enter_projects_product_if_possible(self, page) -> None:
        text = self._safe_text(page)

        # Se estiver na tela de produto, clica em PROJETOS.
        if self._contains_any(text, ["Meus Produtos", "GD4", "Diário de Obras", "PROJETOS"]):
            try:
                card = page.locator("div, section, article").filter(has_text="PROJETOS").first
                card.locator("text=Acessar").first.click(timeout=10000)
                page.wait_for_load_state("load", timeout=60000)
                self._wait(page, 3000)
                return
            except Exception:
                pass

            try:
                page.get_by_text("Acessar", exact=True).nth(2).click(timeout=10000)
                page.wait_for_load_state("load", timeout=60000)
                self._wait(page, 3000)
                return
            except Exception:
                pass

        # Se já está em seleção de conta, nível 5 não adivinha a conta.
        text = self._safe_text(page)
        if self._contains_any(text, ["Conta: Selecione", "Selecione", "Digite para buscar"]):
            return

    def _search_file(self, page, file_name: str) -> None:
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
                "input[placeholder*='Digite para buscar' i]",
                "input",
            ]
        )

        filled = self._fill_first_visible(page, search_selectors, file_name, timeout=15000)

        if not filled:
            text = self._safe_text(page)
            if self._contains_any(text, ["Conta: Selecione", "Selecione uma conta"]):
                raise RuntimeError(
                    "CONTA_SELECIONE_NAO_SUPORTADO_NIVEL_5: AutoDoc pediu seleção de conta. "
                    "Use link do e-mail ou evoluir robô para varrer contas."
                )
            raise RuntimeError("Campo de busca não encontrado para pesquisar arquivo no AutoDoc")

        page.keyboard.press("Enter")
        self._wait(page, 5000)

    # =========================================================
    # Download
    # =========================================================

    def _download_current_result(self, page, file_name: str) -> Optional[Path]:
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

        return None

    def _try_download_from_url(self, page, url: str, file_name: str) -> Optional[Path]:
        try:
            with page.expect_download(timeout=20000) as download_info:
                page.goto(url, wait_until="load", timeout=60000)
            download = download_info.value
            final = self.download_dir / file_name
            download.save_as(str(final))
            return final
        except Exception:
            pass

        # Se a URL abriu página do arquivo, tenta botão Download.
        try:
            page.goto(url, wait_until="load", timeout=60000)
            self._wait(page, 3000)
            return self._download_current_result(page, file_name)
        except Exception:
            return None

    def download_file(self, project: str, autodoc_path: str, file_name: str, context: Optional[Dict[str, Any]] = None) -> dict:
        from playwright.sync_api import sync_playwright

        context = context or {}

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                str(settings.autodoc_profile_dir),
                headless=settings.autodoc_headless,
                accept_downloads=True,
            )
            page = ctx.new_page()

            try:
                self._perform_login_if_needed(page)

                # Cenário 1: link do e-mail.
                candidate_urls = self._candidate_urls_from_context(context)
                for url in candidate_urls:
                    final = self._try_download_from_url(page, url, file_name)
                    if final and final.exists() and final.stat().st_size > 0:
                        ctx.close()
                        return {
                            "ok": True,
                            "local_path": str(final),
                            "status": "BAIXADO",
                            "download_mode": "email_link",
                            "source_url": url,
                            "project": project,
                            "autodoc_path": autodoc_path,
                            "file_name": file_name,
                        }

                # Cenário 2: busca simples pelo nome.
                self._enter_projects_product_if_possible(page)
                page.wait_for_load_state("load", timeout=60000)
                self._wait(page, 2000)

                self._search_file(page, file_name)
                final = self._download_current_result(page, file_name)

                if not final:
                    raise RuntimeError(
                        "DOWNLOAD_NAO_ENCONTRADO_NIVEL_5: nenhum link direto funcionou e não achei botão Download/Baixar após busca simples."
                    )

                ctx.close()
                return {
                    "ok": True,
                    "local_path": str(final),
                    "status": "BAIXADO",
                    "download_mode": "manual_search_file_name",
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
