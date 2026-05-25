# =========================================================
# AUTODOC CENTER — Teste Worker Fase 6.4
# =========================================================

$ApiBaseUrl = "https://fernada-mia-autodoc-api.ipk3s7.easypanel.host"
$ApiKey = "COLE_SUA_API_KEY_AQUI"

python .\worker_autodoc.py `
  --api-base-url $ApiBaseUrl `
  --api-key $ApiKey `
  --worker-id "manual-worker-vscode" `
  --dry-run `
  --once
