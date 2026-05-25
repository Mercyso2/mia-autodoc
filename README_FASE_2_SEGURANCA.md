# Fase 2 — Segurança, variáveis e travas de produção

## Objetivo

Proteger endpoints críticos da API e impedir produção acidental.

## Arquivos alterados

- `core/config.py`
- `core/security.py`
- `api.py`
- `.env.example`
- `.env.production.example`
- `.gitignore`

## Variável obrigatória

Defina uma chave forte no `.env`:

```env
AUTODOC_API_KEY=cole-uma-chave-longa-aqui
REQUIRE_API_KEY=true
```

Sugestão para gerar no terminal:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Como chamar endpoints protegidos

Em todos os HTTP Request do n8n para endpoints críticos, adicione o header:

```txt
X-Autodoc-Api-Key: {{$env.AUTODOC_API_KEY}}
```

Ou cole a chave em uma credencial segura do n8n.

## Endpoints protegidos

- `POST /emails/ingest`
- `POST /emails/{email_id}/parse`
- `POST /emails/parse-latest`
- `POST /sharepoint/sites/discover`
- `POST /sharepoint/sites/{site_row_id}/sync-folders`
- `POST /sharepoint/sites/{site_row_id}/bootstrap-hml`
- `POST /sharepoint/sites/{site_row_id}/generate-folder-map`
- `POST /sharepoint/sites/{site_row_id}/prepare-hml`
- `POST /files/{file_id}/enrich-destination`
- `POST /files/{file_id}/approve`
- `POST /files/{file_id}/correct-path`
- `POST /files/{file_id}/ignore`
- `POST /files/{file_id}/upload-local`
- `POST /files/{file_id}/queue-robot`
- `POST /files/{file_id}/upload-hml`
- `POST /autodoc/login/check`
- `POST /autodoc/login/open`
- `POST /autodoc/download`

## Endpoints públicos de conferência

- `GET /health`
- `GET /security/health`
- `GET /db/health`
- `GET /sharepoint/health`

## Trava de produção

Produção real só deve ser liberada quando:

```env
APP_ENV=PROD
ALLOW_PRODUCTION_UPLOAD=true
```

Antes disso, mantenha:

```env
APP_ENV=HML
ALLOW_PRODUCTION_UPLOAD=false
```

## Testes rápidos

### 1. API viva

```bash
curl http://localhost:8000/health
```

### 2. Segurança configurada

```bash
curl http://localhost:8000/security/health
```

### 3. Endpoint protegido sem chave deve bloquear

```bash
curl -X POST http://localhost:8000/emails/parse-latest
```

Resultado esperado: `401`.

### 4. Endpoint protegido com chave deve passar

```bash
curl -X POST http://localhost:8000/emails/parse-latest \
  -H "X-Autodoc-Api-Key: SUA_CHAVE"
```

Resultado esperado: não pode retornar `401`. Pode retornar mensagem funcional, por exemplo nenhum e-mail pendente.
