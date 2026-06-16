import json
import os
import subprocess
import sys
from pathlib import Path


def resolve_code_root():
    # Supports execution via `exec(open(...).read())` in Colab where __file__ may be missing.
    if "__file__" in globals():
        return Path(__file__).resolve().parent

    colab_root = Path("/content/CAT-update-new")
    if colab_root.exists():
        return colab_root

    return Path.cwd()


ROOT = resolve_code_root()

INPUT_STORAGE = Path(
    "/content/drive/Shareddrives/FA Ops Europe: Rate Maintenance Team /Documents/AI Adoption RMT/RMT_CAT_update/input"
)
PROCESSING_STORAGE = Path(
    "/content/drive/Shareddrives/FA Ops Europe: Rate Maintenance Team /Documents/AI Adoption RMT/RMT_CAT_update/processing"
)
OUTPUT_STORAGE = Path(
    "/content/drive/Shareddrives/FA Ops Europe: Rate Maintenance Team /Documents/AI Adoption RMT/RMT_CAT_update/output"
)

INPUT_DIR = ROOT / "input"
PROCESSING_DIR = ROOT / "processing"
OUTPUT_DIR = ROOT / "output"
INPUT_PREVIOUS_DIR = INPUT_DIR / "previous rate card"
INPUT_UPDATE_DIR = INPUT_DIR / "rate updates"


def choose_file(files, prompt, cli_arg_index):
    if len(sys.argv) > cli_arg_index:
        raw = sys.argv[cli_arg_index].strip()
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(files):
                return files[idx]
            raise ValueError(f"Index out of range for arg {cli_arg_index}: {raw}")
        for f in files:
            if f.name == raw:
                return f
        raise FileNotFoundError(f"File not found for arg {cli_arg_index}: {raw}")

    print(prompt)
    for i, f in enumerate(files, start=1):
        print(f"{i}. {f.name}")
    selected = input("Enter file number: ").strip()
    if not selected.isdigit():
        raise ValueError("Please enter a valid number.")
    idx = int(selected) - 1
    if not (0 <= idx < len(files)):
        raise ValueError("Selected number is out of range.")
    return files[idx]


def run_py(script_name, *args):
    cmd = [sys.executable, str(ROOT / script_name), *args]
    env = os.environ.copy()
    env["CAT_CODE_ROOT"] = str(ROOT)
    env["CAT_INPUT_STORAGE"] = str(INPUT_STORAGE)
    env["CAT_PROCESSING_STORAGE"] = str(PROCESSING_STORAGE)
    env["CAT_OUTPUT_STORAGE"] = str(OUTPUT_STORAGE)
    subprocess.run(cmd, check=True, cwd=ROOT, env=env)


def ensure_storage_symlink(local_path: Path, target_path: Path):
    target_path.mkdir(parents=True, exist_ok=True)

    if local_path.exists() or local_path.is_symlink():
        try:
            if local_path.resolve() == target_path.resolve():
                return
        except Exception:
            pass
        raise RuntimeError(
            f"{local_path} already exists and is not linked to {target_path}. "
            f"Please remove or relink it before running pipeline."
        )

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.symlink_to(target_path, target_is_directory=True)


def setup_storage_paths():
    # On Colab/Linux: wire local project folders to Google Drive storage via symlink,
    # so existing scripts keep working with their project-relative paths.
    if os.name != "nt":
        ensure_storage_symlink(INPUT_DIR, INPUT_STORAGE)
        ensure_storage_symlink(PROCESSING_DIR, PROCESSING_STORAGE)
        ensure_storage_symlink(OUTPUT_DIR, OUTPUT_STORAGE)


def main():
    previous_files = sorted(INPUT_PREVIOUS_DIR.glob("*.xlsx"))
    update_files = sorted(INPUT_UPDATE_DIR.glob("*.xlsx"))
    if not previous_files:
        raise FileNotFoundError(f"No .xlsx files in {INPUT_PREVIOUS_DIR}")
    if not update_files:
        raise FileNotFoundError(f"No .xlsx files in {INPUT_UPDATE_DIR}")

    previous_xlsx = choose_file(
        previous_files,
        "Choose PREVIOUS rate card file:",
        1,
    )
    update_xlsx = choose_file(
        update_files,
        "Choose UPDATE rate file:",
        2,
    )

    # 1) Extract both files into normalized JSON.
    run_py("extract_previous_rate_card.py", previous_xlsx.name)
    run_py("extract_rate_update.py", update_xlsx.name)

    previous_json = PROCESSING_DIR / f"{previous_xlsx.stem}.json"
    update_json = PROCESSING_DIR / f"{update_xlsx.stem}.json"

    update_payload = json.loads(update_json.read_text(encoding="utf-8"))
    update_records = update_payload.get("records", [])
    has_base = any(r.get("sheet_name") == "BASE" for r in update_records)
    has_etsbaf = any(str(r.get("sheet_name")).upper() == "ETSBAF" for r in update_records)
    previous_payload = json.loads(previous_json.read_text(encoding="utf-8"))
    previous_count = int(previous_payload.get("record_count", 0))
    if previous_count == 0 and not has_base:
        raise ValueError(
            f"Previous file produced 0 records: {previous_json.name}. "
            "Without BASE updates there is no data to update."
        )

    # 2) Apply BASE update first when BASE exists.
    if has_base:
        run_py("update_previous_with_base.py", previous_json.name, update_json.name)
        current_json = PROCESSING_DIR / f"{previous_json.stem}_updated.json"
    else:
        current_json = previous_json

    # 3) Apply ETSBAF update on top of latest JSON only when ETSBAF exists.
    if has_etsbaf:
        run_py("update_previous_with_etsbaf.py", current_json.name, update_json.name)
        final_json = PROCESSING_DIR / f"{current_json.stem}_updated_etsbaf.json"
        not_performed_json = PROCESSING_DIR / f"{current_json.stem}_etsbaf_not_performed.json"
    else:
        final_json = current_json
        not_performed_json = None

    # 4) Export final JSON to XLSX.
    run_py("export_updated_json_to_xlsx_json_only.py", final_json.name)
    final_xlsx = OUTPUT_DIR / f"{final_json.stem}.xlsx"

    print("\nPipeline completed.")
    print(f"Code root: {ROOT}")
    print(f"Input storage: {INPUT_STORAGE}")
    print(f"Processing storage: {PROCESSING_STORAGE}")
    print(f"Output storage: {OUTPUT_STORAGE}")
    print(f"Previous source: {previous_xlsx}")
    print(f"Update source: {update_xlsx}")
    print(f"Final JSON: {final_json}")
    print(f"Final XLSX: {final_xlsx}")
    if not_performed_json:
        print(f"ETSBAF not performed log: {not_performed_json}")
    else:
        print("ETSBAF step skipped (no ETSBAF sheet in update JSON).")


if __name__ == "__main__":
    setup_storage_paths()
    main()
