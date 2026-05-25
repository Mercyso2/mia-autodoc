# =========================================================
# AUTODOC CENTER — Teste Worker Fase 6.5
# Download real AutoDoc + upload fake/dry-run
# =========================================================

$ApiBaseUrl = "https://fernada-mia-autodoc-api.ipk3s7.easypanel.host"
$ApiKey = "wq9QkcbW5r-u_jl6utuxhIRDcBunkS0jXK746EQSugZenZi-pGEql1lh0ojdnhW6"

python .\worker_autodoc.py `
  --api-base-url $ApiBaseUrl `
  --api-key $ApiKey `
  --worker-id "manual-worker-vscode-real-download" `
  --real-download `
  --dry-upload `
  --once
