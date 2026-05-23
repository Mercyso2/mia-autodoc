# Checklist de execução V4

## 1. Banco

- [ ] Rodar `sql/002_multisite_sharepoint_complement.sql`.
- [ ] Confirmar tabela `autodoc_sharepoint_sites`.
- [ ] Confirmar tabela `autodoc_sharepoint_inventory`.
- [ ] Confirmar colunas novas em `autodoc_files`.
- [ ] Confirmar view `autodoc_v_dashboard`.

## 2. API

- [ ] `/health` retorna ok.
- [ ] `/db/health` retorna ok.
- [ ] `/sharepoint/health` retorna ok.

## 3. SharePoint

- [ ] Cadastrar primeiro site com `/sharepoint/sites/discover`.
- [ ] Sincronizar pastas com `/sharepoint/sites/{id}/sync-folders`.
- [ ] Cadastrar mapa de disciplina.
- [ ] Testar upload HML.
- [ ] Testar duplicado para obsoletos.

## 4. n8n

- [ ] Importar `00` e testar API.
- [ ] Importar `01` e testar Outlook.
- [ ] Importar `10` para fluxo completo manual.
- [ ] Só depois ativar `11` agendado.

## 5. Validação

- [ ] 1 e-mail real.
- [ ] 5 e-mails reais.
- [ ] 20 e-mails reais.
- [ ] Nenhum arquivo em produção.
