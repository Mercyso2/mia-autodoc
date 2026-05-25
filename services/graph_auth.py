from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

from core.config import settings

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


class GraphAuthError(RuntimeError):
    """Erro de autenticação/autorização no Microsoft Graph."""


class GraphRequestError(RuntimeError):
    """Erro de requisição ao Microsoft Graph."""


@dataclass
class GraphToken:
    access_token: str
    expires_at: float
    token_type: str = "Bearer"

    def is_valid(self, margin_seconds: int = 300) -> bool:
        return bool(self.access_token) and time.time() < (self.expires_at - margin_seconds)


class GraphAuthClient:
    """Cliente centralizado para token e chamadas Microsoft Graph.

    Resolve o problema de token expirado:
    - guarda expires_at;
    - renova antes de vencer;
    - em 401, força refresh e tenta mais uma vez.
    """

    def __init__(self) -> None:
        self._token: Optional[GraphToken] = None

    @property
    def scope(self) -> str:
        return getattr(settings, "ms_graph_scope", "https://graph.microsoft.com/.default") or "https://graph.microsoft.com/.default"

    @property
    def timeout(self) -> int:
        return int(getattr(settings, "graph_timeout_seconds", 60) or 60)

    @property
    def refresh_margin_seconds(self) -> int:
        return int(getattr(settings, "graph_token_refresh_margin_seconds", 300) or 300)

    def _validate_config(self) -> None:
        missing = []
        if not getattr(settings, "microsoft_tenant_id", ""):
            missing.append("MICROSOFT_TENANT_ID")
        if not getattr(settings, "microsoft_client_id", ""):
            missing.append("MICROSOFT_CLIENT_ID")
        if not getattr(settings, "microsoft_client_secret", ""):
            missing.append("MICROSOFT_CLIENT_SECRET")
        if missing:
            raise GraphAuthError("Credenciais Microsoft Graph incompletas no .env: " + ", ".join(missing))

    def get_graph_token(self, force_refresh: bool = False) -> str:
        self._validate_config()

        if not force_refresh and self._token and self._token.is_valid(self.refresh_margin_seconds):
            return self._token.access_token

        url = TOKEN_URL_TEMPLATE.format(tenant=settings.microsoft_tenant_id)
        data = {
            "client_id": settings.microsoft_client_id,
            "client_secret": settings.microsoft_client_secret,
            "scope": self.scope,
            "grant_type": "client_credentials",
        }

        try:
            response = requests.post(url, data=data, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GraphAuthError(f"Falha de conexão ao obter token Graph: {exc}") from exc

        if response.status_code >= 400:
            raise GraphAuthError(f"Erro token Graph {response.status_code}: {response.text}")

        payload = response.json()
        access_token = payload.get("access_token")
        if not access_token:
            raise GraphAuthError(f"Token Graph sem access_token. Resposta: {payload}")

        expires_in = int(payload.get("expires_in") or 3600)
        token_type = payload.get("token_type") or "Bearer"
        self._token = GraphToken(
            access_token=access_token,
            expires_at=time.time() + expires_in,
            token_type=token_type,
        )
        return self._token.access_token

    def headers(self, force_refresh: bool = False, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Authorization": f"Bearer {self.get_graph_token(force_refresh=force_refresh)}"}
        if extra:
            headers.update(extra)
        return headers

    def graph_request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        data: Any = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[int] = None,
        retry_on_401: bool = True,
    ) -> requests.Response:
        """Executa chamada Graph com retry automático em 401.

        `url` pode ser uma URL completa ou um caminho começando por `/`.
        """
        if url.startswith("/"):
            url = GRAPH_BASE_URL + url

        request_timeout = timeout or self.timeout
        auth_headers = self.headers(extra=headers)

        try:
            response = requests.request(
                method.upper(),
                url,
                headers=auth_headers,
                json=json,
                data=data,
                timeout=request_timeout,
            )
        except requests.RequestException as exc:
            raise GraphRequestError(f"Falha de conexão Graph {method.upper()} {url}: {exc}") from exc

        if response.status_code == 401 and retry_on_401:
            # Token expirado ou invalidado. Renova e tenta uma vez.
            try:
                response = requests.request(
                    method.upper(),
                    url,
                    headers=self.headers(force_refresh=True, extra=headers),
                    json=json,
                    data=data,
                    timeout=request_timeout,
                )
            except requests.RequestException as exc:
                raise GraphRequestError(f"Falha de conexão Graph após refresh {method.upper()} {url}: {exc}") from exc

        return response

    def json_request(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        response = self.graph_request(method, url, **kwargs)
        if response.status_code >= 400:
            raise GraphRequestError(f"Graph {method.upper()} {response.status_code}: {response.text}")
        return response.json() if response.text else {}

    def health(self) -> Dict[str, Any]:
        """Valida token e uma chamada simples ao Graph.

        /organization é leve e confirma que o token app-only está válido.
        """
        token = self.get_graph_token(force_refresh=False)
        response = self.graph_request("GET", "/organization", retry_on_401=True)
        ok = response.status_code < 400
        result: Dict[str, Any] = {
            "ok": ok,
            "token_configured": bool(token),
            "status_code": response.status_code,
            "scope": self.scope,
        }
        if ok:
            body = response.json() if response.text else {}
            values = body.get("value") or []
            if values:
                org = values[0]
                result["tenant_display_name"] = org.get("displayName")
                result["tenant_id"] = org.get("id")
        else:
            result["error"] = response.text
        return result


_graph_client = GraphAuthClient()


def get_graph_token(force_refresh: bool = False) -> str:
    return _graph_client.get_graph_token(force_refresh=force_refresh)


def graph_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    return _graph_client.graph_request(method, url, **kwargs)


def graph_json(method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
    return _graph_client.json_request(method, url, **kwargs)


def graph_health() -> Dict[str, Any]:
    return _graph_client.health()
