"""DOM 选择器集中管理（平台改版时只改这里）。

公众号后台会悄悄改版，因此每个目标都给出「候选列表」按序尝试：
- 菜单/按钮：先 CSS 类，再文本匹配
- 文本定位用 @@text()=精确 与 text:包含 两种语法
真实测试期如漂移：跑 run.py --recon 保存页面 HTML 离线分析后更新此表。
"""
from __future__ import annotations

# —— 侧栏菜单 ——
MENU_DRAFT_TEXTS: tuple[str, ...] = (
    "草稿", "草稿箱", "图文消息草稿",          # 新版叫「草稿」
)
MENU_CONTENT_GROUP_TEXTS: tuple[str, ...] = (
    "内容管理", "内容与互动",
)
MENU_PUBLISH_RECORD_TEXTS: tuple[str, ...] = (
    "发表记录", "已发表内容", "全部消息",
)

# —— 草稿箱 ——
DRAFT_CARD_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop-card",
    "css:.js_appmsg_card",
    "css:div.appmsg_card",
    "css:.draft-item",
)
DRAFT_TITLE_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop-card__title",
    "css:.appmsg_title a",
    "css:.appmsg_title",
    "css:h2.title",
    "css:.title",
)
DRAFT_TIME_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop-card__meta",
    "css:.appmsg_date",
    "css:.js_create_time",
    "css:.time",
)
# 草稿列表翻页「下一页」按钮
NEXT_PAGE_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop-pagination__next",
    "css:.weui-desktop-pagination__btn_next",
    "css:.weui-desktop-pagination a[class*=next]",
    "css:a.next",
    "text:下一页",
)

# —— 编辑器页（从草稿点开后的发表流程）——
# 精确文本优先（实战教训：css 类名模糊匹配会点错按钮）
EDITOR_PUBLISH_BUTTON_SELECTORS: tuple[str, ...] = (
    "@@tag()=button@@text()=发表",
    "css:#js_send",
    "css:.weui-desktop-btn.weui-desktop-btn_primary",
    "text:发表",
)
# 确认面板中的「发表」按钮（两步确认）
CONFIRM_PUBLISH_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop-dialog .weui-desktop-btn_primary",
    "css:.dialog_wrp .btn_primary",
    "@@tag()=button@@text()=发表",
)
# 「继续发表」二次确认（提示“此操作将直接发表”）
CONTINUE_PUBLISH_SELECTORS: tuple[str, ...] = (
    "@@tag()=button@@text()=继续发表",
    "text:继续发表",
)
# 手机安全验证（随机风控弹窗，需要人工）
SECURITY_VERIFY_MARKERS: tuple[str, ...] = (
    "安全验证", "身份验证", "手机验证", "扫码验证", "环境异常",
)
SECURITY_VERIFY_SELECTORS: tuple[str, ...] = (
    "@@tag()=button@@text()=开始验证",
    "text:验证",
)

# —— 贴图（发表记录 → 已自动生成贴图草稿 → 去查看）——
PICPOST_ENTRY_TEXTS: tuple[str, ...] = (
    "已自动生成贴图草稿", "自动生成贴图",
)
PICPOST_VIEW_LINK_TEXTS: tuple[str, ...] = (
    "去查看", "查看",
)
# 贴图编辑页「草稿加载中」弹窗标记
PICPOST_LOADING_MARKERS: tuple[str, ...] = (
    "草稿加载中", "加载中", "生成中",
)
PICPOST_PUBLISH_SELECTORS: tuple[str, ...] = (
    "@@tag()=button@@text()=发表",
    "css:.publish-btn",
    "text:发表",
)

# —— 成功标记 ——
PUBLISH_SUCCESS_MARKERS: tuple[str, ...] = (
    "已发表", "发表成功", "发布成功",
)

# —— 账号登出 ——
AVATAR_SELECTORS: tuple[str, ...] = (
    "css:.weui-desktop_account__meta",
    "css:.account_meta",
    "css:.weui-desktop_avatar",
)
LOGOUT_MENU_TEXTS: tuple[str, ...] = (
    "退出登录", "退出",
)
