import asyncio
import enum
import inspect
import re
import typing
from asyncio import iscoroutinefunction
from concurrent.futures import ThreadPoolExecutor

from yast.datastructures import URL, URLPath
from yast.exceptions import HttpException
from yast.graphql import GraphQLApp
from yast.requests import Request
from yast.responses import PlainTextResponse, RedirectResponse, Response
from yast.types import ASGIApp, ASGIInstance, Receive, Scope, Send
from yast.websockets import WebSocket, WebSocketClose


class NoMatchFound(Exception):
    pass


class Match(enum.Enum):
    NONE = 0
    PARTIAL = 1
    FULL = 2


class BaseRoute(object):
    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        raise NotImplementedError()

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        raise NotImplementedError()

    def __call__(self, scope: Scope) -> ASGIInstance:
        raise NotImplementedError()

    def __str__(self) -> str:
        return "%s(path=%s,endpoint=%s)" % (
            self.__class__.__name__,
            getattr(self, "path", ""),
            getattr(self, "name", ""),
        )


class Route(BaseRoute):
    def __init__(
        self,
        path: str,
        endpoint: typing.Callable,
        *,
        methods: typing.Sequence[str] = None,
        name: str = None,
        include_in_schema: bool = True
    ) -> None:
        assert path.startswith("/"), 'Routed paths must always start "/"'
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name
        self.include_in_schema = include_in_schema
        if inspect.isfunction(endpoint) or inspect.ismethod(endpoint):
            self.app = req_res(endpoint)
            if methods is None:
                methods = ["GET"]
        else:
            self.app = endpoint
        self.methods = methods
        regex = "^" + path + "$"
        regex = re.sub("{([a-zA-Z_][a-zA-Z0-9_]*)}", r"(?P<\1>[^/]+)", regex)
        self.path_regex = re.compile(regex)
        self.param_names = set(self.path_regex.groupindex.keys())

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "http":
            match = self.path_regex.match(scope["path"])
            if match:
                path_params = dict(scope.get("path_params", {}))
                path_params.update(match.groupdict())
                child_scope = {"path_params": path_params}
                if self.methods and scope["method"] not in self.methods:
                    return Match.PARTIAL, child_scope
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        if name != self.name or self.param_names != set(path_params.keys()):
            raise NoMatchFound()
        path, remaining_params = replace_params(self.path, path_params)
        assert not remaining_params
        return URLPath(protocol="http", path=path)

    def __call__(self, scope: Scope) -> ASGIInstance:
        if self.methods and scope["method"] not in self.methods:
            if "app" in scope:
                raise HttpException(status_code=405)
            return PlainTextResponse("Method Not Allowed", 405)

        return self.app(scope)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Route)
            and self.path == other.path
            and self.endpoint == other.endpoint
            and self.methods == other.methods
        )


class WebSocketRoute(BaseRoute):
    def __init__(
        self, path: str, endpoint: typing.Callable, *, name: str = None
    ) -> None:
        assert path.startswith("/"), 'Routed paths must be always start "/"'
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name

        if inspect.isfunction(endpoint) or inspect.ismethod(endpoint):
            self.app = ws_session(endpoint)
        else:
            self.app = endpoint

        regex = "^" + path + "$"
        regex = re.sub("{([a-zA-Z_][a-zA-Z0-9_]*)}", r"(?P<\1>[^/]+)", regex)
        self.path_regex = re.compile(regex)
        self.param_names = set(self.path_regex.groupindex.keys())

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "websocket":
            match = self.path_regex.match(scope["path"])
            if match:
                path_params = dict(scope.get("path_params", {}))
                path_params.update(match.groupdict())
                child_scope = {"path_params": path_params}
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        if name != self.name or self.param_names != set(path_params.keys()):
            raise NoMatchFound()
        path, remaining_params = replace_params(self.path, path_params)
        assert not remaining_params
        return URLPath(protocol="websocket", path=path)

    def __call__(self, scope: Scope) -> ASGIInstance:
        return self.app(scope)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, WebSocketRoute)
            and self.path == other.path
            and self.endpoint == other.endpoint
        )


