import pytest


@pytest.fixture(
    params=[
        pytest.param(("asyncio", {"httpcore_use_anyio": False}), id="asyncio"),
        pytest.param(("asyncio", {"httpcore_use_anyio": True}), id="asyncio+anyio"),
        pytest.param(("trio", {}), id="trio"),
    ]
)
def anyio_backend(request, monkeypatch):
    backend_name, options = request.param
    options = {**options}
    use_anyio = options.pop("httpcore_use_anyio", False)
    monkeypatch.setenv("HTTPCORE_PREFER_ANYIO", "1" if use_anyio else "0")
    return backend_name, options
