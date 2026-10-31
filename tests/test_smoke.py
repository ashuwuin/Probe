import sys

def test_package_import():
    """Verify that the probe package is importable and has a version string."""
    import probe
    assert hasattr(probe, "__version__")
    assert isinstance(probe.__version__, str)

def test_dependencies_healthy():
    """Verify core third-party dependencies are installed and importable."""
    import fastapi
    import pydantic
    import httpx
    import openai
    assert fastapi.__version__ is not None
    assert pydantic.__version__ is not None
