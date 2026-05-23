from robot.autodoc_robot import AutodocRobot

def download(project: str, autodoc_path: str, file_name: str):
    return AutodocRobot().download_file(project, autodoc_path, file_name)
