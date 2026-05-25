# Fase 3 — Microsoft Graph e SharePoint estáveis

Este patch corrige o ponto crítico de produção: **token Microsoft Graph renovável**.

## Arquivos do patch

```txt
core/config.py
services/graph_auth.py
services/sharepoint_service.py
.env.example
.env.production.example
```

## O que muda

- Cria `services/graph_auth.py`.
- Guarda `expires_at` do token Graph.
- Renova o token antes de expirar.
- Se o Graph retornar `401`, força refresh e tenta mais uma vez.
- Mantém compatibilidade com o código atual: `sharepoint.token()`, `sharepoint.get()`, `sharepoint.post()`, `sharepoint.put_bytes()` e `sharepoint.patch()` continuam existindo.

## Variáveis obrigatórias

No `.env`, confirme:

```env
MICROSOFT_TENANT_ID=
MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET=
MS_GRAPH_SCOPE=https://graph.microsoft.com/.default
GRAPH_TOKEN_REFRESH_MARGIN_SECONDS=300
GRAPH_TIMEOUT_SECONDS=60
SHAREPOINT_DEFAULT_LIBRARY_NAME=Documentos
SHAREPOINT_HML_ROOT=_AUTODOC_HOMOLOGACAO
```

## Testes

Com a API rodando:

```powershell
uvicorn api:app --reload --port 8000
```

Teste:

```powershell
curl.exe -i "http://localhost:8000/sharepoint/health"
```

Resultado esperado:

```txt
HTTP/1.1 200 OK
```

E JSON parecido com:

```json
{
  "ok": true,
  "token_configured": true,
  "status_code": 200,
  "mode": "multi-site"
}
```

Depois teste os fluxos n8n:

```txt
12 - Cadastrar Sites SharePoint
13 - Sincronizar Pastas SharePoint
14 - Preparar Site SharePoint HML
```

## Se der erro 403

Token está funcionando, mas faltam permissões no App Registration.
Confirme permissões Application no Microsoft Graph e admin consent:

```txt
Sites.ReadWrite.All
Files.ReadWrite.All
```

Para descoberta de sites, se necessário:

```txt
Sites.Read.All
```

## Se der erro 401

Verifique:

```txt
MICROSOFT_TENANT_ID
MICROSOFT_CLIENT_ID
MICROSOFT_CLIENT_SECRET
```

Gere um novo segredo se o atual expirou.