class Mount(BaseRoute):
    def __init__(self, path: str, app: ASGIApp, name: str = None) -> None:
        assert path == "" or path.startswith("/"), 'Routed paths must always start "/"'
        self.path = path.rstrip("/")
        self.app = app

        regex = "^" + self.path + "(?P<path>/.*)$"
        regex = re.sub("{([a-zA-Z_][a-zA-Z0-9_]*)}", r"(?P<\1>[^/]+)", regex)
        self.path_regex = re.compile(regex)
        self.name = name

    @property
    def routes(self):
        return getattr(self.app, "routes", None)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "http":
            path = scope['path']
            match = self.path_regex.match(path)
            if match:
                matched_params = match.groupdict()
                remaining_path = matched_params.pop('path')
                matched_path = path[:-len(remaining_path)]
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {
                    'path_param': path_params,
                    'root_path': scope.get('root_path', '') + matched_path,
                    'path': remaining_path,
                }
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            path_params["path"] = path_params["path"].lstrip("/")
            path, remaining_params = replace_params(self.path + "/{path}", path_params)
            if not remaining_params:
                return URLPath(path, protocol="http")

        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                remaining_name = name
            else:
                remaining_name = name[len(self.name) + 1 :]

            path, remaining_params = replace_params(self.path, path_params)
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(protocol=url.protocol, path=path + str(url))
                except NoMatchFound as exc:
                    pass

        raise NoMatchFound()

    def __call__(self, scope: Scope) -> ASGIInstance:
        return self.app(scope)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Mount)
            and self.path == other.path
            and self.app == other.app
        )


class Router(object):
    def __init__(
        self,
        routes: typing.List[BaseRoute] = None,
        redirect_slashes: bool = True,
        default: ASGIApp = None,
    ) -> None:
        self.routes = [] if routes is None else routes
        self.redirect_slashes = redirect_slashes
        self.default = self.not_found if default is None else default

    def mount(self, path: str, app: ASGIApp) -> None:
        prefix = Mount(path, app=app)
        self.routes.append(prefix)

    def route(
        self,
        path: str,
        methods: typing.Sequence[str] = None,
        include_in_schema: bool = True,
    ) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_route(
                path, func, methods=methods, include_in_schema=include_in_schema
            )
            return func

        return decorator

    def route_ws(self, path: str) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_route_ws(path, func)
            return func

        return decorator

    def add_route(
        self,
        path: str,
        endpoint: typing.Callable,
        methods: typing.Sequence[str] = None,
        include_in_schema: bool = True,
    ) -> None:
        instance = Route(
            path,
            endpoint=endpoint,
            methods=methods,
            include_in_schema=include_in_schema,
        )
        self.routes.append(instance)

    def add_route_ws(self, path, route: typing.Callable) -> None:
        instance = WebSocketRoute(path, endpoint=route)
        self.routes.append(instance)

    def not_found(self, scope: Scope) -> ASGIInstance:
        if scope["type"] == "websocket":
            return WebSocketClose()

        if "app" in scope:
            raise HttpException(status_code=404)
        return PlainTextResponse("Not Found", 404)

    def url_path_for(self, name: str, **path_params) -> URLPath:
        for route in self.routes:
            try:
                return route.url_path_for(name, **path_params)
            except NoMatchFound:
                pass

        raise NoMatchFound()

    def __call__(self, scope: Scope) -> ASGIInstance:
        assert scope["type"] in ("http", "websocket")
        if "router" not in scope:
            scope["router"] = self

        partial = None

        for route in self.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                return route(scope)
            elif match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            scope.update(partial_scope)
            return partial(scope)

        if self.redirect_slashes and not scope["path"].endswith("/"):
            redirect_scope = dict(scope)
            redirect_scope["path"] += "/"

            for route in self.routes:
                match, child_scope = route.matches(redirect_scope)
                if match != Match.NONE:
                    redirect_url = URL(scope=redirect_scope)
                    return RedirectResponse(url=str(redirect_url))

        return self.default(scope)

    def __eq__(self, other: typing.Any) -> bool:
        return isinstance(other, Router) and self.routes == other.routes


class ProtocalRouter(object):
    def __init__(self, protocals: typing.Dict[str, ASGIApp]) -> None:
        self.protocals = protocals

    def __call__(self, scope: Scope) -> ASGIInstance:
        return self.protocals[scope["type"]](scope)


def req_res(func: typing.Callable):
    is_coroutine = iscoroutinefunction(func)

    def app(scope: Scope) -> ASGIInstance:
        async def awaitable(recv: Receive, send: Send) -> None:
            req = Request(scope, recv)
            kwargs = scope.get("kwargs", {})
            if is_coroutine:
                res = await func(req)
            else:
                loop = asyncio.get_event_loop()
                res = await loop.run_in_executor(None, func, req)

            await res(recv, send)

        return awaitable

    return app


def ws_session(func: typing.Callable):
    def app(scope: Scope) -> ASGIInstance:
        async def awaitable(recv: Receive, send: Send) -> None:
            session = WebSocket(scope, recv, send)
            await func(session)

        return awaitable

    return app


def get_name(endpoint: typing.Callable) -> str:
    if inspect.isfunction(endpoint) or inspect.isclass(endpoint):
        return endpoint.__name__

    return endpoint.__class__.__name__


def replace_params(
    path: str, path_params: typing.Dict[str, str]
) -> typing.Tuple[str, dict]:
    for _k, _v in list(path_params.items()):
        if "{" + _k + "}" in path:
            path_params.pop(_k)
            path = path.replace("{" + _k + "}", _v)
    return path, path_params
