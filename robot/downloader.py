from typing import Any, Dict, Optional

from robot.autodoc_robot import AutodocRobot


def download(
    project: str,
    autodoc_path: str,
    file_name: str,
    context: Optional[Dict[str, Any]] = None,
):
    return AutodocRobot().download_file(project, autodoc_path, file_name, context=context or {})
