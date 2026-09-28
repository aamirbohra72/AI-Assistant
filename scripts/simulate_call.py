"""Local end-to-end call test without a phone: this script plays Twilio's role against /media-stream.

It creates a throwaway candidate/role/interview, streams TTS-generated candidate answers as 8 kHz mu-law
in real time, echoes playback marks like Twilio does, then prints latency, transcript and the scored report.

Usage (server must be running):  python -m scripts.simulate_call [--base ws://127.0.0.1:8000] [--keep]
"""

import argparse
import asyncio
import base64
import json
import time
from datetime import UTC, datetime

import httpx
import websockets
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import Candidate, Interview, InterviewStatus, JobRole, Report, Transcript
from app.schemas import InterviewConfig, Rubric
from app.services.rubric import DEFAULT_CRITERIA
from app.services.twilio_client import stream_token
from app.tls import client_ssl_context

CANDIDATE_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
FRAME = 160  # 20 ms of 8 kHz mu-law
SILENCE = b"\xff" * FRAME
ANSWERS = [
    "Sure. At Zeta I led the payments API. I rebuilt the hot endpoints in FastAPI and added Redis caching, "
    "which cut p99 latency by about forty percent.",
    "We cached merchant configs and idempotency keys in Redis with short expiries, and invalidated them on writes. "
    "The tricky part was cache stampedes, which we solved with request coalescing.",
    "A teammate and I once disagreed on a database migration plan. I wrote up both options with data, we ran a "
    "small spike together, and picked the safer rollout. We still shipped on time.",
]
RESUME = {
    "full_name": "Priya Sharma",
    "skills": ["Python", "FastAPI", "Postgres", "Redis", "Kafka"],
    "experience": [
        {"company": "Zeta", "title": "SDE II", "start_date": "2021-06", "end_date": "Present",
         "achievements": ["Cut payments API p99 latency by 40% with Redis caching"]}
    ],
    "education": [{"institution": "IIT Delhi", "degree": "B.Tech", "field": "Computer Science"}],
    "claims_to_validate": ["Cut p99 latency by 40%"],
}


def _media(frame: bytes) -> str:
    return json.dumps({"event": "media", "streamSid": "MZsim", "media": {"track": "inbound", "payload": base64.b64encode(frame).decode()}})


async def synthesize(text: str) -> bytes:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=60, verify=client_ssl_context()) as client:
        response = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{CANDIDATE_VOICE_ID}",
            params={"output_format": "ulaw_8000"},
            headers={"xi-api-key": settings.ELEVENLABS_API_KEY},
            json={"text": text, "model_id": settings.ELEVENLABS_MODEL_ID},
        )
        response.raise_for_status()
        return response.content


async def create_interview() -> tuple[int, int, int]:
    rubric = Rubric(criteria=DEFAULT_CRITERIA, interview_config=InterviewConfig(max_questions=3, max_duration_minutes=5))
    async with SessionLocal() as db:
        candidate = Candidate(name="Priya Sharma", phone="+15555550100", email="sim@example.com", resume_json=RESUME)
        role = JobRole(
            title="Backend Engineer",
            jd_text="Build and scale Python FastAPI microservices on Postgres and Redis. 3+ years backend experience.",
            rubric_json=rubric.model_dump(),
        )
        db.add_all([candidate, role])
        await db.flush()
        interview = Interview(
            candidate_id=candidate.id, job_role_id=role.id, status=InterviewStatus.CALLING, scheduled_at=datetime.now(UTC)
        )
        db.add(interview)
        await db.commit()
        return interview.id, candidate.id, role.id


