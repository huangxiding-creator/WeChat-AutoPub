"""CGI 快车道（模式B）：浏览器 cookie 同步 → requests 直调公众号 Web 接口。

职责边界（保守仲裁策略）：
- 读操作（草稿列表 / 发表记录列表）→ CGI 直调（快 10~50 倍，零风险）
- 写操作（发布）→ 始终浏览器模式（URL 跳转验证最可靠，红线安全）
所有请求先过安全守卫（assert_url_safe），任何失败回退浏览器解析。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from ..browser.safety import assert_url_safe
from ..constants import MP_BASE_URL
from ..browser.session import BrowserSession

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class CGIClient:
    """用浏览器登录态直调 Web CGI（只读）。"""

    def __init__(self, session: BrowserSession, token: str) -> None:
        if not token:
            raise ValueError("token 为空，无法调 CGI")
        self._token = token
        self._http = requests.Session()
        self._http.headers.update({"User-Agent": _UA, "Referer": MP_BASE_URL})
        self._sync_cookies(session)

    def _sync_cookies(self, session: BrowserSession) -> None:
        """把浏览器 cookie 同步进 requests session。"""
        try:
            cookies = session.tab.cookies(all_domains=True) \
                if hasattr(session.tab, "cookies") else session.tab.cookies()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"读取浏览器 cookie 失败: {exc}") from exc
        count = 0
        for c in cookies:
            try:
                self._http.cookies.set(
                    c.get("name", ""),
                    c.get("value", ""),
                    domain=c.get("domain", "").lstrip("."),
                    path=c.get("path", "/"),
                )
                count += 1
            except Exception:  # noqa: BLE001
                continue
        logger.info("cookie 同步完成（%d 条）", count)
        if count == 0:
            raise RuntimeError("未同步到任何 cookie")

    def _get(self, path: str, params: dict[str, Any]) -> Optional[dict]:
        url = f"{MP_BASE_URL}{path}"
        assert_url_safe(url)                       # 🛡 只读接口也过守卫
        full = dict(params)
        full["token"] = self._token
        try:
            resp = self._http.get(url, params=full, timeout=_TIMEOUT)
            if resp.status_code != 200:
                logger.warning("CGI %s HTTP %d", path, resp.status_code)
                return None
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("CGI %s 失败: %s", path, exc)
            return None

    # —— 只读接口 ——

    def list_drafts(self, begin: int = 0, count: int = 10) -> Optional[dict]:
        """草稿列表（appmsg?action=list_ex）。失败返回 None → 上层回退浏览器。"""
        return self._get("/cgi-bin/appmsg", {
            "action": "list_ex",
            "begin": begin,
            "count": count,
            "type": 10,               # 10 = 图文素材
            "t": "media/appmsg_list",
        })

    def list_publish_records(self, begin: int = 0, count: int = 10) -> Optional[dict]:
        """发表记录列表（appmsgpublish?sub=list）。失败返回 None。"""
        return self._get("/cgi-bin/appmsgpublish", {
            "sub": "list",
            "begin": begin,
            "count": count,
        })
