"""精品护栏：快照、降级、并发、素材。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.content_api as content_api
from app.content_pipeline import ContentIndexStore


def _asset(source_id: str = "v001") -> dict:
    return {
        "source_id": source_id,
        "author": "tester",
        "source_title": "进气温度传感器",
        "source_path": f"/tmp/{source_id}.mp4",
        "transcript_path": f"/tmp/{source_id}/transcript.txt",
        "sha256": "b" * 64,
        "size_bytes": 12,
        "mtime": 1,
    }


def test_store_revision_not_draft(tmp_path: Path):
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset())
    store.save_premium_script(
        "v001",
        fact_ledger=[{"beat_id": "B01"}],
        video_plan={"scenes": []},
        draft=True,
    )
    assert store.list_premium_revisions("v001") == []
    store.save_premium_script(
        "v001",
        fact_ledger=[{"beat_id": "B01"}],
        video_plan={"scenes": []},
        draft=False,
    )
    assert len(store.list_premium_revisions("v001")) == 1


def test_store_asset_rejects_mp4_path(tmp_path: Path):
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset())
    try:
        store.register_premium_asset(
            "v001",
            path=str(tmp_path / "clip.mp4"),
            sha256="c" * 64,
        )
        raise AssertionError("mp4 不应登记成功")
    except Exception as exc:
        assert "png/jpg" in str(exc)


def test_flags_coerce_to_safe_defaults(monkeypatch, tmp_path: Path):
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset())
    store.set_task_mode("v001", "premium")
    monkeypatch.setattr(
        content_api,
        "premium_flags",
        lambda: {
            "premium_enable": True,
            "premium_max_inflight": 1,
            "premium_dub_sync_enable": False,
            "premium_ending_enable": False,
        },
    )
    monkeypatch.setattr(content_api, "content_store", lambda: store)
    app = FastAPI()
    app.include_router(content_api.router)
    client = TestClient(app)
    response = client.post(
        "/api/content-index/video/v001/premium/save",
        json={
            "draft": True,
            "fact_ledger": [],
            "video_plan": {},
            "audio_mode": "sync_speech",
            "ending_mode": "black_fixed_text",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["audio_mode"] == "dub_after"
    assert body["ending_mode"] == "normal"
    script = store.get_premium_script("v001")
    assert script["audio_mode"] == "dub_after"
    assert script["ending_mode"] == "normal"


def test_premium_inflight_returns_429(monkeypatch, tmp_path: Path):
    from tests.test_content_video_pipeline import _plan, _quote

    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset("p1"))
    store.upsert_asset(_asset("p2"))
    store.upsert_asset(_asset("b1"))
    store.set_task_mode("p1", "premium")
    store.set_task_mode("p2", "premium")
    store.set_task_mode("b1", "batch")
    plan = _plan()
    store.save_video_plan("p1", plan, "premium-manual", estimated_cost_cny=1, premium_status="ready")
    store.save_video_plan("p2", plan, "premium-manual", estimated_cost_cny=1, premium_status="ready")
    store.save_video_plan("b1", plan, "deepseek", estimated_cost_cny=1)
    assert store.claim_video_job("p1", "preview", _quote(), premium_max_inflight=1) == "claimed"
    assert store.claim_video_job("p2", "preview", _quote(), premium_max_inflight=1) == "limited"
    assert store.claim_video_job("b1", "preview", _quote()) == "claimed"

    monkeypatch.setattr(content_api, "content_store", lambda: store)
    monkeypatch.setattr(
        content_api,
        "premium_flags",
        lambda: {
            "premium_enable": True,
            "premium_max_inflight": 1,
            "premium_dub_sync_enable": False,
            "premium_ending_enable": False,
        },
    )

    class Gateway:
        def quote(self, *args, **kwargs):
            return _quote()

        def close(self):
            return None

    monkeypatch.setattr(content_api, "_ads_media_client", Gateway)
    app = FastAPI()
    app.include_router(content_api.router)
    client = TestClient(app)
    # p1 already claimed; request another premium generate -> 429
    response = client.post(
        "/api/content-index/video/p2/generate",
        json={
            "quality": "preview",
            "confirm_cost": True,
            "quote_id": _quote()["quote_id"],
            "generation_id": _quote()["generation_id"],
        },
    )
    assert response.status_code == 429


def test_missing_asset_omits_reference_key(tmp_path: Path):
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset())
    missing = tmp_path / "gone.jpg"
    store.register_premium_asset(
        "v001",
        path=str(missing),
        sha256="d" * 64,
        scene_index=1,
    )
    assert store.reference_key_for_scene("v001", 1) == ""


def test_blank_start_skips_model(monkeypatch, tmp_path: Path):
    store = ContentIndexStore(tmp_path / "ci.sqlite")
    store.upsert_asset(_asset())
    monkeypatch.setattr(content_api, "content_store", lambda: store)
    monkeypatch.setattr(
        content_api,
        "premium_flags",
        lambda: {
            "premium_enable": True,
            "premium_max_inflight": 1,
            "premium_dub_sync_enable": False,
            "premium_ending_enable": False,
        },
    )

    class Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("空白账本不得调用模型")

    monkeypatch.setattr(content_api, "OpenAICompatibleJSONClient", Boom)
    monkeypatch.setattr(content_api, "AdsAIJSONClient", Boom)
    app = FastAPI()
    app.include_router(content_api.router)
    client = TestClient(app)
    response = client.post(
        "/api/content-index/video/v001/premium/start",
        json={"blank": True},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "blank"
    assert response.json()["premium_status"] == "editing"
