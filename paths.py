import os
from pathlib import Path

_DEFAULT_DRIVE_BASE = Path(
    "/content/drive/Shareddrives/FA Ops Europe: Rate Maintenance Team "
    "/Documents/AI Adoption RMT/RMT_CAT_update"
)


def resolve_code_root() -> Path:
    env_root = os.environ.get("CAT_CODE_ROOT")
    if env_root:
        return Path(env_root).resolve()

    colab_root = Path("/content/CAT-update-new")
    if colab_root.exists():
        return colab_root

    return Path(__file__).resolve().parent


def _storage_path(env_key: str, default: Path) -> Path:
    override = os.environ.get(env_key)
    return Path(override) if override else default


CODE_ROOT = resolve_code_root()

INPUT_STORAGE = _storage_path("CAT_INPUT_STORAGE", _DEFAULT_DRIVE_BASE / "input")
PROCESSING_STORAGE = _storage_path("CAT_PROCESSING_STORAGE", _DEFAULT_DRIVE_BASE / "processing")
OUTPUT_STORAGE = _storage_path("CAT_OUTPUT_STORAGE", _DEFAULT_DRIVE_BASE / "output")

INPUT_PREVIOUS_DIR = INPUT_STORAGE / "previous rate card"
INPUT_UPDATE_DIR = INPUT_STORAGE / "rate updates"
PROCESSING_DIR = PROCESSING_STORAGE
OUTPUT_DIR = OUTPUT_STORAGE


def ensure_storage_dirs() -> None:
    for directory in (INPUT_PREVIOUS_DIR, INPUT_UPDATE_DIR, PROCESSING_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
