"""Database models — SQLAlchemy 2.x.

Backend auto-switches between SQLite (dev) and Postgres (prod) based on the
DATABASE_URL env var. Postgres is recommended for any multi-user deployment
because SQLite locks under concurrent writes.
"""

import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Boolean, text,
    Table
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# DATABASE_URL examples:
#   sqlite:///./recruiter.db           (default, local dev)
#   postgresql+psycopg://user:pw@host:5432/db   (prod — Neon/Supabase/Railway/etc)
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./recruiter.db")

# Normalize: Heroku/Neon sometimes hand out "postgres://..." which SQLAlchemy 2 rejects.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)

_engine_kwargs: dict = {}
if DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    # Keep connections healthy on cloud Postgres (Neon, Supabase idle-closes)
    _engine_kwargs["pool_pre_ping"] = True

engine = create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# Many-to-many: candidate <-> talent_pool
candidate_pool_assoc = Table(
    "candidate_pool_assoc",
    Base.metadata,
    Column("candidate_id", ForeignKey("candidates.id", ondelete="CASCADE"), primary_key=True),
    Column("pool_id", ForeignKey("talent_pools.id", ondelete="CASCADE"), primary_key=True),
)


class Candidate(Base):
    __tablename__ = "candidates"
    id = Column(Integer, primary_key=True)
    full_name = Column(String(255), nullable=False)
    profile_url = Column(String(500), unique=True, index=True)
    headline = Column(String(500))
    current_title = Column(String(255))
    current_company = Column(String(255))
    location = Column(String(255))
    about = Column(Text)
    tags = Column(String(500))       # comma-separated for v1 simplicity
    stage = Column(String(50), default="New")
    notes = Column(Text)
    # --- Enriched fields ---
    email = Column(String(255), index=True)
    phone = Column(String(100))
    skills_json = Column(Text)              # JSON list of strings
    languages_json = Column(Text)           # JSON list of strings
    visa_status = Column(String(100))
    open_to_relocation = Column(Boolean, default=False)
    years_experience = Column(Integer)
    salary_expectation = Column(String(100))
    # --- Provenance / ownership ---
    source = Column(String(50), default="manual")  # manual / linkedin_extension / csv / proxycurl / unipile
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    last_activity_at = Column(DateTime, default=datetime.utcnow)
    # Kanban: user must explicitly add people they want to track on the board
    in_kanban = Column(Boolean, default=False, nullable=False)
    owner_user_id = Column(Integer, nullable=True, index=True)  # which user created/owns this candidate
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="candidate", cascade="all, delete-orphan")
    pools = relationship("TalentPool", secondary=candidate_pool_assoc, back_populates="candidates")
    owner = relationship("User", foreign_keys=[owner_id])


