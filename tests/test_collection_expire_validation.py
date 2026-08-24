# 验证收件箱三码过期时间的跨单位限制：
# 收件箱非永久时，投件码/取件码不可为永久，也不可超出收件箱的时间上限。
# 整箱过期禁用 count（存活期应有时间维度）；子码为 count（按次数）时无时间维度，不与收件箱时间比较直接放行
import pytest


async def _create_box(client, **overrides):
    """创建收件箱，默认：收件箱 3 天 / 投件码 50 小时 / 取件码 1 天"""
    payload = {
        "title": "测试箱",
        "description": "",
        "max_files": 5,
        "expire_style": "day",
        "expire_value": 3,
        "delivery_expire_style": "hour",
        "delivery_expire_value": 50,
        "retrieve_expire_style": "day",
        "retrieve_expire_value": 1,
    }
    payload.update(overrides)
    return await client.post("/collection/create/", json=payload)


async def test_same_unit_comparison(client):
    """单位相同（小时 vs 天）：50 小时未超 3 天应通过，100 小时超限应拒绝。"""

    # 通过用例：50h = 180000s < 3d = 259200s
    resp = await _create_box(client)
    assert resp.status_code == 200, resp.text

    # 拒绝用例：100h = 360000s > 3d
    resp = await _create_box(client, delivery_expire_value=100)
    assert resp.status_code == 400
    assert "投件码过期时间不能超过收件箱过期时间" in resp.text


async def test_forever_exceeds_collection(client):
    """收件箱 3 天（非永久）时，投件码/取件码设为永久应被拒绝。"""

    resp = await _create_box(client, delivery_expire_style="forever")
    assert resp.status_code == 400
    assert "投件码过期时间不能超过收件箱过期时间" in resp.text

    resp = await _create_box(client, retrieve_expire_style="forever")
    assert resp.status_code == 400
    assert "取件码过期时间不能超过收件箱过期时间" in resp.text


async def test_count_semantics(client):
    """整箱过期禁用 count（存活期应有时间维度）；子码为 count（按次数）无时间维度，不与收件箱比较直接放行。"""

    # 整箱 count → 拒绝
    resp = await _create_box(client, expire_style="count", expire_value=5)
    assert resp.status_code == 400
    assert "收件箱过期时间不支持按次数" in resp.text

    # 收件箱 3 天时，子码投件/取件设 count → 放行（次数用尽即过期）
    resp = await _create_box(client, delivery_expire_style="count", delivery_expire_value=5)
    assert resp.status_code == 200, resp.text
    resp = await _create_box(client, retrieve_expire_style="count", retrieve_expire_value=3)
    assert resp.status_code == 200, resp.text


async def test_forever_collection_allows_forever_subs(client):
    """收件箱为永久时，投件码/取件码可为永久或任意时长（不受子码约束）。"""

    resp = await _create_box(
        client,
        expire_style="forever", expire_value=0,
        delivery_expire_style="forever", delivery_expire_value=0,
        retrieve_expire_style="forever", retrieve_expire_value=0,
    )
    assert resp.status_code == 200, resp.text


async def test_update_rejects_exceeding_values(client):
    """更新配置：投件码改为超过收件箱时长（含永久/count）应被拒绝。"""

    resp = await _create_box(client)  # 箱 3 天 / 投件 50h / 取件 1d
    assert resp.status_code == 200, resp.text
    collection_code = resp.json()["detail"]["collection_code"]

    # 超限：100 小时
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "hour", "delivery_expire_value": 100},
    )
    assert resp.status_code == 400
    assert "投件码过期时间不能超过收件箱过期时间" in resp.text

    # 永久（旧 bug：此前被跳过校验）
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "forever", "delivery_expire_value": 0},
    )
    assert resp.status_code == 400

    # count：按次数无时间维度，不与收件箱时间比较，直接放行
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "count", "delivery_expire_value": 3},
    )
    assert resp.status_code == 200, resp.text

    # 合法：48 小时（未超 3 天）
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "hour", "delivery_expire_value": 48},
    )
    assert resp.status_code == 200, resp.text


async def test_update_remaining_time_boundary(client):
    """收件箱已过一段时间（剩余时长 < 子码时长）时，子码不应超出收件箱绝对过期时间。"""
    from datetime import timedelta
    from apps.base.models import CollectionBox
    from core.utils import get_now

    resp = await _create_box(client)  # 箱 3 天 / 投件 50h / 取件 1d
    assert resp.status_code == 200, resp.text
    collection_code = resp.json()["detail"]["collection_code"]

    # 模拟时间流逝：收件箱只剩 1 天
    box = await CollectionBox.filter(collection_code=collection_code).first()
    box.expired_at = await get_now() + timedelta(days=1)
    await box.save()

    # 投件码设 2 天：总时长(2d) 未超箱总时长(3d)，但绝对过期时间超出收件箱剩余时间 → 应拒绝
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "day", "delivery_expire_value": 2},
    )
    assert resp.status_code == 400
    assert "投件码过期时间不能超过收件箱过期时间" in resp.text

    # 投件码设 12 小时：明显小于收件箱剩余时间（1 天）→ 允许
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "hour", "delivery_expire_value": 12},
    )
    assert resp.status_code == 200, resp.text


async def test_update_count_sub_allowed_even_after_elapse(client):
    """收件箱已过一段时间后，子码设为 count（按次数）仍放行（无时间维度，不比较收件箱剩余时间）。"""
    from datetime import timedelta
    from apps.base.models import CollectionBox
    from core.utils import get_now

    resp = await _create_box(client)  # 箱 3 天 / 投件 50h / 取件 1d
    assert resp.status_code == 200, resp.text
    collection_code = resp.json()["detail"]["collection_code"]

    # 模拟时间流逝：收件箱只剩 1 天
    box = await CollectionBox.filter(collection_code=collection_code).first()
    box.expired_at = await get_now() + timedelta(days=1)
    await box.save()

    # 子码 count：按次数，不与收件箱剩余时间比较 → 放行
    resp = await client.patch(
        f"/collection/update/{collection_code}",
        json={"delivery_expire_style": "count", "delivery_expire_value": 3},
    )
    assert resp.status_code == 200, resp.text
