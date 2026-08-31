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


def _resolve_storage_roots() -> tuple[Path, Path, Path]:
    # Explicit env overrides (set by run_pipeline subprocess calls).
    if any(
        os.environ.get(key)
        for key in ("CAT_INPUT_STORAGE", "CAT_PROCESSING_STORAGE", "CAT_OUTPUT_STORAGE")
    ):
        return (
            _storage_path("CAT_INPUT_STORAGE", _DEFAULT_DRIVE_BASE / "input"),
            _storage_path("CAT_PROCESSING_STORAGE", _DEFAULT_DRIVE_BASE / "processing"),
            _storage_path("CAT_OUTPUT_STORAGE", _DEFAULT_DRIVE_BASE / "output"),
        )

    # Colab: use Google Drive when the shared project folder is mounted.
    if _DEFAULT_DRIVE_BASE.exists():
        return (
            _DEFAULT_DRIVE_BASE / "input",
            _DEFAULT_DRIVE_BASE / "processing",
            _DEFAULT_DRIVE_BASE / "output",
        )

    # Local machine: keep data next to the code checkout.
    code_root = resolve_code_root()
    return (
        code_root / "input",
        code_root / "processing",
        code_root / "output",
    )


CODE_ROOT = resolve_code_root()

INPUT_STORAGE, PROCESSING_STORAGE, OUTPUT_STORAGE = _resolve_storage_roots()

INPUT_PREVIOUS_DIR = INPUT_STORAGE / "previous rate card"
INPUT_UPDATE_DIR = INPUT_STORAGE / "rate updates"
PROCESSING_DIR = PROCESSING_STORAGE
OUTPUT_DIR = OUTPUT_STORAGE


def ensure_storage_dirs() -> None:
    for directory in (INPUT_PREVIOUS_DIR, INPUT_UPDATE_DIR, PROCESSING_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
