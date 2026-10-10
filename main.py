import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
MODEL = os.getenv("RODIUMAI_MODEL", "anthropic/claude-sonnet-4-5-20250929")

# Liste des modèles autorisés, définie côté serveur (jamais décidée par le client)
ALLOWED_MODELS = [
    m.strip()
    for m in os.getenv("ALLOWED_MODELS", f"{MODEL},openai/gpt-4o-mini").split(",")
    if m.strip()
]

PREVIEW_LENGTH = 60

# Rôles que le LLM comprend. Tout autre rôle stocké en base reste hors de son prompt.
LLM_ROLES = {"user", "assistant"}

NOTIFICATION_ROLE = "system-notification"
NOTE_ROLE = "note"  # rôle custom : note personnelle de l'étudiant

NOTIFICATION_EVERY = 10
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

# Le prompt système est rangé dans un fichier dédié
SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")

app = FastAPI(title="Study Buddy Chatbot")


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str


class NoteRequest(BaseModel):
    content: str


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return db.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq)
    ).all()


def build_llm_history(rows: list[Message]) -> list[dict]:
    # L'historique en base n'est pas forcément celui vu par le LLM : on filtre ici, juste avant la requête.
    # Les rôles "note" (note privée de l'étudiant) et "system-notification" sont exclus :
    # le LLM ne connaît que user/assistant, et une note privée ne doit ni fuiter ni coûter des tokens.
    return [{"role": m.role, "content": m.content} for m in rows if m.role in LLM_ROLES]


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/models")
def list_models() -> list[str]:
    return ALLOWED_MODELS


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(Message, and_(Message.conversation_id == Conversation.id, Message.seq == 1))
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def add_note(conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)) -> MessageResponse:
    rows = load_messages(db, conversation_id)
    note = Message(
        conversation_id=conversation_id,
        seq=rows[-1].seq + 1 if rows else 1,
        role=NOTE_ROLE,
        content=req.content,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return MessageResponse(seq=note.seq, role=note.role, content=note.content, created_at=note.created_at)


@app.post("/chat")
def chat(req: ChatRequest, db: Session = Depends(get_db)):
    # Ne jamais faire confiance au client : le modèle doit être dans la liste du serveur
    if req.model not in ALLOWED_MODELS:
        raise HTTPException(status_code=400, detail="Model not allowed.")

    rows = load_messages(db, req.conversation_id)
    history = build_llm_history(rows)
    next_seq = rows[-1].seq + 1 if rows else 1
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": req.message},
    ]
    dialogue_count = len(history) + 2

    def event_stream():
        parts: list[str] = []
        try:
            with httpx.stream(
                "POST",
                RODIUMAI_URL,
                headers={"Authorization": f"Bearer {RODIUMAI_API_KEY}"},
                json={"model": req.model, "messages": messages, "max_tokens": 512, "stream": True},
                timeout=60,
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    choices = json.loads(data).get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}).get("content")
                    if delta:
                        parts.append(delta)
                        yield sse({"content": delta})
        except (httpx.HTTPError, json.JSONDecodeError):
            # Flux interrompu : rien n'a été écrit en base, donc aucune incohérence
            yield sse({"error": "The LLM API call failed."})
            return

        reply = "".join(parts)
        if not reply:
            yield sse({"error": "Empty response from the LLM."})
            return

        # Enregistrement seulement une fois le flux terminé, avec une session dédiée
        gen = get_db()
        save_db = next(gen)
        notification = None
        try:
            save_db.add_all([
                Message(conversation_id=req.conversation_id, seq=next_seq, role="user", content=req.message),
                Message(conversation_id=req.conversation_id, seq=next_seq + 1, role="assistant", content=reply),
            ])
            if dialogue_count % NOTIFICATION_EVERY == 0:
                notification = NOTIFICATION_TEXT
                save_db.add(Message(
                    conversation_id=req.conversation_id, seq=next_seq + 2,
                    role=NOTIFICATION_ROLE, content=notification,
                ))
            try:
                save_db.commit()
            except IntegrityError:
                save_db.rollback()
                yield sse({"error": "The conversation was updated concurrently, please retry."})
                return
        finally:
            gen.close()

        yield sse({"done": True, "notification": notification})

    return StreamingResponse(event_stream(), media_type="text/event-stream")