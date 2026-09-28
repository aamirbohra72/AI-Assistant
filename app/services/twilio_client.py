import base64
import hashlib
import hmac
import re
from collections.abc import Mapping
from xml.sax.saxutils import escape, quoteattr

from app.config import get_settings
from app.http_client import request_with_retry

_API = "https://api.twilio.com/2010-04-01"
_CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")

HANGUP_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>'


def _credentials() -> tuple[str, str]:
    settings = get_settings()
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and settings.TWILIO_FROM_NUMBER):
        raise RuntimeError("Twilio is not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER)")
    return settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN


def stream_token(interview_id: int) -> str:
    secret = get_settings().STREAM_TOKEN_SECRET.encode()
    return hmac.new(secret, f"media-stream:{interview_id}".encode(), hashlib.sha256).hexdigest()


def verify_stream_token(interview_id: int, token: str) -> bool:
    return bool(token) and hmac.compare_digest(stream_token(interview_id), token)


def build_stream_twiml(interview_id: int) -> str:
    settings = get_settings()
    ws_url = f"{settings.public_ws_base}/media-stream/{interview_id}"
    # If our WebSocket drops (e.g. worker restart), Twilio falls through to <Redirect> and reconnects.
    redirect_url = f"{settings.public_base}/twilio/twiml/{interview_id}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f"<Stream url={quoteattr(ws_url)}>"
        f'<Parameter name="token" value={quoteattr(stream_token(interview_id))}/>'
        "</Stream></Connect>"
        f'<Redirect method="POST">{escape(redirect_url)}</Redirect></Response>'
    )


async def create_call(to: str, interview_id: int) -> str:
    account_sid, auth_token = _credentials()
    settings = get_settings()
    data = {
        "To": to,
        "From": settings.TWILIO_FROM_NUMBER,
        "Twiml": build_stream_twiml(interview_id),
        "StatusCallback": f"{settings.public_base}/twilio/status/{interview_id}",
        "StatusCallbackMethod": "POST",
        "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        "Timeout": "30",
    }
    response = await request_with_retry(
        "POST",
        f"{_API}/Accounts/{account_sid}/Calls.json",
        data=data,
        auth=(account_sid, auth_token),
        idempotent=False,
    )
    return response.json()["sid"]


async def hangup(call_sid: str) -> None:
    if not _CALL_SID_RE.match(call_sid):
        raise ValueError("invalid call sid")
    account_sid, auth_token = _credentials()
    await request_with_retry(
        "POST",
        f"{_API}/Accounts/{account_sid}/Calls/{call_sid}.json",
        data={"Status": "completed"},
        auth=(account_sid, auth_token),
    )


def validate_signature(url: str, params: Mapping[str, str], signature: str) -> bool:
    """Twilio request validation: base64(HMAC-SHA1(auth_token, url + sorted(key+value)))."""
    auth_token = get_settings().TWILIO_AUTH_TOKEN
    if not auth_token or not signature:
        return False
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)
