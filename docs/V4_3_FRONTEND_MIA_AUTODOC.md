# V4.3 — Front-end visual MIA Autodoc

Esta atualização substitui o painel Streamlit antigo por um painel visual premium com a identidade **MIA Autodoc** e cor de marca `#A9798B`.

## Arquivo atualizado

- `app.py`

## O que mudou

- Sidebar com marca MIA Autodoc.
- Navegação visual: Dashboard, Inbox Autodoc, Fila de Arquivos, Robô Autodoc, SharePoint HML, Histórico, Mapas de Pastas e Configurações.
- Dashboard com cards KPI.
- Tabela operacional da fila mostrando claramente **onde cada arquivo foi salvo**.
- Painel de fluxo operacional.
- Painel de rastreabilidade do arquivo.
- Painel visual para cadastro, sincronização e preparação automática de sites SharePoint HML.
- Tela de mapas Projeto + Disciplina → SharePoint.
- Tema dark premium com cor principal `#A9798B`.

## Como aplicar

1. Pare o painel Streamlit, se estiver rodando.
2. Faça backup do arquivo atual `app.py`.
3. Copie o `app.py` deste patch por cima do arquivo atual.
4. Rode:

```bat
scripts\run_panel.bat
```

Ou manualmente:

```bat
.venv\Scripts\activate
streamlit run app.py
```

## Observação

Esta atualização é apenas visual/operacional. Não altera o banco, SQL, API nem n8n.
