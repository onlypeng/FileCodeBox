# 管理后台认证：登录 / Token 校验 / 未授权拦截
from core.settings import settings


async def test_login_success(client):
    """默认口令登录成功，返回 JWT token。"""
    resp = await client.post(
        "/admin/login", json={"password": settings.admin_token}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert body["detail"]["token"]
    assert body["detail"]["token_type"] == "Bearer"


async def test_login_wrong_password(client):
    """错误口令返回 401。"""
    resp = await client.post("/admin/login", json={"password": "wrong-password"})
    assert resp.status_code == 401


async def test_verify_valid_token(client, admin_token):
    """有效 Token 通过 /admin/verify 校验。"""
    resp = await client.get("/admin/verify", headers={"Authorization": f"Bearer {admin_token}"})
    assert resp.status_code == 200
    assert resp.json()["detail"]["valid"] is True


async def test_verify_invalid_token(client):
    """伪造/过期 Token 返回 401。"""
    resp = await client.get(
        "/admin/verify", headers={"Authorization": "Bearer bad.token.here"}
    )
    assert resp.status_code == 401


async def test_admin_endpoints_require_auth(client):
    """未携带 Token 访问管理接口返回 401（dashboard / 配置 / 列表）。"""
    for path in ["/admin/dashboard", "/admin/config/get", "/admin/file/list", "/admin/collection/list", "/admin/room/list"]:
        resp = await client.get(path)
        assert resp.status_code == 401, path


async def test_login_audit_no_password_leak(client, caplog):
    """登录失败日志记录 IP，不记录口令。"""
    import logging
    with caplog.at_level(logging.WARNING, logger="apps.admin.views"):
        await client.post("/admin/login", json={"password": "secret-12345"})
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "secret-12345" not in joined
