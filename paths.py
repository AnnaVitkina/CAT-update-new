import os
import sys
from pathlib import Path

_DEFAULT_DRIVE_BASE = Path(
    "/content/drive/Shareddrives/FA Ops Europe: Rate Maintenance Team "
    "/Documents/AI Adoption RMT/RMT_CAT_update"
)

COLAB_CODE_ROOT = Path("/content/CAT-update-new")


def resolve_code_root() -> Path:
    env_root = os.environ.get("CAT_CODE_ROOT")
    if env_root:
        return Path(env_root).resolve()

    if COLAB_CODE_ROOT.exists():
        return COLAB_CODE_ROOT

    return Path(__file__).resolve().parent


def is_colab_runtime() -> bool:
    if COLAB_CODE_ROOT.exists():
        return True
    return "google.colab" in sys.modules


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

    # Colab: code lives under /content, all data lives on Google Drive.
    if is_colab_runtime():
        return (
            _DEFAULT_DRIVE_BASE / "input",
            _DEFAULT_DRIVE_BASE / "processing",
            _DEFAULT_DRIVE_BASE / "output",
        )

    # Optional local use when the Drive folder is mounted/available.
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


def refresh_storage_paths() -> None:
    global INPUT_STORAGE, PROCESSING_STORAGE, OUTPUT_STORAGE
    global INPUT_PREVIOUS_DIR, INPUT_UPDATE_DIR, PROCESSING_DIR, OUTPUT_DIR

    INPUT_STORAGE, PROCESSING_STORAGE, OUTPUT_STORAGE = _resolve_storage_roots()
    INPUT_PREVIOUS_DIR = INPUT_STORAGE / "previous rate card"
    INPUT_UPDATE_DIR = INPUT_STORAGE / "rate updates"
    PROCESSING_DIR = PROCESSING_STORAGE
    OUTPUT_DIR = OUTPUT_STORAGE


CODE_ROOT = resolve_code_root()

INPUT_STORAGE, PROCESSING_STORAGE, OUTPUT_STORAGE = _resolve_storage_roots()

INPUT_PREVIOUS_DIR = INPUT_STORAGE / "previous rate card"
INPUT_UPDATE_DIR = INPUT_STORAGE / "rate updates"
PROCESSING_DIR = PROCESSING_STORAGE
OUTPUT_DIR = OUTPUT_STORAGE


def validate_colab_storage() -> None:
    if not is_colab_runtime():
        return

    drive_root = Path("/content/drive")
    if not drive_root.exists():
        raise RuntimeError(
            "Google Drive is not mounted in Colab. Run this first:\n"
            "  from google.colab import drive\n"
            "  drive.mount('/content/drive')"
        )

    if not _DEFAULT_DRIVE_BASE.exists():
        raise RuntimeError(
            "Google Drive is mounted, but the project storage folder was not found:\n"
            f"  {_DEFAULT_DRIVE_BASE}\n"
            "Check the Shared Drive path in paths.py and confirm the folder exists."
        )


def ensure_storage_dirs() -> None:
    validate_colab_storage()
    for directory in (INPUT_PREVIOUS_DIR, INPUT_UPDATE_DIR, PROCESSING_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
