from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, status

from app.config import get_settings
from app.logging_setup import interview_id_var
from app.services import interviews, twilio_client
from app.services.call_session import CallSession

router = APIRouter(include_in_schema=False)


async def _verified_form(request: Request) -> dict[str, str]:
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    settings = get_settings()
    if settings.TWILIO_VALIDATE_SIGNATURES:
        # Twilio signs the public URL it called, not the internal one behind Render's proxy.
        url = settings.public_base + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        if not twilio_client.validate_signature(url, params, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid Twilio signature")
    return params


@router.post("/twilio/status/{interview_id}")
async def call_status(interview_id: int, request: Request) -> Response:
    params = await _verified_form(request)
    token = interview_id_var.set(interview_id)
    try:
        await interviews.handle_call_status(interview_id, params.get("CallStatus", ""))
    finally:
        interview_id_var.reset(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/twilio/twiml/{interview_id}")
async def reconnect_twiml(interview_id: int, request: Request) -> Response:
    await _verified_form(request)
    token = interview_id_var.set(interview_id)
    try:
        allowed = await interviews.allow_stream_reconnect(interview_id)
    finally:
        interview_id_var.reset(token)
    xml = twilio_client.build_stream_twiml(interview_id) if allowed else twilio_client.HANGUP_TWIML
    return Response(content=xml, media_type="application/xml")


@router.websocket("/media-stream/{interview_id}")
async def media_stream(websocket: WebSocket, interview_id: int) -> None:
    await CallSession(websocket, interview_id).run()
