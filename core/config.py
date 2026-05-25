from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'autodoc_center'
    app_env: str = 'HML'
    allow_production_upload: bool = False
    api_host: str = '0.0.0.0'
    api_port: int = 8000
    api_base_url: str = 'http://localhost:8000'

    # Segurança API/n8n/painel
    autodoc_api_key: str = ''
    require_api_key: bool = True
    cors_allow_origins: str = '*'
    prod_allowed_projects: str = ''
    prod_min_auto_score: int = 95
    n8n_fallback_is_success: bool = False
    require_file_id_for_pipeline: bool = True
    require_login_for_autodoc_download: bool = True
    require_local_path_for_upload: bool = True

    supabase_url: str = ''
    supabase_service_key: str = ''

    # Microsoft Graph / SharePoint
    microsoft_tenant_id: str = ''
    microsoft_client_id: str = ''
    microsoft_client_secret: str = ''
    ms_graph_scope: str = 'https://graph.microsoft.com/.default'
    graph_token_refresh_margin_seconds: int = 300
    graph_timeout_seconds: int = 60

    sharepoint_hostname: str = ''
    sharepoint_default_library_name: str = 'Documentos'
    sharepoint_hml_root: str = '_AUTODOC_HOMOLOGACAO'
    sharepoint_prod_root: str = ''
    sharepoint_obsolete_folder: str = '_OBSOLETOS'
    sharepoint_pending_folder: str = '_PENDENTES'
    sharepoint_error_folder: str = '_ERROS'
    sharepoint_site_id: str = ''
    sharepoint_drive_id: str = ''

    autodoc_url: str = ''
    autodoc_login_url: str = ''
    autodoc_user: str = ''
    autodoc_password: str = ''
    autodoc_profile_dir: str = 'storage/browser_profile'
    autodoc_headless: bool = False
    autodoc_search_selector: str = "input[type='search']"
    autodoc_download_selector: str = "button:has-text('Download'), a:has-text('Download'), button:has-text('Baixar'), a:has-text('Baixar')"
    autodoc_login_success_text: str = 'Logout,Sair,Projetos'

    n8n_outlook_folder: str = 'Autodoc'
    n8n_api_base_url: str = 'http://localhost:8000'
    min_auto_score: int = 90
    min_review_score: int = 70
    enable_sharepoint_upload: bool = True
    enable_autodoc_robot: bool = True
    enable_obsolete_rule: bool = True
    enable_n8n_automation: bool = True

    @field_validator('app_env')
    @classmethod
    def validate_app_env(cls, value: str) -> str:
        env = (value or 'HML').upper().strip()
        allowed = {'LOCAL', 'DRY_RUN', 'HML', 'PROD'}
        if env not in allowed:
            raise ValueError(f'APP_ENV inválido: {value}. Use LOCAL, DRY_RUN, HML ou PROD.')
        return env

    @property
    def is_hml(self) -> bool:
        return self.app_env.upper() == 'HML'

    @property
    def is_dry_run(self) -> bool:
        return self.app_env.upper() in {'LOCAL', 'DRY_RUN'}

    @property
    def is_prod(self) -> bool:
        return self.app_env.upper() == 'PROD'

    @property
    def is_prod_enabled(self) -> bool:
        return self.is_prod and self.allow_production_upload

    @property
    def cors_origins_list(self) -> List[str]:
        raw = (self.cors_allow_origins or '*').strip()
        if raw == '*':
            return ['*']
        return [x.strip() for x in raw.split(',') if x.strip()]

    @property
    def prod_allowed_projects_list(self) -> List[str]:
        return [x.strip().upper() for x in (self.prod_allowed_projects or '').split(',') if x.strip()]

    def assert_api_key_configured(self) -> None:
        if self.require_api_key and not self.autodoc_api_key:
            raise RuntimeError('REQUIRE_API_KEY=true, mas AUTODOC_API_KEY não foi configurada.')

    def assert_graph_configured(self) -> None:
        missing = []
        if not self.microsoft_tenant_id:
            missing.append('MICROSOFT_TENANT_ID')
        if not self.microsoft_client_id:
            missing.append('MICROSOFT_CLIENT_ID')
        if not self.microsoft_client_secret:
            missing.append('MICROSOFT_CLIENT_SECRET')
        if missing:
            raise RuntimeError('Credenciais Microsoft Graph incompletas: ' + ', '.join(missing))

    def assert_not_prod_unless_allowed(self) -> None:
        if self.is_prod and not self.allow_production_upload:
            raise RuntimeError('Produção bloqueada: defina ALLOW_PRODUCTION_UPLOAD=true somente após validação HML.')

    def assert_production_upload_allowed(self, project_name: str = '') -> None:
        """Trava dupla para qualquer upload em produção."""
        self.assert_not_prod_unless_allowed()
        if not self.is_prod_enabled:
            raise RuntimeError('Upload em produção bloqueado: APP_ENV precisa ser PROD e ALLOW_PRODUCTION_UPLOAD=true.')
        allowed_projects = self.prod_allowed_projects_list
        if allowed_projects:
            normalized_project = (project_name or '').upper().strip()
            if normalized_project not in allowed_projects:
                raise RuntimeError(f'Projeto não liberado para produção assistida: {project_name}')


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
Path('logs').mkdir(exist_ok=True)
Path('storage/downloads').mkdir(parents=True, exist_ok=True)
Path('storage/temp').mkdir(parents=True, exist_ok=True)
Path('storage/hml').mkdir(parents=True, exist_ok=True)
