from typing import Any, Dict, Optional
import os

from robot.autodoc_robot import AutodocRobot


def download(
    project: str,
    autodoc_path: str,
    file_name: str,
    context: Optional[Dict[str, Any]] = None,
):
    context = context or {}
    engine = str(os.getenv("AUTODOC_ROBOT_ENGINE", "v1")).strip().lower()
    shadow = str(os.getenv("AUTODOC_ROBOT_SHADOW", "false")).strip().lower() in {"1", "true", "yes", "sim", "on"}

    if engine == "v2" or shadow:
        from robot.autodoc_v2.engine import run_shadow_download_plan
        return run_shadow_download_plan(project, autodoc_path, file_name, context=context)

    return AutodocRobot().download_file(project, autodoc_path, file_name, context=context)
