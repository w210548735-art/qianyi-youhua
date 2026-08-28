from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.api.assessment_routes import router as assessment_router
from app.api.feedback_routes import router as feedback_router
from app.api.memory_routes import router as memory_router
from app.api.output_routes import router as output_router
from app.api.place_routes import router as place_router
from app.api.report_routes import router as report_router
from app.api.routes import router
from app.core.config import ROOT_DIR, settings
from app.db.session import Base, SessionLocal, engine
from app.services.assessment_service import AssessmentService
from app.services.feedback_service import FeedbackService
from app.services.output_service import OutputService
from app.services.report_service import ReportService


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        AssessmentService(db).recover_unfinished_assessments()
        OutputService(db).recover_unfinished_outputs()
        FeedbackService(db).recover_unfinished()
        ReportService(db).recover_unfinished()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(memory_router)
app.include_router(place_router)
app.include_router(assessment_router)
app.include_router(output_router)
app.include_router(feedback_router)
app.include_router(report_router)
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context={})
