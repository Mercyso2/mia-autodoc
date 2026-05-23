@echo off
docker run --rm -p 8501:8501 --env-file .env mia-autodoc-panel
