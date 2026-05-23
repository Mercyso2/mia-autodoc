# V4.2 — Bootstrap HML automático

Esta atualização remove a necessidade de criar pastas manualmente no SharePoint do cliente.

## O que ela faz

Para cada site SharePoint cadastrado, o sistema agora consegue:

1. sincronizar as pastas existentes da biblioteca Documentos;
2. criar automaticamente `_AUTODOC_HOMOLOGACAO`;
3. criar automaticamente `_OBSOLETOS`, `_PENDENTES` e `_ERROS`;
4. replicar as pastas principais do cliente dentro de `_AUTODOC_HOMOLOGACAO`;
5. gerar automaticamente o mapa `Projeto + Disciplina -> Pasta HML` no Supabase;
6. fazer upload criando pasta automaticamente caso ela ainda não exista.

## Endpoints novos

### Criar estrutura HML

```http
POST /sharepoint/sites/{site_row_id}/bootstrap-hml
```

### Gerar mapa automático

```http
POST /sharepoint/sites/{site_row_id}/generate-folder-map
```

### Preparar tudo de uma vez

```http
POST /sharepoint/sites/{site_row_id}/prepare-hml
```

Este é o endpoint recomendado.

## Ordem de uso

1. Rode `sql/003_bootstrap_hml_automatico_v4_2.sql` no Supabase.
2. Reinicie a API.
3. Abra `http://localhost:8000/docs`.
4. Execute `POST /sharepoint/sites/{site_row_id}/prepare-hml`.
5. Confira no SharePoint se foi criada a pasta `_AUTODOC_HOMOLOGACAO`.
6. Confira no Supabase se `autodoc_folder_map` foi preenchida.

## n8n

Foi adicionado o workflow:

```txt
14_AUTODOC_Preparar_Site_SharePoint_HML.json
```

Antes de executar, edite o node `Configurar site` e troque `siteRowId` pelo ID real do site cadastrado no Supabase.
