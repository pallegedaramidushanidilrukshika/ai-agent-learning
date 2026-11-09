import os


ARTIFACT_DIR_ENV = "WORKFLOW_ARTIFACT_DIR"


def resolve_artifact_path(filename, base_dir, run_dir=None):
    """Resolve an artifact under a run directory or the standalone base."""
    artifact_dir = (
        run_dir
        or os.environ.get(ARTIFACT_DIR_ENV)
        or base_dir
    )
    return os.path.join(
        os.fspath(artifact_dir),
        os.fspath(filename),
    )