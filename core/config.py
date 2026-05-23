from functools import lru_cache
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'autodoc_center'
    app_env: str = 'HML'
    allow_production_upload: bool = False
    api_host: str = '0.0.0.0'
    api_port: int = 8000
    api_base_url: str = 'http://localhost:8000'

    supabase_url: str = ''
    supabase_service_key: str = ''

    microsoft_tenant_id: str = ''
    microsoft_client_id: str = ''
    microsoft_client_secret: str = ''

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

    @property
    def is_hml(self) -> bool:
        return self.app_env.upper() == 'HML'

    @property
    def is_prod_enabled(self) -> bool:
        return self.app_env.upper() == 'PROD' and self.allow_production_upload

    def assert_not_prod_unless_allowed(self) -> None:
        if self.app_env.upper() == 'PROD' and not self.allow_production_upload:
            raise RuntimeError('Produção bloqueada: defina ALLOW_PRODUCTION_UPLOAD=true somente após validação HML.')

@lru_cache
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
Path('logs').mkdir(exist_ok=True)
Path('storage/downloads').mkdir(parents=True, exist_ok=True)
Path('storage/temp').mkdir(parents=True, exist_ok=True)
Path('storage/hml').mkdir(parents=True, exist_ok=True)
