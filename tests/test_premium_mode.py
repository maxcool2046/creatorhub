"""精品模式：库迁移、跳过自动分镜、API 闸口。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.content_api as content_api
from app.content_pipeline import ContentIndexStore, ContentPipeline, PipelineError


def _asset(source_id: str = "v001") -> dict:
    return {
        "source_id": source_id,
        "author": "tester",
        "source_title": "进气温度传感器",
        "source_path": f"/tmp/{source_id}.mp4",
        "transcript_path": f"/tmp/{source_id}/transcript.txt",
        "sha256": "a" * 64,
        "size_bytes": 12,
        "mtime": 1,
    }


def test_alter_adds_task_mode_and_premium_tables(tmp_path: Path):
    """旧库无列时，初始化必须补列并建精品表。"""
    db = tmp_path / "old.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE assets (
                source_id TEXT PRIMARY KEY,
                author TEXT NOT NULL,
                source_title TEXT NOT NULL,
                source_path TEXT NOT NULL,
                transcript_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime INTEGER NOT NULL,
                metadata_json TEXT,
                metadata_model TEXT,
                metadata_confirmed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'indexed',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE video_jobs (
                source_id TEXT PRIMARY KEY,
                plan_json TEXT NOT NULL,
                plan_model TEXT NOT NULL,
                quality TEXT NOT NULL DEFAULT 'preview',
                status TEXT NOT NULL DEFAULT 'planned',
                progress INTEGER NOT NULL DEFAULT 0,
                estimated_cost_cny REAL NOT NULL DEFAULT 0,
                quote_id TEXT NOT NULL DEFAULT '',
                quote_model TEXT NOT NULL DEFAULT '',
                generation_id TEXT NOT NULL DEFAULT '',
                output_path TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT INTO assets (
                source_id, author, source_title, source_path, transcript_path,
                sha256, size_bytes, mtime, updated_at
            ) VALUES ('v001', 'a', 't', '/a.mp4', '/t.txt', 'h', 1, 1, 'now')
            """
        )
    store = ContentIndexStore(db)
    asset = store.get_asset("v001")
    assert asset is not None
    assert asset.get("task_mode") == "batch"
    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        video_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(video_jobs)").fetchall()
        }
    assert "premium_script" in tables
    assert "premium_asset" in tables
    assert "premium_revision" in tables
    assert "premium_status" in video_cols


def test_mode_upsert_preserves_task_mode(tmp_path: Path):
    """扫描 upsert 不得冲掉已落库的 task_mode。"""
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset())
    store.set_task_mode("v001", "premium")
    store.upsert_asset(_asset())
    assert store.get_asset("v001")["task_mode"] == "premium"


def test_skip_auto_plan_when_premium(tmp_path: Path, monkeypatch):
    """精品素材两处自动分镜都不调用 video_planner.create。"""
    source_id = "7580322665321700999"
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset(source_id))
    store.set_task_mode(source_id, "premium")
    store.save_metadata(
        source_id,
        {
            "title": "t",
            "summary": "s",
            "content_type": "故障讲解",
            "make": "",
            "model": "",
            "year_start": None,
            "year_end": None,
            "topic": "诊断",
            "confidence": 0.9,
            "evidence": ["a"],
            "review_required": True,
        },
        "deepseek",
        confirmed=False,
    )
    from app.content_pipeline import PLATFORMS

    store.save_drafts(
        source_id,
        {
            platform: {
                "title": f"{platform}-t",
                "body": f"{platform}-body",
                "hashtags": ["a"],
                "language": "zh-CN",
                "notes": "",
            }
            for platform in PLATFORMS
        },
        "deepseek",
    )

    class Planner:
        def __init__(self):
            self.calls = 0

        def create(self, metadata, transcript):
            self.calls += 1
            raise AssertionError("精品不得自动创建分镜")

    planner = Planner()

    class Service:
        def analyze(self, item):
            raise AssertionError("不应重新分析")

        def make_drafts(self, metadata, transcript):
            raise AssertionError("不应重写草稿")

    share = tmp_path / "share" / "tester"
    inbox = tmp_path / "inbox" / source_id
    share.mkdir(parents=True)
    inbox.mkdir(parents=True)
    (share / f"{source_id}_demo.mp4").write_bytes(b"mp4")
    (inbox / "transcript.txt").write_text("口播内容", encoding="utf-8")

    pipeline = ContentPipeline(store, Service(), planner)
    result = pipeline.run(share.parent, inbox.parent)
    assert planner.calls == 0
    job = store.get_video_job(source_id)
    assert job is None or not (job.get("plan") or {}).get("scenes")
    assert result["planned"] == 0


def _client(monkeypatch, tmp_path: Path, *, enable: bool = True) -> TestClient:
    db = tmp_path / "ci.sqlite"
    store = ContentIndexStore(db)
    store.upsert_asset(_asset())
    monkeypatch.setattr(content_api, "content_store", lambda: store)
    monkeypatch.setattr(
        content_api,
        "premium_flags",
        lambda: {
            "premium_enable": enable,
            "premium_max_inflight": 1,
            "premium_dub_sync_enable": False,
            "premium_ending_enable": False,
        },
    )
    app = FastAPI()
    app.include_router(content_api.router)
    return TestClient(app), store


def test_premium_api_disabled_returns_404(monkeypatch, tmp_path: Path):
    client, _ = _client(monkeypatch, tmp_path, enable=False)
    response = client.post("/api/content-index/video/v001/premium/start", json={})
    assert response.status_code == 404


def test_premium_api_submit_rejects_invalid_plan(monkeypatch, tmp_path: Path):
    client, store = _client(monkeypatch, tmp_path)
    store.set_task_mode("v001", "premium")
    start = client.post(
        "/api/content-index/video/v001/premium/start",
        json={"blank": True},
    )
    assert start.status_code == 200
    response = client.post(
        "/api/content-index/video/v001/premium/submit",
        json={"plan": {"rights_mode": "regenerate", "scenes": []}},
    )
    assert response.status_code == 400
    job = store.get_video_job("v001")
    assert job is None or job.get("plan_model") == "premium-draft"
    assert not (job or {}).get("plan", {}).get("scenes")


def test_premium_api_submit_planned_without_quote(monkeypatch, tmp_path: Path):
    client, store = _client(monkeypatch, tmp_path)
    store.set_task_mode("v001", "premium")
    from tests.test_content_video_pipeline import _plan

    plan = _plan()
    quoted = {"called": False}

    class Gateway:
        def quote(self, *args, **kwargs):
            quoted["called"] = True
            raise AssertionError("submit 不得报价")

        def close(self):
            return None

    monkeypatch.setattr(content_api, "_ads_media_client", Gateway)
    response = client.post(
        "/api/content-index/video/v001/premium/submit",
        json={"plan": plan, "audio_mode": "dub_after", "ending_mode": "normal"},
    )
    assert response.status_code == 200
    job = store.get_video_job("v001")
    assert job is not None
    assert job["status"] == "planned"
    assert quoted["called"] is False
    assert store.get_asset("v001")["task_mode"] == "premium"
