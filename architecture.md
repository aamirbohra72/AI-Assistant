# AI Voice Interviewer Architecture

An asynchronous FastAPI service that turns candidate and job-role data into scheduled phone interviews, streams each conversation in real time, and produces a rubric-based report.

## System Overview

```mermaid
flowchart LR
    HR[Recruiter / HR system]
    Candidate[Candidate on phone]

    subgraph App[AI Voice Interviewer · FastAPI]
        API[REST API<br/>candidates · job roles · interviews]
        Hooks[Twilio webhooks<br/>status · TwiML]
        WS[Media WebSocket<br/>/media-stream]
        Worker[Background worker<br/>scheduled calls · scoring]
        Session[CallSession<br/>audio · turn-taking · transcripts]
        Agent[InterviewAgent<br/>conversation state · prompts]
        Resume[Resume parser<br/>PDF / DOCX]
        Rubric[Rubric generator]
        Scorer[Interview scorer<br/>structured report]
        VAD[Silero VAD<br/>ONNX · turn detection]
        Auth[API key · internal token<br/>Twilio signature · stream token]
    end

    subgraph Data[Persistence and queue]
        DB[(PostgreSQL<br/>candidates · roles · interviews<br/>transcripts · reports)]
        Redis[(Redis<br/>call schedule · scoring queue<br/>conversation state · retries)]
        Model[(Silero ONNX model<br/>models/silero_vad.onnx)]
    end

    subgraph Providers[External providers]
        Twilio[Twilio<br/>outbound calls · telephony audio]
        Deepgram[Deepgram<br/>speech-to-text]
        Gemini[Google Gemini<br/>resume parsing · rubrics<br/>interview responses · scoring]
        Eleven[ElevenLabs<br/>text-to-speech]
    end

    HR -->|HTTPS · X-API-Key| API
    API --> Auth
    Hooks --> Auth
    WS --> Auth
    API --> DB
    API --> Resume
    API --> Rubric
    Resume --> Gemini
    Rubric --> Gemini
    API -->|enqueue scheduled call| Redis
    Worker -->|claim due calls / scoring| Redis
    Worker -->|read and update records| DB
    Worker -->|place outbound call| Twilio
    Twilio -->|status callback · TwiML| Hooks
    Twilio <-->|bidirectional media stream| WS
    WS --> Session
    Session --> VAD
    VAD --> Model
    Session <-->|stream audio / transcript| Deepgram
    Session --> Agent
    Agent <-->|streamed response| Gemini
    Agent -->|spoken reply| Eleven
    Eleven -->|audio returned to call| Session
    Session -->|persist transcript / status| DB
    Session <-->|resume state · reconnect count| Redis
    Session -->|completed interview| Redis
    Worker --> Scorer
    Scorer -->|transcript + rubric| DB
    Scorer -->|structured evaluation| Gemini
    Scorer -->|report · status| DB

    classDef api fill:#e8f1ff,stroke:#3867a8,color:#12243a,stroke-width:1.5px
    classDef logic fill:#e7f5ee,stroke:#34805a,color:#153627,stroke-width:1.5px
    classDef data fill:#fff3d9,stroke:#a87516,color:#38290e,stroke-width:1.5px
    classDef external fill:#fce8e6,stroke:#af4b42,color:#3c201e,stroke-width:1.5px
    class API,Hooks,WS api
    class Worker,Session,Agent,Resume,Rubric,Scorer,VAD,Auth logic
    class DB,Redis,Model data
    class Twilio,Deepgram,Gemini,Eleven external
```

## Interview Lifecycle

```mermaid
sequenceDiagram
    autonumber
    actor HR as Recruiter / HR system
    participant API as FastAPI API
    participant DB as PostgreSQL
    participant Q as Redis
    participant W as Worker
    participant T as Twilio
    participant C as CallSession
    participant STT as Deepgram
    participant LLM as Gemini
    participant TTS as ElevenLabs

    HR->>API: Create role and candidate (API key)
    API->>LLM: Generate rubric / parse resume
    API->>DB: Save role and candidate
    HR->>API: Schedule interview
    API->>DB: Save scheduled interview
    API->>Q: Enqueue scheduled call
    W->>Q: Claim due call
    W->>DB: Load interview and mark calling
    W->>T: Place outbound call
    T->>API: Fetch TwiML and post call status
    T->>C: Open authenticated media WebSocket
    loop Each conversation turn
        T->>C: Candidate audio frames
        C->>C: Silero VAD detects speech turn
        C->>STT: Stream audio
        STT-->>C: Finalized candidate utterance
        C->>LLM: Interview context and utterance
        LLM-->>C: Streamed interviewer response
        C->>TTS: Response text
        TTS-->>C: Speech audio
        C-->>T: Interviewer audio frames
        C->>DB: Persist transcript turns
        C->>Q: Save resumable call state
    end
    T-->>C: Call ends / stream stops
    C->>DB: Mark interview completed
    C->>Q: Enqueue scoring
    W->>Q: Claim scoring job
    W->>DB: Load transcript, resume, and rubric
    W->>LLM: Score evidence against rubric
    LLM-->>W: Scores, recommendation, summary
    W->>DB: Save report and mark scored
    HR->>API: Fetch transcript or report (API key)
    API->>DB: Read interview results
```

## Component Responsibilities

| Area | Responsibility |
| --- | --- |
| `app/main.py` | Builds the FastAPI app, includes routers, loads VAD, starts the in-process worker, and closes shared clients on shutdown. |
| `app/routers/` | Authenticated candidate, job-role, and interview APIs; Twilio callbacks and media WebSocket. |
| `app/services/call_session.py` | Owns one live call: VAD turn detection, streaming STT/LLM/TTS, barge-in, transcript persistence, and reconnect handling. |
| `app/services/interviewer.py` | Interview context, conversation state, question flow, and streamed responses. |
| `app/services/scoring.py` | Scores completed interviews against the role rubric and writes the report. |
| `app/services/resume_parser.py`, `rubric.py` | Extracts resume data and prepares job-specific interview rubrics with Gemini. |
| `app/worker.py`, `app/redis_store.py` | Dispatches due calls, queues scoring, retries scoring, and stores resumable call state. |
| `app/models.py`, `app/db.py`, `alembic/` | SQLAlchemy data model, async PostgreSQL access, and schema migrations. |
| `app/services/stt.py`, `tts.py`, `gemini.py`, `twilio_client.py`, `vad.py` | Provider adapters and local voice-activity detection. |

## Runtime Notes

- The default deployment runs one web instance with the worker inside that same process (`RUN_WORKER_IN_WEB=true`). The Redis queue and PostgreSQL records are the durable scheduling/recovery sources; active WebSocket sessions are process-local.
- PostgreSQL stores business records and interview artifacts. Redis stores transient scheduling/queue entries, retry timing, reconnect counters, and conversation state with a TTL.
- The public deployment must expose HTTPS and secure WebSockets so Twilio can reach the webhook and media-stream endpoints. `PUBLIC_BASE_URL` is used to validate Twilio signatures and construct callback URLs.
- `app/main.py` exposes `/health` for deployment health checks. FastAPI's interactive API documentation is available at `/docs` when the service is running.