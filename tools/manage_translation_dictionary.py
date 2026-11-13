"""Download or import the gallery's independent ffdkj SQLite dictionary."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("gallery_translation", ROOT / "py/shared/translation/translation_loader.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--download", action="store_true")
    action.add_argument("--import-file", type=Path)
    action.add_argument("--status", action="store_true")
    args = parser.parse_args()
    target = module.DEFAULT_DICTIONARY
    if args.status:
        print(json.dumps({"path": str(target), **module.TranslationLoader().get_stats()}, ensure_ascii=False))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="dictionary-", suffix=".sqlite", dir=target.parent)
    os.close(fd)
    try:
        if args.download:
            with urlopen(module.SOURCE_URL, timeout=120) as response, open(temporary, "wb") as output:
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError("Dictionary exceeds 64 MiB")
                    output.write(chunk)
        else:
            shutil.copyfile(args.import_file, temporary)
        count = module.validate_dictionary(temporary)
        digest = hashlib.sha256(Path(temporary).read_bytes()).hexdigest()
        os.replace(temporary, target)
        print(json.dumps({"path": str(target), "tags": count, "sha256": digest}, ensure_ascii=False))
    finally:
        Path(temporary).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
