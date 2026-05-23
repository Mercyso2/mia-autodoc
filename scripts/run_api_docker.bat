@echo off
docker run --rm -p 8000:8000 --env-file .env mia-autodoc-api
