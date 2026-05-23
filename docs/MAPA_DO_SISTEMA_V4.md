# Mapa do sistema V4

## Fluxo correto

E-mail Autodoc → n8n → API Python → parser → score → Supabase → destino SharePoint por projeto → HML → obsoletos → histórico.

## Multi-site SharePoint

O sistema não usa um único `SHAREPOINT_SITE_ID` como destino global. Cada projeto fica cadastrado em `autodoc_sharepoint_sites`.

Exemplo:

- SAE - GUANÁS → site SAE - GUANÁS → drive Documentos → pastas ARQUITETURA/ESTRUTURA/etc.
- CASAINC - RUA DO PORTO → site CASAINC - RUA DO PORTO → drive Documentos → pastas do projeto.

## Tabelas novas

- `autodoc_sharepoint_sites`: site por projeto/obra.
- `autodoc_sharepoint_inventory`: inventário de pastas sincronizadas.
- `autodoc_folder_map`: agora aponta para `sharepoint_site_ref`.
- `autodoc_files`: agora recebe `sharepoint_site_ref`, `sharepoint_drive_id` e caminho sugerido.

## Regra de decisão

1. Normaliza nome do projeto.
2. Compara com projeto/aliases dos sites cadastrados.
3. Escolhe site por maior similaridade.
4. Normaliza disciplina e caminho Autodoc.
5. Escolhe pasta no mapa.
6. Aplica score.
7. Se score alto: upload HML.
8. Se score baixo: pendência manual.

## Garantia de segurança

O sistema reduz erros com normalização, aliases, score, histórico e aprovação manual. Nenhum sistema pode garantir 100% sem teste real, por isso a produção permanece bloqueada.
