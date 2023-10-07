import pytest

from yaa import TestClient
from yaa.plugins.exceptions.middlewares.server_error import ServerErrorMiddleware
from yaa.responses import JSONResponse


def test_handler():
    async def app(scope, receive, send):
        raise RuntimeError("Some error happens")

    def error_500(req, exc):
        return JSONResponse({"detail": "Srv Err"}, status_code=500)

    app = ServerErrorMiddleware(app, handler=error_500)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/")
    assert res.status_code == 500
    assert res.json() == {"detail": "Srv Err"}


def test_debug_text():
    async def app(scope, receive, send):
        raise RuntimeError("Some error happens")

    app = ServerErrorMiddleware(app, debug=True)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/", headers={"Accept": "text/plain, */*"})
    assert res.status_code == 500
    assert res.headers["content-type"].startswith("text/plain")
    assert "RuntimeError" in res.text


def test_debug_html():
    async def app(scope, receive, send):
        raise RuntimeError("Some error happens")

    app = ServerErrorMiddleware(app, debug=True)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/", headers={"Accept": "text/html, */*"})
    assert res.status_code == 500
    assert res.headers["content-type"].startswith("text/html")
    assert "<h2>" in res.text
    assert "RuntimeError" in res.text


def test_error_during_scope():
    async def app(scope, receive, send):
        raise RuntimeError("Some error happens")

    app = ServerErrorMiddleware(app, debug=True)
    client = TestClient(app, raise_server_exceptions=False)
    res = client.get("/", headers={"Accept": "text/html, */*"})
    assert res.status_code == 500
    assert res.headers["content-type"].startswith("text/html")
    assert "RuntimeError" in res.text


def test_debug_not_http():
    """
    DebugMiddleware should just pass through any non-http messages as-is.
    """
    from yaa.concurrency import run_in_threadpool, asyncio

    async def app(scope, receive, send):
        raise RuntimeError("Something went wrong")

    with pytest.raises(RuntimeError):
        asyncio.run(ServerErrorMiddleware(app)({"type": "websocket"}, None, None))


def test_repr():
    from yaa.exceptions import HttpException

    assert repr(HttpException(404)) == (
        "HttpException(status_code=404, detail='Not Found')"
    )
    assert repr(HttpException(404, detail="Not Found: foo")) == (
        "HttpException(status_code=404, detail='Not Found: foo')"
    )

    class CustomHTTPException(HttpException):
        pass

    assert repr(CustomHTTPException(500, detail="Something custom")) == (
        "CustomHTTPException(status_code=500, detail='Something custom')"
    )
