"""
AUTODOC CENTER — Worker do Robô v0.1 — Fase 6.4

Objetivo desta versão:
- Consumir jobs PENDING da API.
- Travar job via POST /robot/jobs/next.
- Simular processamento em modo DRY_RUN.
- Finalizar com POST /robot/jobs/{job_id}/complete.
- Em caso de erro, chamar POST /robot/jobs/{job_id}/fail.

Próximas fases:
- 6.5: plugar download real do AutoDoc.
- 6.6: plugar upload real no SharePoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import requests


DEFAULT_API_BASE_URL = "https://fernada-mia-autodoc-api.ipk3s7.easypanel.host"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return str(value).strip().lower() in {"1", "true", "yes", "sim", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    try:
        return int(value) if value not in (None, "") else default
    except Exception:
        return default


@dataclass
class WorkerConfig:
    api_base_url: str
    api_key: str
    worker_id: str
    poll_interval_seconds: int
    dry_run: bool
    downloads_dir: Path
    max_idle_cycles: int
    request_timeout_seconds: int


class ApiError(RuntimeError):
    pass


class AutodocWorker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-Autodoc-Api-Key": config.api_key,
                "Content-Type": "application/json",
                "User-Agent": f"autodoc-worker/{config.worker_id}",
            }
        )

    def log(self, message: str, **extra: Any) -> None:
        payload = {
            "ts": now_iso(),
            "worker_id": self.config.worker_id,
            "message": message,
            **extra,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.config.api_base_url.rstrip('/')}{path}"

        try:
            response = self.session.request(
                method=method.upper(),
                url=url,
                json=payload,
                timeout=self.config.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Falha de conexão com API: {exc}") from exc

        text = response.text or ""

        try:
            data = response.json() if text else {}
        except Exception:
            data = {"raw": text}

        if response.status_code >= 400:
            raise ApiError(
                f"HTTP {response.status_code} em {method.upper()} {path}: {json.dumps(data, ensure_ascii=False)}"
            )

        return data

    def get_next_job(self) -> Optional[Dict[str, Any]]:
        data = self.request(
            "POST",
            "/robot/jobs/next",
            {"worker_id": self.config.worker_id},
        )

        if not data.get("ok") or not data.get("job"):
            return None

        return data

    def complete_job(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", f"/robot/jobs/{job_id}/complete", payload)

    def fail_job(
        self,
        job_id: str,
        file_id: Optional[str],
        error_type: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        retryable: bool = True,
        retry_delay_minutes: int = 2,
    ) -> Dict[str, Any]:
        return self.request(
            "POST",
            f"/robot/jobs/{job_id}/fail",
            {
                "file_id": file_id,
                "error_type": error_type,
                "message": message,
                "details": details or {},
                "retryable": retryable,
                "retry_delay_minutes": retry_delay_minutes,
            },
        )

    def fake_download_file(self, file_row: Dict[str, Any]) -> Dict[str, Any]:
        """
        DRY_RUN:
        Cria um arquivo fake local para validar o ciclo completo do worker
        sem abrir AutoDoc nem subir SharePoint.
        """
        file_name = file_row.get("file_name") or "autodoc_fake_file.bin"
        safe_name = file_name.replace("/", "_").replace("\\", "_")

        self.config.downloads_dir.mkdir(parents=True, exist_ok=True)

        local_path = self.config.downloads_dir / safe_name

        content = (
            f"AUTODOC CENTER DRY RUN\n"
            f"generated_at={now_iso()}\n"
            f"file_id={file_row.get('id')}\n"
            f"file_name={file_name}\n"
            f"project={file_row.get('project_detected')}\n"
            f"discipline={file_row.get('discipline_detected')}\n"
        ).encode("utf-8")

        local_path.write_bytes(content)

        sha256 = hashlib.sha256(content).hexdigest()

        return {
            "local_path": str(local_path),
            "local_sha256": sha256,
            "local_size_bytes": len(content),
        }

    def process_job(self, job_data: Dict[str, Any]) -> Dict[str, Any]:
        job = job_data["job"]
        file_row = job_data.get("file") or job.get("payload") or {}

        job_id = job["id"]
        file_id = job.get("file_id") or file_row.get("id")

        if not file_id:
            raise RuntimeError("file_id ausente no job")

        file_name = file_row.get("file_name") or (job.get("payload") or {}).get("file_name") or "arquivo_autodoc"

        self.log(
            "Processando job",
            job_id=job_id,
            file_id=file_id,
            file_name=file_name,
            dry_run=self.config.dry_run,
        )

        if self.config.dry_run:
            downloaded = self.fake_download_file({"id": file_id, **file_row})

            sharepoint_final_path = (
                file_row.get("sharepoint_final_path")
                or file_row.get("sharepoint_suggested_path")
                or "_AUTODOC_HOMOLOGACAO/_PENDENTES"
            )

            result_payload = {
                "file_id": file_id,
                "local_path": downloaded["local_path"],
                "local_sha256": downloaded["local_sha256"],
                "local_size_bytes": downloaded["local_size_bytes"],
                "sharepoint_item_id": f"dry_run_item_{file_id}",
                "sharepoint_web_url": f"https://dry-run.local/sharepoint/{file_name}",
                "sharepoint_final_path": f"{sharepoint_final_path.rstrip('/')}/{file_name}",
                "saved_environment": os.getenv("APP_ENV", "HML"),
                "result": {
                    "dry_run": True,
                    "worker_id": self.config.worker_id,
                    "processed_at": now_iso(),
                },
            }

            return self.complete_job(job_id, result_payload)

        # Próxima fase:
        # Aqui vamos plugar:
        # 1) download real do AutoDoc
        # 2) upload real no SharePoint
        raise NotImplementedError(
            "WORKER_DRY_RUN=false ainda não implementado nesta fase. "
            "Use WORKER_DRY_RUN=true até a Fase 6.5/6.6."
        )

    def run_once(self) -> bool:
        job_data = self.get_next_job()

        if not job_data:
            self.log("Nenhum job PENDING disponível")
            return False

        job = job_data["job"]
        file_row = job_data.get("file") or job.get("payload") or {}
        job_id = job["id"]
        file_id = job.get("file_id") or file_row.get("id")

        try:
            result = self.process_job(job_data)
            self.log(
                "Job finalizado",
                job_id=job_id,
                file_id=file_id,
                status=result.get("status"),
            )
            return True

        except Exception as exc:
            self.log(
                "Erro processando job",
                job_id=job_id,
                file_id=file_id,
                error=str(exc),
            )

            try:
                fail_result = self.fail_job(
                    job_id=job_id,
                    file_id=file_id,
                    error_type="WORKER_PROCESS_ERROR",
                    message=str(exc),
                    details={
                        "worker_id": self.config.worker_id,
                        "dry_run": self.config.dry_run,
                    },
                    retryable=True,
                    retry_delay_minutes=2,
                )
                self.log(
                    "Falha registrada na API",
                    job_id=job_id,
                    file_id=file_id,
                    fail_status=fail_result.get("status"),
                    retry_scheduled=fail_result.get("retry_scheduled"),
                )
            except Exception as fail_exc:
                self.log(
                    "Falha crítica: não consegui registrar erro na API",
                    job_id=job_id,
                    file_id=file_id,
                    error=str(fail_exc),
                )

            return False

    def loop(self) -> None:
        idle_cycles = 0

        self.log(
            "Worker iniciado",
            api_base_url=self.config.api_base_url,
            dry_run=self.config.dry_run,
            poll_interval_seconds=self.config.poll_interval_seconds,
            max_idle_cycles=self.config.max_idle_cycles,
        )

        while True:
            processed = self.run_once()

            if processed:
                idle_cycles = 0
            else:
                idle_cycles += 1

            if self.config.max_idle_cycles > 0 and idle_cycles >= self.config.max_idle_cycles:
                self.log("Worker encerrado por max_idle_cycles", idle_cycles=idle_cycles)
                return

            time.sleep(self.config.poll_interval_seconds)


def load_config(args: argparse.Namespace) -> WorkerConfig:
    api_base_url = args.api_base_url or os.getenv("AUTODOC_API_BASE_URL") or DEFAULT_API_BASE_URL
    api_key = args.api_key or os.getenv("AUTODOC_API_KEY") or ""
    worker_id = args.worker_id or os.getenv("AUTODOC_WORKER_ID") or f"worker-{os.getpid()}"

    if not api_key:
        raise RuntimeError(
            "AUTODOC_API_KEY ausente. Defina por variável de ambiente ou use --api-key."
        )

    return WorkerConfig(
        api_base_url=api_base_url,
        api_key=api_key,
        worker_id=worker_id,
        poll_interval_seconds=args.poll_interval or env_int("WORKER_POLL_INTERVAL_SECONDS", 10),
        dry_run=args.dry_run if args.dry_run is not None else env_bool("WORKER_DRY_RUN", True),
        downloads_dir=Path(args.downloads_dir or os.getenv("WORKER_DOWNLOADS_DIR") or "storage/worker_downloads"),
        max_idle_cycles=args.max_idle_cycles if args.max_idle_cycles is not None else env_int("WORKER_MAX_IDLE_CYCLES", 1),
        request_timeout_seconds=args.timeout or env_int("WORKER_REQUEST_TIMEOUT_SECONDS", 60),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AUTODOC CENTER Worker — Fase 6.4")

    parser.add_argument("--api-base-url", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--poll-interval", type=int, default=None)
    parser.add_argument("--downloads-dir", default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--max-idle-cycles", type=int, default=None)

    dry_group = parser.add_mutually_exclusive_group()
    dry_group.add_argument("--dry-run", dest="dry_run", action="store_true")
    dry_group.add_argument("--real", dest="dry_run", action="store_false")
    parser.set_defaults(dry_run=None)

    parser.add_argument(
        "--once",
        action="store_true",
        help="Executa apenas um ciclo e encerra.",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_config(args)
        worker = AutodocWorker(config)

        if args.once:
            worker.log("Executando modo --once")
            worker.run_once()
            return 0

        worker.loop()
        return 0

    except KeyboardInterrupt:
        print("\nWorker interrompido pelo usuário.", flush=True)
        return 130
    except Exception as exc:
        print(f"ERRO FATAL: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