async def simulate(base: str, interview_id: int) -> None:
    answers = [await synthesize(a) for a in ANSWERS]
    outgoing: asyncio.Queue[bytes] = asyncio.Queue()
    agent_turn_done = asyncio.Event()
    closed = asyncio.Event()
    state = {"first_audio_at": None, "playing_until": 0.0}

    async with websockets.connect(f"{base}/media-stream/{interview_id}") as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({
            "event": "start",
            "streamSid": "MZsim",
            "start": {"streamSid": "MZsim", "callSid": "CA" + "0" * 32, "customParameters": {"token": stream_token(interview_id)}},
        }))

        async def sender() -> None:
            next_tick = time.monotonic()
            while not closed.is_set():
                frame = SILENCE if outgoing.empty() else outgoing.get_nowait()
                try:
                    await ws.send(_media(frame))
                except websockets.ConnectionClosed:
                    return
                next_tick += 0.02
                await asyncio.sleep(max(0.0, next_tick - time.monotonic()))

        async def echo_mark(name: str, delay: float) -> None:
            await asyncio.sleep(delay)
            try:
                await ws.send(json.dumps({"event": "mark", "streamSid": "MZsim", "mark": {"name": name}}))
            except websockets.ConnectionClosed:
                pass
            agent_turn_done.set()

        async def receiver() -> None:
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    now = time.monotonic()
                    if msg["event"] == "media":
                        size = len(base64.b64decode(msg["media"]["payload"]))
                        if state["first_audio_at"] is None:
                            state["first_audio_at"] = now
                        state["playing_until"] = max(state["playing_until"], now) + size / 8000
                    elif msg["event"] == "mark":
                        asyncio.create_task(echo_mark(msg["mark"]["name"], max(0.0, state["playing_until"] - now)))
                    elif msg["event"] == "clear":
                        state["playing_until"] = now
            except websockets.ConnectionClosed:
                pass
            closed.set()
            agent_turn_done.set()

        tasks = [asyncio.create_task(sender()), asyncio.create_task(receiver())]
        t0 = time.monotonic()
        state["first_audio_at"] = None
        await asyncio.wait_for(agent_turn_done.wait(), 180)
        print(f"greeting played (first audio {state['first_audio_at'] - t0:.2f}s after stream start)")

        for i, audio in enumerate(answers, 1):
            if closed.is_set():
                break
            agent_turn_done.clear()
            await asyncio.sleep(0.4)
            for j in range(0, len(audio), FRAME):
                outgoing.put_nowait(audio[j : j + FRAME].ljust(FRAME, b"\xff"))
            while not outgoing.empty():
                await asyncio.sleep(0.01)
            speech_end = time.monotonic()
            state["first_audio_at"] = None
            while state["first_audio_at"] is None and not closed.is_set():
                await asyncio.sleep(0.005)
            if state["first_audio_at"]:
                print(f"answer {i}: agent audio {state['first_audio_at'] - speech_end:.2f}s after candidate stopped "
                      f"(includes {get_settings().VAD_SILENCE_MS} ms end-of-speech window)")
            await asyncio.wait_for(agent_turn_done.wait(), 180)

        with_goodbye = not closed.is_set()
        try:
            await asyncio.wait_for(closed.wait(), 30)
            print("server ended the call" if with_goodbye else "call closed")
        except TimeoutError:
            print("server did not end the call; sending stop")
            await ws.send(json.dumps({"event": "stop", "streamSid": "MZsim"}))
        closed.set()
        for task in tasks:
            task.cancel()


async def show_results(interview_id: int) -> None:
    report = None
    for _ in range(90):
        async with SessionLocal() as db:
            interview = await db.get(Interview, interview_id)
            report = await db.scalar(select(Report).where(Report.interview_id == interview_id))
        if report or interview.status in {InterviewStatus.SCORING_FAILED, InterviewStatus.INCOMPLETE}:
            break
        await asyncio.sleep(2)
    async with SessionLocal() as db:
        turns = list(await db.scalars(select(Transcript).where(Transcript.interview_id == interview_id).order_by(Transcript.turn_index)))
    print(f"\nstatus: {interview.status}\n--- transcript ---")
    for t in turns:
        print(f"[{t.turn_index}] {t.speaker}: {t.text}")
    if report:
        print(f"--- report ---\noverall {report.overall_score} -> {report.recommendation}")
        for s in report.scores_json:
            print(f"  {s['criterion']}: {s['score']}")
        print(f"strengths: {report.strengths}\nred flags: {report.red_flags}\nsummary: {report.summary}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="ws://127.0.0.1:8000")
    parser.add_argument("--keep", action="store_true", help="keep the test rows in the database")
    args = parser.parse_args()

    interview_id, candidate_id, role_id = await create_interview()
    print(f"interview {interview_id} created")
    try:
        await simulate(args.base, interview_id)
        await show_results(interview_id)
    finally:
        if not args.keep:
            async with SessionLocal() as db:
                await db.execute(delete(Candidate).where(Candidate.id == candidate_id))
                await db.execute(delete(JobRole).where(JobRole.id == role_id))
                await db.commit()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
