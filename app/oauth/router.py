from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from app.api.deps import client_ip, get_current_user_id, same_origin
from app.core.exceptions import AppError
from app.core.responses import api_router
from app.oauth import service
from app.oauth.service import OAuthError

# OAuth's own endpoints answer in the shapes its specs give, not the API's envelope
protocol = APIRouter(tags=["oauth"])
# what our consent page calls, as the signed-in person
consent = api_router(prefix="/oauth/requests", tags=["oauth"])

NO_STORE = {"Cache-Control": "no-store"}


def _refused(error: OAuthError) -> JSONResponse:
    return JSONResponse({"error": error.code, "error_description": error.description}, status_code=error.status, headers=NO_STORE)


@protocol.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata() -> JSONResponse:
    return JSONResponse(service.metadata())


@protocol.post("/oauth/register")
async def register(request: Request, ip: str = Depends(client_ip)) -> JSONResponse:
    try:
        document = await request.json()
        if not isinstance(document, dict):
            raise ValueError
    except ValueError:
        return _refused(OAuthError("invalid_client_metadata", "send the client's metadata as a JSON object"))
    try:
        return JSONResponse(await service.register(document, ip), status_code=201, headers=NO_STORE)
    except OAuthError as error:
        return _refused(error)


@protocol.get("/oauth/authorize")
async def authorize(request: Request):
    try:
        return RedirectResponse(await service.start(dict(request.query_params)), status_code=302)
    except OAuthError as error:
        # the client or its redirect address isn't trusted, so the person is told here rather than sent there
        return PlainTextResponse(f"Can't connect this AI tool: {error.description}.", status_code=error.status)


@protocol.post("/oauth/token")
async def token(request: Request) -> JSONResponse:
    form = {name: value for name, value in (await request.form()).items() if isinstance(value, str)}
    try:
        return JSONResponse(await service.tokens(form), headers=NO_STORE)
    except OAuthError as error:
        return _refused(error)
    except AppError as error:
        # e.g. the person has as many connected tools as one may have
        return _refused(OAuthError("invalid_grant", error.message))


class PendingRequest(BaseModel):
    id: str
    client_name: str
    #: where the person is sent back to, to judge whether this is the tool they meant
    redirect_host: str
    #: the tool runs on the person's own machine
    on_this_device: bool


class Approval(BaseModel):
    workspace_id: str
    read_only: bool = False
    ask_first: bool = True


class Answered(BaseModel):
    #: the tool's address to send the browser to
    redirect_to: str


def _api_error(error: OAuthError) -> AppError:
    return AppError(error.description, error_code="sign_in_request_gone", status_code=error.status)


@consent.get("/{request_id}")
async def pending(request_id: str, _: str = Depends(get_current_user_id)) -> PendingRequest:
    try:
        return PendingRequest(**await service.pending(request_id))
    except OAuthError as error:
        raise _api_error(error)


@consent.post("/{request_id}/approve", dependencies=[Depends(same_origin)])
async def approve(request_id: str, body: Approval, user_id: str = Depends(get_current_user_id)) -> Answered:
    try:
        return Answered(redirect_to=await service.approve(
            user_id, request_id, body.workspace_id, read_only=body.read_only, ask_first=body.ask_first,
        ))
    except OAuthError as error:
        raise _api_error(error)


@consent.post("/{request_id}/deny", dependencies=[Depends(same_origin)])
async def deny(request_id: str, _: str = Depends(get_current_user_id)) -> Answered:
    try:
        return Answered(redirect_to=await service.deny(request_id))
    except OAuthError as error:
        raise _api_error(error)
