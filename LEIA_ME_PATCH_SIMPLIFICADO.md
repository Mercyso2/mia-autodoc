# MIA AUTODOC — Patch Simplificado Robô v2

Este pacote já vem sem pasta de backup e sem arquivos duplicados.

## O que fazer

1. Extraia esta pasta dentro da raiz do projeto.
2. Rode:

```bash
python aplicar_patch_sem_backup.py
```

3. Teste:

```bash
python tests/test_autodoc_email_intelligence.py
```

4. Suba:

```bash
git add .
git commit -m "add AutoDoc robot v2 shadow plan"
git push
```

## O que muda

### Arquivos substituídos

- `api.py`
- `worker_autodoc.py`
- `services/email_parser.py`
- `robot/autodoc_robot.py`
- `robot/downloader.py`

### Arquivos novos

- `services/autodoc_email_intelligence.py`
- `robot/autodoc_v2/__init__.py`
- `robot/autodoc_v2/engine.py`
- `robot/autodoc_v2/scoring.py`
- `robot/autodoc_v2/evidence.py`
- `tests/test_autodoc_email_intelligence.py`
- `tests/fixtures/autodoc_email_20_links.html`

## Como usar sem risco

Por padrão, o sistema continua no robô antigo.

Para testar o v2 sem baixar nada real:

```powershell
$env:AUTODOC_ROBOT_ENGINE="v2"
$env:AUTODOC_ROBOT_SHADOW="true"
```

Depois rode o worker com `--real-download --dry-upload --once`.

O v2 em shadow gera plano e evidência, mas não faz download real.
