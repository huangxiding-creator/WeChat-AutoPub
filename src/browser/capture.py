"""数据包侦察：listen 监听公众号 CGI 请求，落盘 JSONL 供接口复刻。

这是「浏览器自动化 → API 方式」逆向的正规路径：
1. 浏览器登录态下手动/自动走一遍发布流程
2. listen 捕获全部 mp.weixin.qq.com/cgi-bin 请求（URL+方法+POST体）
3. 从捕获记录复刻请求（cookie 同步），逐步替换浏览器操作

注意：DrissionPage 监听是 tab 级的——登录/换页后必须 retarget 到
活跃 tab（session.retarget_capture），否则整轮零捕获（实战教训）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from ..constants import RECON_DIR

logger = logging.getLogger(__name__)

_TARGETS = "mp.weixin.qq.com/cgi-bin"
# 这些接口的响应体是逆向关键数据（账号列表结构 / 发布提交回执）
_RESP_CAPTURE_KEYS = ("switchacct", "appmsgpublish", "operate_", "freepublish",
                      "get_acct_list", "get_temp_url")


class PacketCapture:
    """监听并落盘 CGI 请求数据包（挂到单个 tab）。"""

    def __init__(self, tab: Any, jsonl_path: Path | None = None) -> None:
        self._tab = tab
        self._path = jsonl_path or (RECON_DIR / "captured_requests.jsonl")
        self._resp_path = RECON_DIR / "captured_responses.jsonl"
        self._active = False

    def start(self) -> bool:
        try:
            RECON_DIR.mkdir(parents=True, exist_ok=True)
            self._tab.listen.start(_TARGETS)
            self._active = True
            logger.info("📡 数据包监听已启动（%s）", _TARGETS)
            return True
        except Exception as exc:  # noqa: BLE001 — 监听失败不影响主流程
            logger.warning("监听启动失败（继续纯浏览器模式）: %s", exc)
            return False

    def drain(self, tag: str) -> int:
        """把已捕获的数据包写入 JSONL。返回写入条数。"""
        if not self._active:
            return 0
        written = 0
        resp_written = 0
        try:
            with self._path.open("a", encoding="utf-8") as f, \
                    self._resp_path.open("a", encoding="utf-8") as rf:
                for pkt in self._tab.listen.steps(timeout=1.0):
                    try:
                        row = {
                            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "tag": tag,
                            "url": pkt.url,
                            "method": pkt.method,
                            "post_data": _safe_post_data(pkt),
                        }
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        written += 1
                        # 关键接口连响应体一起存（账号列表/发布回执的真相）
                        url = pkt.url or ""
                        if any(k in url for k in _RESP_CAPTURE_KEYS):
                            body = _safe_response_body(pkt)
                            if body:
                                rf.write(json.dumps({
                                    "ts": row["ts"], "tag": tag,
                                    "url": url[:200], "resp": body,
                                }, ensure_ascii=False) + "\n")
                                resp_written += 1
                    except Exception:  # noqa: BLE001 — 单包失败跳过
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.debug("drain 失败: %s", exc)
        if written:
            logger.info("📡 捕获 %d 个 CGI 请求（场景=%s）→ %s", written, tag, self._path)
        if resp_written:
            logger.info("📡 其中 %d 个关键接口已连响应体存档 → %s",
                        resp_written, self._resp_path)
        return written

    def stop(self) -> None:
        if self._active:
            try:
                self._tab.listen.stop()
            except Exception:  # noqa: BLE001
                pass
            self._active = False


def _safe_post_data(pkt: Any) -> str:
    """提取 POST 体（文本截断，避免巨大载荷）。"""
    try:
        req = getattr(pkt, "request", None)
        if req is None:
            return ""
        data = getattr(req, "postData", None) or getattr(req, "post_data", None) or ""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return str(data)[:3000]
    except Exception:  # noqa: BLE001
        return ""


def _safe_response_body(pkt: Any) -> str:
    """提取响应体（截断 5000 字符，接口复刻的关键数据）。"""
    try:
        resp = getattr(pkt, "response", None)
        if resp is None:
            return ""
        body = getattr(resp, "body", None) or ""
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        return str(body)[:5000]
    except Exception:  # noqa: BLE001
        return ""
