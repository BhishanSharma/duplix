import subprocess
import sys
from pathlib import Path


def test_scaffold_copies_template(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-m", "duplix", "test_copy", "--dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    dest = tmp_path / "test_copy"
    assert dest.exists()
    assert (dest / "pyproject.toml").exists()
    assert (dest / "src" / "cli.py").exists()
    assert (dest / "configs" / "config.yml").exists()
    # excel/ was intentionally excluded from the bundled template
    assert not (dest / "excel").exists()


def test_scaffold_refuses_existing_dir(tmp_path: Path):
    (tmp_path / "already_here").mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "duplix", "already_here", "--dir", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "already exists" in result.stderr
