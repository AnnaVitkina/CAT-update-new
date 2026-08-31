import json
import os
import subprocess
import sys
from pathlib import Path


def bootstrap_code_root():
    if "__file__" in globals():
        root = Path(__file__).resolve().parent
    else:
        colab_root = Path("/content/CAT-update-new")
        root = colab_root if colab_root.exists() else Path.cwd()

    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    return root


CODE_ROOT = bootstrap_code_root()

import paths  # noqa: E402


def choose_file(files, prompt, cli_arg_index):
    def resolve_selector(raw):
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(files):
                return files[idx]
            return None
        for f in files:
            if f.name == raw:
                return f
        return None

    # Notebook runtimes inject non-user argv values (e.g., kernel json path).
    # Keep only selectors that actually resolve to one of available files.
    cli_values = [arg for arg in sys.argv[1:] if arg and not str(arg).startswith("-")]
    valid_selectors = [arg for arg in cli_values if resolve_selector(str(arg).strip()) is not None]
    if len(valid_selectors) >= cli_arg_index:
        selected = resolve_selector(valid_selectors[cli_arg_index - 1].strip())
        if selected is not None:
            return selected

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
    cmd = [sys.executable, str(CODE_ROOT / script_name), *args]
    env = os.environ.copy()
    env["CAT_CODE_ROOT"] = str(CODE_ROOT)
    env["CAT_INPUT_STORAGE"] = str(paths.INPUT_STORAGE)
    env["CAT_PROCESSING_STORAGE"] = str(paths.PROCESSING_STORAGE)
    env["CAT_OUTPUT_STORAGE"] = str(paths.OUTPUT_STORAGE)
    subprocess.run(cmd, check=True, cwd=CODE_ROOT, env=env)


def main():
    paths.refresh_storage_paths()

    previous_files = sorted(paths.INPUT_PREVIOUS_DIR.glob("*.xlsx"))
    update_files = sorted(paths.INPUT_UPDATE_DIR.glob("*.xlsx"))
    if not previous_files:
        raise FileNotFoundError(
            f"No .xlsx files in {paths.INPUT_PREVIOUS_DIR}. "
            "On Colab, input files must be on Google Drive under "
            f"{paths.INPUT_STORAGE / 'previous rate card'}."
        )
    if not update_files:
        raise FileNotFoundError(
            f"No .xlsx files in {paths.INPUT_UPDATE_DIR}. "
            "On Colab, input files must be on Google Drive under "
            f"{paths.INPUT_STORAGE / 'rate updates'}."
        )

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

    previous_json = paths.PROCESSING_DIR / f"{previous_xlsx.stem}.json"
    update_json = paths.PROCESSING_DIR / f"{update_xlsx.stem}.json"

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
        current_json = paths.PROCESSING_DIR / f"{previous_json.stem}_updated.json"
    else:
        current_json = previous_json

    # 3) Apply ETSBAF update on top of latest JSON only when ETSBAF exists.
    if has_etsbaf:
        run_py("update_previous_with_etsbaf.py", current_json.name, update_json.name)
        final_json = paths.PROCESSING_DIR / f"{current_json.stem}_updated_etsbaf.json"
        not_performed_json = paths.PROCESSING_DIR / f"{current_json.stem}_etsbaf_not_performed.json"
    else:
        final_json = current_json
        not_performed_json = None

    # 4) Export final JSON to XLSX.
    run_py("export_updated_json_to_xlsx_json_only.py", final_json.name)
    final_xlsx = paths.OUTPUT_DIR / f"{final_json.stem}.xlsx"

    not_performed_xlsx = None
    if not_performed_json and not_performed_json.exists():
        run_py("export_etsbaf_not_performed_to_xlsx.py", not_performed_json.name)
        not_performed_xlsx = paths.OUTPUT_DIR / f"{not_performed_json.stem}.xlsx"

    print("\nPipeline completed.")
    print(f"Code root: {CODE_ROOT}")
    print(f"Input storage: {paths.INPUT_STORAGE}")
    print(f"Processing storage: {paths.PROCESSING_STORAGE}")
    print(f"Output storage: {paths.OUTPUT_STORAGE}")
    print(f"Previous source: {previous_xlsx}")
    print(f"Update source: {update_xlsx}")
    print(f"Final JSON: {final_json}")
    print(f"Final XLSX: {final_xlsx}")
    if not_performed_json:
        print(f"ETSBAF not performed log: {not_performed_json}")
        if not_performed_xlsx:
            print(f"ETSBAF not performed XLSX: {not_performed_xlsx}")
    else:
        print("ETSBAF step skipped (no ETSBAF sheet in update JSON).")


if __name__ == "__main__":
    paths.refresh_storage_paths()
    paths.ensure_storage_dirs()
    main()
