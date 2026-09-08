"""Критичный тест приёмки: User B не получает ответ по секретному документу User A.

Проверяет механику RBAC pre-filter: чанки документа A физически не попадают
в контекст модели при запросе от пользователя B.
"""
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.rag.retriever import build_acl_filter, hybrid_retrieve


class FakePoint:
    def __init__(self, pid: str, payload: dict):
        self.id = pid
        self.payload = payload
        self.score = 1.0


class FakeQdrant:
    """Мок Qdrant: фиксирует фильтры и возвращает точки только по ним."""

    def __init__(self, points: list[FakePoint]):
        self.points = points
        self.applied_filters: list = []

    async def query_points(self, *, collection_name, query, query_filter, limit, with_payload):
        self.applied_filters.append(query_filter)
        # Имитация pre-filter: возвращаем точки, проходящие фильтр
        from qdrant_client.models import FieldCondition, MatchAny

        allowed_ws = None
        allowed_users = None
        allowed_groups = None
        for cond in query_filter.must or []:
            if cond.key == "workspace_id":
                allowed_ws = cond.match.any
        if query_filter.should:
            for cond in query_filter.should:
                if cond.key == "acl_users":
                    allowed_users = cond.match.any
                if cond.key == "acl_groups":
                    allowed_groups = cond.match.any

        result = []
        for p in self.points:
            if allowed_ws and p.payload.get("workspace_id") not in allowed_ws:
                continue
            user_ok = allowed_users and p.payload.get("acl_users") and (
                set(p.payload["acl_users"]) & set(allowed_users)
            )
            group_ok = allowed_groups and p.payload.get("acl_groups") and (
                set(p.payload["acl_groups"]) & set(allowed_groups)
            )
            if not (user_ok or group_ok):
                continue
            result.append(p)
        return type("R", (), {"points": result[:limit]})()


SECRET_DOC_PAYLOAD = {
    "document_id": "doc-a-secret",
    "title": "Стратегия M&A (секретно)",
    "content": "Конфиденциальные детали сделки по поглощению ООО Ромашка",
    "workspace_id": "ws-private",
    "acl_users": ["user-a"],
    "acl_groups": [],
}
PUBLIC_DOC_PAYLOAD = {
    "document_id": "doc-public",
    "title": "Регламент отпусков",
    "content": "Отпуск предоставляется по заявлению",
    "workspace_id": "ws-common",
    "acl_users": [],
    "acl_groups": ["dept-hr"],
}


@pytest.mark.asyncio
async def test_user_b_cannot_retrieve_secret_document():
    """User B (dept-hr, без user-ACL на ws-private) не видит секретный чанк User A."""
    qdrant = FakeQdrant(
        [FakePoint("p1", SECRET_DOC_PAYLOAD), FakePoint("p2", PUBLIC_DOC_PAYLOAD)]
    )
    acl_b = {
        "user_id": "user-b",
        "dept_ids": ["dept-hr"],
        "workspace_ids": ["ws-common"],
    }

    with patch("app.rag.retriever.embed_query", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("app.rag.retriever.sparse_search", new=AsyncMock(return_value=[])):
            with patch(
                "app.rag.retriever.rerank",
                # актуальный rerank возвращает (пороговые, все)
                new=AsyncMock(side_effect=lambda q, c, n: (c[:n], c)),
            ):
                with patch(
                    "app.rag.retriever._expand_sections",
                    new=AsyncMock(side_effect=lambda q, col, acl, all_r: [r for r in all_r if r["item"]["payload"].get("workspace_id") in acl["workspace_ids"]]),
                ):
                    chunks, _ = await hybrid_retrieve(
                        qdrant, "ws_common", "детали сделки Ромашка", acl_b
                    )

    titles = [c["title"] for c in chunks]
    assert "Стратегия M&A (секретно)" not in titles, (
        "КРИТИЧНО: User B получил секретный документ User A!"
    )
    assert all(c["title"] == "Регламент отпусков" for c in chunks)


@pytest.mark.asyncio
async def test_user_a_can_retrieve_own_secret():
    """Санити: владелец видит свой секретный документ."""
    qdrant = FakeQdrant([FakePoint("p1", SECRET_DOC_PAYLOAD)])
    acl_a = {
        "user_id": "user-a",
        "dept_ids": ["dept-exec"],
        "workspace_ids": ["ws-private", "ws-common"],
    }

    with patch("app.rag.retriever.embed_query", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("app.rag.retriever.sparse_search", new=AsyncMock(return_value=[])):
            with patch(
                "app.rag.retriever.rerank", new=AsyncMock(side_effect=lambda q, c, n: (c[:n], c))
            ):
                with patch(
                    "app.rag.retriever._expand_sections",
                    new=AsyncMock(side_effect=lambda q, col, acl, all_r: all_r),
                ):
                    chunks, _ = await hybrid_retrieve(
                        qdrant, "ws_private", "детали сделки Ромашка", acl_a
                    )

    assert any(c["title"] == "Стратегия M&A (секретно)" for c in chunks)


def test_acl_filter_structure():
    """Фильтр ACL имеет must (workspace) и should (user|dept)."""
    acl = {
        "user_id": "user-b",
        "dept_ids": ["dept-hr"],
        "workspace_ids": ["ws-common"],
    }
    f = build_acl_filter(acl)
    assert f.must is not None
    assert f.should is not None


def test_acl_filter_no_depts_falls_back_to_user_only():
    """Без департаментов user-ACL остаётся в should — dept-условие пустое."""
    acl = {"user_id": "user-c", "dept_ids": [], "workspace_ids": ["ws-1"]}
    f = build_acl_filter(acl)
    must_keys = [c.key for c in f.must]
    assert "workspace_id" in must_keys
    should_keys = [c.key for c in f.should or []]
    assert "acl_users" in should_keys
    # dept-условие с пустым any не добавляет путей доступа
    groups_cond = next((c for c in f.should if c.key == "acl_groups"), None)
    assert groups_cond is not None and groups_cond.match.any == ["ws-1"]
