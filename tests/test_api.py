"""端到端测试：使用临时数据库，覆盖全部业务规则。

运行：ATTRIBUTION_DB_PATH 指向临时文件后 pytest。
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("ATTRIBUTION_DB_PATH", str(db_file))

    import app.database as database
    import app.main as main

    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    with TestClient(main.app) as c:
        yield c


# ---------- 基础 ----------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_empty_batch_rejected(client):
    r = client.post("/api/revisions", json={"entries": []})
    assert r.status_code == 422  # pydantic min_length


# ---------- 修订号递增 ----------

def test_revision_numbers_increment(client):
    r1 = client.post("/api/revisions", json={
        "entries": [{"employee_id": "E1", "cost_center": "CC-A",
                     "effective_from": "2026-01-01", "effective_to": "2026-02-01"}]
    })
    r2 = client.post("/api/revisions", json={
        "entries": [{"employee_id": "E1", "cost_center": "CC-B",
                     "effective_from": "2026-03-01", "effective_to": "2026-04-01"}]
    })
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["revision_id"] == 1
    assert r2.json()["revision_id"] == 2
    assert r1.json()["entry_count"] == 1


# ---------- 整批拒绝规则 ----------

def test_invalid_range_from_not_before_to_rejects_whole_batch(client):
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-02-01", "effective_to": "2026-02-01"},
    ]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_DATE_RANGE"
    # 数据与修订号均不变
    assert client.get("/api/revisions").json() == []


def test_overlap_within_batch_rejects_whole_batch(client):
    # 先放一个合法修订号 1
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"}
    ]})
    # 批内同员工重叠 -> 拒绝
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-X",
         "effective_from": "2026-06-01", "effective_to": "2026-08-01"},
        {"employee_id": "E1", "cost_center": "CC-Y",
         "effective_from": "2026-07-01", "effective_to": "2026-09-01"},
    ]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "OVERLAPPING_SEGMENTS"
    # 修订号没有被消耗，下一批仍是 2
    r2 = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E2", "cost_center": "CC-Z",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
    ]})
    assert r2.status_code == 201
    assert r2.json()["revision_id"] == 2


def test_adjacent_segments_allowed(client):
    # 首尾相接（半开）不算重叠
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-02-01", "effective_to": "2026-03-01"},
    ]})
    assert r.status_code == 201


def test_overlap_check_is_per_employee(client):
    # 不同员工同一日期区间不构成重叠
    r = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
        {"employee_id": "E2", "cost_center": "CC-B",
         "effective_from": "2026-01-01", "effective_to": "2026-03-01"},
    ]})
    assert r.status_code == 201


# ---------- 归属查询：左闭右开、空档、未归属 ----------

def test_half_open_boundary_and_gap(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        # 2 月整月空档
        {"employee_id": "E1", "cost_center": "CC-B",
         "effective_from": "2026-03-01", "effective_to": "2026-04-01"},
    ]})
    r = client.post("/api/attribution/query", json={
        "revision_id": 1,
        "records": [
            {"employee_id": "E1", "work_date": "2026-01-01", "hours": 8},   # 左端点含
            {"employee_id": "E1", "work_date": "2026-01-31", "hours": 4},   # 区间内
            {"employee_id": "E1", "work_date": "2026-02-01", "hours": 8},   # 右端点不含 + 空档
            {"employee_id": "E1", "work_date": "2026-03-01", "hours": 8},   # 下一段左端点
            {"employee_id": "E9", "work_date": "2026-01-01", "hours": 8},   # 未知员工
        ],
    })
    assert r.status_code == 200
    results = r.json()
    assert results[0]["cost_center"] == "CC-A" and results[0]["attributed"] is True
    assert results[1]["cost_center"] == "CC-A"
    assert results[2]["cost_center"] is None and results[2]["attributed"] is False
    assert results[3]["cost_center"] == "CC-B"
    assert results[4]["cost_center"] is None


# ---------- 核心：旧修订口径不可变 ----------

def test_old_revision_result_immutable_after_later_backfill(client):
    # 修订 1：E1 全年归属 CC-OLD
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-OLD",
         "effective_from": "2026-01-01", "effective_to": "2027-01-01"}
    ]})
    body = {
        "revision_id": 1,
        "records": [{"employee_id": "E1", "work_date": "2026-05-15", "hours": 8}],
    }
    before = client.post("/api/attribution/query", json=body).json()
    assert before[0]["cost_center"] == "CC-OLD"

    # 修订 2：跨段补录，覆盖 4~6 月（与旧区间跨批重叠是允许的，批内才禁止）
    r2 = client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-NEW",
         "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
    ]})
    assert r2.json()["revision_id"] == 2

    # 旧修订 1 的查询结果保持不变（月结口径冻结）
    after = client.post("/api/attribution/query", json=body).json()
    assert after == before
    assert after[0]["cost_center"] == "CC-OLD"
    assert after[0]["matched_revision_id"] == 1

    # 新修订 2 下同一天命中新成本中心，区间外仍保留旧归属
    q2 = client.post("/api/attribution/query", json={
        "revision_id": 2,
        "records": [
            {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
            {"employee_id": "E1", "work_date": "2026-03-15", "hours": 8},
        ],
    }).json()
    assert q2[0]["cost_center"] == "CC-NEW" and q2[0]["matched_revision_id"] == 2
    assert q2[1]["cost_center"] == "CC-OLD" and q2[1]["matched_revision_id"] == 1


def test_revision_zero_is_empty_caliber(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"}
    ]})
    r = client.post("/api/attribution/query", json={
        "revision_id": 0,
        "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
    })
    assert r.status_code == 200
    assert r.json()[0]["cost_center"] is None


def test_query_unknown_revision_404(client):
    r = client.post("/api/attribution/query", json={
        "revision_id": 99,
        "records": [{"employee_id": "E1", "work_date": "2026-01-15", "hours": 8}],
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REVISION_NOT_FOUND"


def test_revision_segments_listing(client):
    client.post("/api/revisions", json={"entries": [
        {"employee_id": "E1", "cost_center": "CC-A",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
        {"employee_id": "E2", "cost_center": "CC-B",
         "effective_from": "2026-01-01", "effective_to": "2026-02-01"},
    ]}, )
    r = client.post("/api/revisions/1/segments", json={"employee_id": "E1"})
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["cost_center"] == "CC-A"

    assert client.post("/api/revisions/99/segments", json={}).status_code == 404
