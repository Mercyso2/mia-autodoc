# Deploy MIA Autodoc V4.4 no EasyPanel

Objetivo: colocar a API Python e, opcionalmente, o painel Streamlit na internet para o n8n da VPS conseguir testar os fluxos reais.

## Decisão rápida

Para o n8n funcionar, o mínimo necessário é subir a **API**.

- API FastAPI: porta interna `8000`
- Painel Streamlit: porta interna `8501` (opcional)

## 1. Preparar o projeto local

Antes de enviar para GitHub/EasyPanel, confirme que NÃO vai junto:

- `.env`
- `.venv/`
- `storage/browser_profile/`
- `storage/downloads/`
- `storage/temp/`
- `logs/`

O arquivo `.dockerignore` deste patch já ignora esses itens.

## 2. Criar app da API no EasyPanel

Crie um novo app, por exemplo:

`mia-autodoc-api`

Configuração:

- Build: Dockerfile
- Dockerfile path: `Dockerfile.api`
- Porta interna: `8000`
- Health check: `/health`

Variáveis de ambiente: use o conteúdo de `.env.production.example`, preenchendo com seus dados reais.

Pontos importantes:

- `APP_ENV=HML`
- `ALLOW_PRODUCTION_UPLOAD=false`
- `AUTODOC_HEADLESS=true`
- `API_BASE_URL=https://URL-DA-API-GERADA-PELO-EASYPANEL`
- `N8N_API_BASE_URL=https://URL-DA-API-GERADA-PELO-EASYPANEL`

Depois do deploy, teste no navegador:

`https://URL-DA-API/health`

Resultado esperado:

```json
{
  "status": "ok",
  "service": "autodoc_center",
  "environment": "HML",
  "production_enabled": false,
  "multi_site_sharepoint": true
}
```

Teste também:

- `/db/health`
- `/sharepoint/health`

## 3. Atualizar n8n

No EasyPanel do n8n, adicione variável de ambiente:

`AUTODOC_API_URL=https://URL-DA-API-GERADA-PELO-EASYPANEL`

Depois reinicie o app do n8n.

Nos workflows, o `apiBaseUrl` deve usar:

`{{$env.AUTODOC_API_URL}}`

Assim todos os endpoints ficam:

- `{{$env.AUTODOC_API_URL}}/health`
- `{{$env.AUTODOC_API_URL}}/emails/ingest`
- `{{$env.AUTODOC_API_URL}}/emails/{id}/parse`
- `{{$env.AUTODOC_API_URL}}/files/{id}/upload-hml`

## 4. Criar app do painel Streamlit (opcional)

Crie outro app:

`mia-autodoc-panel`

Configuração:

- Dockerfile path: `Dockerfile.panel`
- Porta interna: `8501`

Use as mesmas variáveis de ambiente da API.

Depois acesse:

`https://URL-DO-PAINEL`

## 5. Volumes recomendados

Na API, se possível, crie volumes persistentes para:

- `/app/storage/downloads`
- `/app/storage/temp`
- `/app/storage/browser_profile`
- `/app/logs`

Isso evita perder downloads temporários, logs e sessão do robô quando reiniciar.

## 6. Ordem de testes depois do deploy

1. `GET /health`
2. `GET /db/health`
3. `GET /sharepoint/health`
4. n8n workflow `00 - Healthcheck API`
5. n8n workflow `01 - Capturar Email Teste`
6. n8n workflow `02 - Enviar Email para API`
7. n8n workflow `03 - Parser de Email`
8. n8n workflow `05 - Download de Anexos`
9. n8n workflow `10 - Fluxo Completo HML`

## 7. Segurança

Enquanto estiver em HML, mantenha:

`ALLOW_PRODUCTION_UPLOAD=false`

Quando a API estiver pública, a próxima melhoria recomendada é adicionar API Key nos endpoints chamados pelo n8n.
