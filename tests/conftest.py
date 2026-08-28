from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.api.assessment_routes import get_assessment_agent
from app.api.routes import get_embedding_service, get_profile_agent
from app.db.session import Base
from app.main import app
from app.services.assessment_agent import FakeAssessmentAgent
from app.services.embedding_service import FakeEmbeddingService
from app.services.profile_agent import FakeProfileAgent


@pytest.fixture(autouse=True)
def fake_api_embedding_dependency():
    app.dependency_overrides[get_embedding_service] = FakeEmbeddingService
    app.dependency_overrides[get_profile_agent] = FakeProfileAgent
    app.dependency_overrides[get_assessment_agent] = FakeAssessmentAgent
    yield
    app.dependency_overrides.pop(get_embedding_service, None)
    app.dependency_overrides.pop(get_profile_agent, None)
    app.dependency_overrides.pop(get_assessment_agent, None)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    local_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = local_session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
