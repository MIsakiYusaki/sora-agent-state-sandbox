from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a UTF-8 text file without PowerShell mojibake.")
    parser.add_argument("path", help="UTF-8 text file to print.")
    args = parser.parse_args(argv)

    path = Path(args.path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"read_utf8: {exc}", file=sys.stderr)
        return 1
    except UnicodeDecodeError as exc:
        print(f"read_utf8: {path} is not valid UTF-8: {exc}", file=sys.stderr)
        return 1

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
