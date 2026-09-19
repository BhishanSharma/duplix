"""Command-line entry point for duplix.

Running `python -m duplix <project_name>` copies duplix's bundled template
project, as-is, into a new folder named <project_name> in the current
directory (or --dir). No files are renamed or rewritten - it's a plain
copy of the template's files and code.
"""

from __future__ import annotations

import argparse
import importlib.resources
import shutil
import sys
from pathlib import Path


def get_template_dir() -> Path:
    """Locate the bundled template directory inside the installed package."""
    template = importlib.resources.files("duplix").joinpath("_template")
    return Path(str(template))


def scaffold(project_name: str, target_dir: Path) -> Path:
    dest = target_dir / project_name
    if dest.exists():
        raise FileExistsError(f"'{dest}' already exists.")

    template_dir = get_template_dir()
    if not template_dir.exists():
        raise FileNotFoundError(
            f"Bundled template not found at '{template_dir}'. "
            "The duplix package may be installed incorrectly."
        )

    shutil.copytree(template_dir, dest)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="duplix",
        description="Copy duplix's bundled template project into a new folder.",
    )
    parser.add_argument("project_name", help="Name of the new project folder to create")
    parser.add_argument(
        "-d", "--dir", default=".", help="Directory to create the project in (default: current dir)"
    )
    args = parser.parse_args(argv)

    try:
        dest = scaffold(args.project_name, Path(args.dir).resolve())
    except (FileExistsError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Copied template to: {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
