# AUTODOC CENTER — Fase 4: Parser + file_id real

Este patch ajusta os 4 arquivos enviados:

- `api.py`
- `services/email_parser.py`
- `services/file_service.py`
- `core/database.py`

## Problemas encontrados

1. `parse_saved_email` fazia `upsert` em `autodoc_files` usando `on_conflict='email_id,file_name,autodoc_path_safe'`, mas o payload não garantia `autodoc_path_safe`.
2. Se não existir índice único no Supabase para esse conflito, o upsert pode falhar.
3. O retorno do parser não destacava claramente `file_id`, que é o identificador oficial da Fase 4.
4. O histórico era registrado, mas sem payload suficiente para auditoria.
5. O parser era básico e podia deixar disciplina/caminho/título/status do AutoDoc vazios mesmo quando existiam indícios no e-mail.
6. `database.insert_history` não aceitava `message`/`payload` de forma segura.
7. Upload HML não preenchia todos os campos de rastreabilidade adicionados na Fase 1.

## O que mudou

### API

- Gera `autodoc_path_safe`.
- Gera `source_hash`.
- Faz upsert manual por identidade:
  - `email_id`
  - `file_name`
  - `autodoc_path_safe`
- Retorna `file_id` real no parser.
- Atualiza `processing_started_at`, `processing_finished_at`, `parser_payload`, `error_message`.
- Registra histórico com `message` e `payload`.
- Registra erro com payload quando parser falha.

### Parser

- Melhorou regex de arquivos.
- Detecta projeto, disciplina, título, status, usuário e data/hora quando possível.
- Cria `autodoc_path_safe`.
- Suporta HTML table, texto e anexos.

### Database

- Adiciona `now_iso()`.
- `update_row` passa a preencher `updated_at`.
- `insert_history` aceita `message` e `payload`.
- `insert_error` aceita `source` e `severity`.

### File Service

- Atualiza `last_processed_at`.
- Preenche `sharepoint_final_path`.
- Preenche `upload_started_at` e `upload_finished_at`.
- Registra histórico mais completo.

## Como aplicar

Copie os arquivos para a raiz do projeto:

```txt
api.py
services/email_parser.py
services/file_service.py
core/database.py
```

Depois faça commit e redeploy:

```bash
git add .
git commit -m "fase 4 parser file id real"
git push
```

No EasyPanel, faça redeploy da API.

## Teste rápido

1. Suba a API local ou use a URL publicada.
2. Ingest:

```bash
curl.exe -X POST "https://SUA_API/emails/ingest" ^
  -H "Content-Type: application/json" ^
  -H "X-Autodoc-Api-Key: SUA_CHAVE" ^
  -d @tests/email_ingest_fase4_payload.json
```

3. Parse latest:

```bash
curl.exe -X POST "https://SUA_API/emails/parse-latest" ^
  -H "X-Autodoc-Api-Key: SUA_CHAVE"
```

## Resultado esperado

```json
{
  "ok": true,
  "email_id": "...",
  "files_created": 1,
  "files": [
    {
      "file_id": "...",
      "file_name": "...",
      "status": "...",
      "confidence_score": 90
    }
  ]
}
```

O campo mais importante é:

```txt
file_id
```
