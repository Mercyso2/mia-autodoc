# Autodoc Center V4 — Multi-site SharePoint

Esta versão foi reestruturada para o cenário real: vários sites SharePoint, um por obra/cliente.

## Ideia central

- `.env` guarda credenciais gerais: Supabase, Microsoft Graph, Autodoc e API.
- Supabase guarda o mapa de destino: projeto Autodoc → site SharePoint → biblioteca/drive → pasta.
- Python decide o destino final.
- n8n orquestra e chama a API.

## Ordem de instalação

```bat
cd autodoc_center
scripts\setup_windows.bat
```

Depois:
1. Preencha `.env`.
2. Rode `sql/002_multisite_sharepoint_complement.sql` no Supabase, pois você já rodou o SQL anterior.
3. Rode `scripts\run_api.bat`.
4. Teste `/health`, `/db/health`, `/sharepoint/health`.
5. Abra `scripts\run_panel.bat`.
6. Cadastre sites SharePoint pelo painel ou endpoint `/sharepoint/sites/discover`.
7. Cadastre mapa de pastas por disciplina.
8. Importe workflows n8n.

## Endpoint mais importante para cadastrar site

POST `/sharepoint/sites/discover`

```json
{
  "project_name": "SAE - GUANÁS",
  "site_url": "https://netorg1198871.sharepoint.com/sites/SAEGUANAS",
  "aliases": "SAE GUANAS, GUANÁS, SAE",
  "library_name": "Documentos"
}
```

Isso descobre `site_id` e `drive_id` automaticamente via Microsoft Graph.