class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    thread_url = Column(String(1000), index=True)  # dedup key — unique LinkedIn thread URL
    captured_at = Column(DateTime, default=datetime.utcnow)
    summary = Column(Text)            # AI-generated conversation brief
    sentiment = Column(String(50))    # interested / maybe / not_interested / neutral
    source = Column(String(50), default="linkedin_messaging")
    # NEW — recruiter-friendly meta populated by /analyze
    person_brief = Column(Text)                 # 3-4 sentence snapshot about the PERSON
    suggested_pool_id = Column(Integer, ForeignKey("talent_pools.id"), nullable=True)
    suggested_role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    pool_id = Column(Integer, ForeignKey("talent_pools.id"), nullable=True)      # recruiter's pick (overrides suggestion)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)             # recruiter's pick (overrides suggestion)
    analyzed_at = Column(DateTime)
    # Background analyser state — "pending" → "analyzing" → "done" / "failed"
    analysis_status = Column(String(20), default="pending", nullable=False)
    analysis_error = Column(Text)
    owner_user_id = Column(Integer, nullable=True, index=True)  # recruiter who ingested this thread

    candidate = relationship("Candidate", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id")
    drafts = relationship("ReplyDraft", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    sender = Column(String(255))
    body = Column(Text)
    timestamp_raw = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="messages")


class ReplyDraft(Base):
    __tablename__ = "reply_drafts"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    draft_text = Column(Text, nullable=False)
    tone = Column(String(50), default="professional")
    approved = Column(Boolean, default=False)
    approved_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation", back_populates="drafts")


# ============ v2 modules: Roles, Matches, Templates, Replies, Tasks ============

class Role(Base):
    __tablename__ = "roles"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    company = Column(String(255))
    location = Column(String(255))
    seniority = Column(String(50))           # junior / mid / senior / lead / principal
    employment_type = Column(String(50))     # full-time / contract / etc
    jd_text = Column(Text)                   # raw JD pasted in
    must_have_json = Column(Text)            # JSON list of must-have skills
    nice_to_have_json = Column(Text)         # JSON list
    target_companies_json = Column(Text)     # JSON list
    target_titles_json = Column(Text)        # JSON list of ideal title variations
    search_keywords_json = Column(Text)      # JSON list
    outreach_angle = Column(Text)
    persona = Column(Text)                   # ideal candidate persona description
    exclusions = Column(Text)
    status = Column(String(50), default="open")  # open / paused / closed
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    matches = relationship("CandidateRoleMatch", back_populates="role", cascade="all, delete-orphan")
    templates = relationship("MessageTemplate", back_populates="role", cascade="all, delete-orphan")


class CandidateRoleMatch(Base):
    __tablename__ = "candidate_role_matches"
    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)
    fit_score = Column(Integer)              # 0-100
    fit_reason = Column(Text)                # short explanation
    fit_bullets_json = Column(Text)          # JSON list of evidence bullets
    risk_flags_json = Column(Text)           # JSON list of risks
    stage = Column(String(50), default="New")  # per-role pipeline
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    candidate = relationship("Candidate")
    role = relationship("Role", back_populates="matches")


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    id = Column(Integer, primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    template_type = Column(String(50))       # connection_note / first_outreach / follow_up_1 / follow_up_2 / close_loop
    channel = Column(String(50), default="linkedin")  # linkedin / email
    subject_template = Column(Text)
    body_template = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    role = relationship("Role", back_populates="templates")


class ReplyClassification(Base):
    __tablename__ = "reply_classifications"
    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    last_message_body = Column(Text)   # body snapshot so we can detect re-classification needs
    label = Column(String(50))         # interested / maybe_later / send_jd / salary_mismatch / location_mismatch / not_interested / needs_more_info / refer_someone / no_longer_available / other
    confidence = Column(Integer)       # 0-100
    suggested_action = Column(Text)
    suggested_reply = Column(Text)
    handled = Column(Boolean, default=False)  # recruiter has triaged this
    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship("Conversation")


class Task(Base):
    """Follow-up reminders and recruiter todo items."""
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    entity_type = Column(String(50))   # candidate / conversation / role
    entity_id = Column(Integer)
    task_type = Column(String(50))     # follow_up / review_reply / review_fit / stale / other
    title = Column(String(500))
    due_at = Column(DateTime)
    status = Column(String(50), default="pending")  # pending / done / dismissed
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============ v3 modules: Users, Pools, Campaigns, Settings, History ============

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(100), unique=True, index=True, nullable=False)
    password_hash = Column(String(500), nullable=False)   # "scrypt$salt_hex$hash_hex"
    display_name = Column(String(200))
    email = Column(String(255))
    role = Column(String(50), default="recruiter")        # admin / recruiter
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False, nullable=False)  # superuser / workspace admin
    created_at = Column(DateTime, default=datetime.utcnow)
    # Per-user API key — pasted into the Chrome extension so each recruiter
    # saves conversations under their OWN account on a shared backend.
    api_key = Column(String(100), unique=True, index=True, nullable=True)
    # Per-user Apify API token. When set, profile enrichment for candidates
    # this user owns goes through THEIR Apify account (so usage is billed to
    # them). When empty, we fall back to the workspace-wide key stored in the
    # Setting table. Admins can set/clear this from the Admin UI; users can
    # also set their own from Settings.
    apify_api_key = Column(String(500), nullable=True)


class Session(Base):
    __tablename__ = "sessions"
    token = Column(String(100), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)

    user = relationship("User")


class TalentPool(Base):
    __tablename__ = "talent_pools"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    color = Column(String(30), default="#2563eb")
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    candidates = relationship("Candidate", secondary=candidate_pool_assoc, back_populates="pools")


class Campaign(Base):
    __tablename__ = "campaigns"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    template_id = Column(Integer, ForeignKey("message_templates.id"), nullable=True)
    pool_id = Column(Integer, ForeignKey("talent_pools.id"), nullable=True)
    channel = Column(String(50), default="linkedin")   # linkedin / email / unipile
    status = Column(String(50), default="draft")      # draft / sending / done / paused
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CampaignRecipient(Base):
    __tablename__ = "campaign_recipients"
    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id"), nullable=False)
    draft_text = Column(Text)
    status = Column(String(50), default="draft")       # draft / approved / sent / replied / failed / skipped
    sent_at = Column(DateTime)
    replied_at = Column(DateTime)
    external_message_id = Column(String(255))
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class StageHistory(Base):
    __tablename__ = "stage_history"
    id = Column(Integer, primary_key=True)
    candidate_id = Column(Integer, ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True)
    from_stage = Column(String(50))
    to_stage = Column(String(50))
    changed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    changed_at = Column(DateTime, default=datetime.utcnow)


