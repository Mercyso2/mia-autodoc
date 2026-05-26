from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent

FILES = [
    "api.py",
    "worker_autodoc.py",
    "services/email_parser.py",
    "services/autodoc_email_intelligence.py",
    "robot/autodoc_robot.py",
    "robot/downloader.py",
    "robot/autodoc_v2/__init__.py",
    "robot/autodoc_v2/engine.py",
    "robot/autodoc_v2/scoring.py",
    "robot/autodoc_v2/evidence.py",
    "tests/test_autodoc_email_intelligence.py",
    "tests/fixtures/autodoc_email_20_links.html",
]

for rel in FILES:
    src = ROOT / rel
    dst = PROJECT / rel

    if not src.exists():
        raise FileNotFoundError(f"Arquivo do patch não encontrado: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"OK -> {rel}")

print("\nPatch aplicado sem backup.")
print("Agora teste:")
print("  python tests/test_autodoc_email_intelligence.py")
