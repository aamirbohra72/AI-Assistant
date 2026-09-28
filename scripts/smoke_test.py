"""In-process API smoke test against the real Neon/Redis/Gemini config (2 Gemini requests, no phone call).

Usage:  python -m scripts.smoke_test
"""

import asyncio
import base64
import hashlib
import hmac
import io
import os

os.environ.setdefault("RUN_WORKER_IN_WEB", "false")

import docx  # noqa: E402
import certifi  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from app import redis_store  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Candidate, JobRole  # noqa: E402

failures = 0


def check(condition: bool, name: str) -> None:
    global failures
    failures += not condition
    print(("PASS " if condition else "FAIL ") + name)


def twilio_signature(url: str, params: dict[str, str]) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(get_settings().TWILIO_AUTH_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def resume_docx() -> bytes:
    document = docx.Document()
    for line in [
        "Priya Sharma - Backend Engineer - priya@example.com",
        "Zeta (2021-Present) SDE II: Built FastAPI payment services; cut p99 latency 40% with Redis caching; led Kafka migration.",
        "Skills: Python, FastAPI, Postgres, Redis, Kafka, Docker",
        "B.Tech Computer Science, IIT Delhi, 2020",
    ]:
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def main() -> None:
    s = get_settings()
    headers = {"X-API-Key": s.ADMIN_API_KEY}
    candidate_id = role_id = interview_id = None
    try:
        with TestClient(app) as c:
            check(c.get("/health").status_code == 200, "health")
            check(c.post("/job-roles", json={}).status_code == 401, "admin API key required")

            r = c.post("/job-roles", headers=headers, json={
                "title": "Backend Engineer",
                "jd_text": "Build Python FastAPI microservices on Postgres and Redis for a payments platform. 3+ years experience.",
                "interview_config": {"max_questions": 6, "max_duration_minutes": 10},
            })
            check(r.status_code == 201, "create job role (Gemini rubric)")
            role = r.json()
            role_id = role["id"]
            print("   rubric:", [(x["name"], x["weight"]) for x in role["rubric_json"]["criteria"]])
            print("   focus areas:", role["rubric_json"]["technical_focus_areas"])

            r = c.post(
                "/candidates",
                headers=headers,
                data={"name": "Priya Sharma", "phone": "+1 (415) 555-0123", "email": "Priya@Example.com"},
                files={"resume": ("cv.docx", resume_docx(), "application/octet-stream")},
            )
            check(r.status_code == 201, "create candidate (Gemini resume parsing)")
            if r.status_code != 201:
                print("   ", r.text)
                return
            candidate = r.json()
            candidate_id = candidate["id"]
            print("   phone:", candidate["phone"], "| email:", candidate["email"])
            print("   skills:", candidate["resume_json"].get("skills"))
            print("   claims to validate:", candidate["resume_json"].get("claims_to_validate"))

            bad_file = c.post("/candidates", headers=headers, data={"name": "X", "phone": "+14155550123", "email": "x@example.com"},
                              files={"resume": ("a.txt", b"hello", "text/plain")})
            check(bad_file.status_code == 415, "reject non PDF/DOCX resume")
            bad_phone = c.post("/candidates", headers=headers, data={"name": "X", "phone": "12345", "email": "x@example.com"},
                               files={"resume": ("a.pdf", b"%PDF-1.4", "application/pdf")})
            check(bad_phone.status_code == 422, "reject non E.164 phone")

            body = {"candidate_id": candidate_id, "job_role_id": role_id, "scheduled_at": "2027-09-26T10:00:00"}
            check(c.post("/interviews/schedule", headers=headers, json=body).status_code == 422, "reject timezone-less schedule time")
            body["scheduled_at"] = "2027-09-26T10:00:00+05:30"
            r = c.post("/interviews/schedule", headers=headers, json=body)
            check(r.status_code == 201 and r.json()["status"] == "scheduled", "schedule interview (far future, no call)")
            interview_id = r.json()["id"]
            # Separate sync client: the app's async client is bound to TestClient's event loop.
            probe = redis.Redis.from_url(s.REDIS_URL, decode_responses=True, ssl_ca_certs=certifi.where())
            score = probe.zscore(redis_store.SCHEDULE_KEY, str(interview_id))
            probe.close()
            check(score is not None, "interview queued in Redis")

            check(c.post(f"/interviews/{interview_id}/start").status_code == 401, "start endpoint requires internal token")
            check(c.get(f"/interviews/{interview_id}", headers=headers).json()["status"] == "scheduled", "get interview")
            check(c.get(f"/interviews/{interview_id}/transcript", headers=headers).json() == [], "get transcript")
            check(c.get(f"/interviews/{interview_id}/report", headers=headers).status_code == 202, "report pending -> 202")
            check(c.get("/interviews/999999/report", headers=headers).status_code == 404, "unknown report -> 404")

            path = f"/twilio/status/{interview_id}"
            params = {"CallStatus": "ringing", "CallSid": "CA" + "1" * 32}
            check(c.post(path, data=params, headers={"X-Twilio-Signature": "bad"}).status_code == 403, "Twilio webhook rejects bad signature")
            good = twilio_signature(s.public_base + path, params)
            check(c.post(path, data=params, headers={"X-Twilio-Signature": good}).status_code == 204, "Twilio webhook accepts valid signature")

            twiml_path = f"/twilio/twiml/{interview_id}"
            r = c.post(twiml_path, headers={"X-Twilio-Signature": twilio_signature(s.public_base + twiml_path, {})})
            check(r.status_code == 200 and "<Hangup/>" in r.text, "reconnect TwiML hangs up when interview is not live")

            with c.websocket_connect(f"/media-stream/{interview_id}") as ws:
                ws.send_json({"event": "start", "start": {"streamSid": "MZ", "callSid": "CA" + "0" * 32, "customParameters": {"token": "forged"}}})
                try:
                    ws.receive_text()
                    check(False, "media stream rejects forged token")
                except Exception:
                    check(True, "media stream rejects forged token")
    finally:
        async def cleanup() -> None:
            if interview_id is not None:
                await redis_store.get_redis().zrem(redis_store.SCHEDULE_KEY, str(interview_id))
            async with SessionLocal() as db:
                if candidate_id is not None:
                    await db.execute(delete(Candidate).where(Candidate.id == candidate_id))
                if role_id is not None:
                    await db.execute(delete(JobRole).where(JobRole.id == role_id))
                await db.commit()
            await redis_store.close()
            await engine.dispose()

        asyncio.run(cleanup())
        print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILED'} (test data cleaned up)")


if __name__ == "__main__":
    main()
