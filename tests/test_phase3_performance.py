from __future__ import annotations

import time
from datetime import date, timedelta

from app.models import Blogger, Output, Schedule
from app.services.output_agent import FakeOutputAgent
from app.services.output_service import OutputService
from app.services.schedule_service import ScheduleService


def make_blogger(db, name: str, frequency: str = "日更") -> Blogger:
    row = Blogger(
        name=name,
        platform="抖音",
        content_types_json='["美食"]',
        style="口播",
        follower_band="1万以下",
        monetization_types_json="[]",
        frequency=frequency,
        profile_state="complete",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def output_row(blogger_id: int, index: int) -> Output:
    return Output(
        blogger_id=blogger_id,
        idempotency_key=f"performance-output-{index}",
        type="script",
        category="美食",
        title=f"性能输出{index}",
        content_json="{}",
        status="succeeded",
        version=1,
        manual_locked=False,
        prompt_version="performance",
        model_name="fake",
    )


def test_1000_output_and_schedule_queries_under_500ms(db):
    blogger = make_blogger(db, "千条查询博主")
    outputs = [output_row(blogger.id, index) for index in range(1000)]
    db.add_all(outputs)
    db.flush()
    planned = date.today() + timedelta(days=30)
    db.add_all(
        [
            Schedule(
                blogger_id=blogger.id,
                output_id=row.id,
                plan_date=planned,
                platform="抖音",
                content_type="script",
                title=row.title,
                status="pending",
            )
            for row in outputs
        ]
    )
    db.commit()

    output_service = OutputService(db, agent=FakeOutputAgent())
    started = time.perf_counter()
    loaded_outputs = output_service.list_outputs(blogger.id, limit=1000)
    output_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    loaded_schedules = ScheduleService(db).list_schedules(blogger.id, limit=1000)
    schedule_elapsed = time.perf_counter() - started

    print(f"PHASE3_OUTPUT_QUERY_1000_SECONDS={output_elapsed:.6f}")
    print(f"PHASE3_SCHEDULE_QUERY_1000_SECONDS={schedule_elapsed:.6f}")

    assert len(loaded_outputs) == 1000 and output_elapsed < 0.5
    assert len(loaded_schedules) == 1000 and schedule_elapsed < 0.5


def test_ordinary_output_and_schedule_crud_under_300ms(db):
    blogger = make_blogger(db, "普通CRUD博主")
    output = output_row(blogger.id, 9999)
    db.add(output)
    db.commit()
    db.refresh(output)
    output_service = OutputService(db, agent=FakeOutputAgent())

    started = time.perf_counter()
    loaded = output_service.get_output(blogger.id, output.id)
    output_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    schedule = ScheduleService(db).create_schedule(
        blogger.id,
        output.id,
        date.today() + timedelta(days=1),
        "抖音",
        "script",
        "普通性能排期",
    )
    schedule_elapsed = time.perf_counter() - started

    print(f"PHASE3_OUTPUT_DETAIL_SECONDS={output_elapsed:.6f}")
    print(f"PHASE3_SCHEDULE_CREATE_SECONDS={schedule_elapsed:.6f}")

    assert loaded.id == output.id and output_elapsed < 0.3
    assert schedule.id is not None and schedule_elapsed < 0.3