class Setting(Base):
    """App-wide settings (API keys, feature flags). Free-form key/value."""
    __tablename__ = "settings"
    key = Column(String(100), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    Base.metadata.create_all(bind=engine)

    # Lightweight in-place migrations for existing DBs. SQLite ADD COLUMN is
    # safe; we ignore "duplicate column" errors so this is idempotent.
    migrations = [
        "ALTER TABLE conversations ADD COLUMN thread_url VARCHAR(1000)",
        # Candidate enrichment columns
        "ALTER TABLE candidates ADD COLUMN email VARCHAR(255)",
        "ALTER TABLE candidates ADD COLUMN phone VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN skills_json TEXT",
        "ALTER TABLE candidates ADD COLUMN languages_json TEXT",
        "ALTER TABLE candidates ADD COLUMN visa_status VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN open_to_relocation BOOLEAN DEFAULT 0",
        "ALTER TABLE candidates ADD COLUMN years_experience INTEGER",
        "ALTER TABLE candidates ADD COLUMN salary_expectation VARCHAR(100)",
        "ALTER TABLE candidates ADD COLUMN source VARCHAR(50) DEFAULT 'manual'",
        "ALTER TABLE candidates ADD COLUMN owner_id INTEGER",
        "ALTER TABLE candidates ADD COLUMN last_activity_at DATETIME",
        # Task assignment
        "ALTER TABLE tasks ADD COLUMN assigned_to INTEGER",
        # Conversation: person brief + pool/role suggestions (recruiter flow)
        "ALTER TABLE conversations ADD COLUMN person_brief TEXT",
        "ALTER TABLE conversations ADD COLUMN suggested_pool_id INTEGER",
        "ALTER TABLE conversations ADD COLUMN suggested_role_id INTEGER",
        "ALTER TABLE conversations ADD COLUMN pool_id INTEGER",
        "ALTER TABLE conversations ADD COLUMN role_id INTEGER",
        "ALTER TABLE conversations ADD COLUMN analyzed_at DATETIME",
        # Kanban membership flag — user chooses who to track on the board
        "ALTER TABLE candidates ADD COLUMN in_kanban BOOLEAN DEFAULT 0",
        # Background analyser state
        "ALTER TABLE conversations ADD COLUMN analysis_status VARCHAR(20) DEFAULT 'pending'",
        "ALTER TABLE conversations ADD COLUMN analysis_error TEXT",
        # Admin flag on users (elevated privileges)
        "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
        # Conversation ownership — which recruiter-user ingested this thread (multi-user)
        "ALTER TABLE conversations ADD COLUMN owner_user_id INTEGER",
        "ALTER TABLE candidates ADD COLUMN owner_user_id INTEGER",
        # Per-user extension API key
        "ALTER TABLE users ADD COLUMN api_key VARCHAR(100)",
        # Per-user Apify token (falls back to workspace setting when NULL/empty)
        "ALTER TABLE users ADD COLUMN apify_api_key VARCHAR(500)",
    ]
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_conversations_thread_url ON conversations(thread_url)",
        "CREATE INDEX IF NOT EXISTS ix_candidates_email ON candidates(email)",
        "CREATE INDEX IF NOT EXISTS ix_candidates_owner_id ON candidates(owner_id)",
        "CREATE INDEX IF NOT EXISTS ix_candidates_last_activity ON candidates(last_activity_at)",
        # Multi-user scoping — every list query filters on owner_user_id.
        "CREATE INDEX IF NOT EXISTS ix_candidates_owner_user_id ON candidates(owner_user_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_owner_user_id ON conversations(owner_user_id)",
        "CREATE INDEX IF NOT EXISTS ix_conversations_candidate_id ON conversations(candidate_id)",
        "CREATE INDEX IF NOT EXISTS ix_messages_conversation_id ON messages(conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_users_api_key ON users(api_key)",
    ]
    # NOTE: each statement runs in its OWN transaction. On Postgres, once ANY
    # statement in a transaction raises (e.g. "column already exists" from a
    # previously-applied ADD COLUMN), the WHOLE transaction enters an aborted
    # state and every following statement fails with InFailedSqlTransaction.
    # Wrapping each in its own begin() block means each ALTER either applies
    # cleanly or is no-op'd without poisoning the run.
    for sql in migrations:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass
    for sql in indexes:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
