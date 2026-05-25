from fastapi import Header, HTTPException, status

from .config import settings


def require_api_key(x_autodoc_api_key: str = Header(default='')) -> bool:
    """Protege endpoints críticos chamados por n8n, painel ou operadores.

    Envie o header:
      X-Autodoc-Api-Key: <AUTODOC_API_KEY>
    """
    try:
        settings.assert_api_key_configured()
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    if not settings.require_api_key:
        return True

    if not x_autodoc_api_key or x_autodoc_api_key != settings.autodoc_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='AUTODOC_API_KEY ausente ou inválida.')

    return True


def security_status() -> dict:
    return {
        'require_api_key': settings.require_api_key,
        'api_key_configured': bool(settings.autodoc_api_key),
        'environment': settings.app_env,
        'production_enabled': settings.is_prod_enabled,
        'allow_production_upload': settings.allow_production_upload,
        'prod_allowed_projects': settings.prod_allowed_projects_list,
        'n8n_fallback_is_success': settings.n8n_fallback_is_success,
        'require_file_id_for_pipeline': settings.require_file_id_for_pipeline,
        'require_login_for_autodoc_download': settings.require_login_for_autodoc_download,
        'require_local_path_for_upload': settings.require_local_path_for_upload,
    }
