"""Shared runtime config for the Digest package.

Loads the project-root .env once (without overriding variables the caller
already set) so digest modules can run standalone scripts as well as inside
the FastAPI process.
"""
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_env_file(env_path: str = None) -> None:
    """Populate os.environ from the project .env; existing values win."""
    path = env_path or os.path.join(_PROJECT_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def project_root() -> str:
    return _PROJECT_ROOT
