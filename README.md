# duplix

Scaffold a new project by copying a bundled template project, exactly as it is.

## Installation

```bash
pip install duplix
```

## Usage

```bash
python -m duplix my_new_project
# or, after install:
duplix my_new_project
```

This copies duplix's bundled template folder verbatim into `./my_new_project/`
(files, code, and structure unchanged - nothing is renamed or rewritten).

### Options

```bash
python -m duplix my_new_project --dir ./somewhere
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Publishing

```bash
python -m build
python -m twine upload dist/*
```
