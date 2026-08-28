"""项目常量：URL、安全白名单、默认值。

安全红线（用户指定，代码级强制）：
- 只允许"只读 + 发表"两类动作
- URL 请求中出现删除/编辑类关键词一律拦截
"""
from __future__ import annotations

from pathlib import Path

# —— 路径 ——
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
EVIDENCE_DIR = DATA_DIR / "evidence"
RECON_DIR = DATA_DIR / "recon"
PROFILE_ROOT = DATA_DIR / "browser_profiles"
STATE_DB_PATH = DATA_DIR / "state.db"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.ini"

# —— 平台 URL ——
MP_BASE_URL = "https://mp.weixin.qq.com"
MP_HOME_URL = f"{MP_BASE_URL}/cgi-bin/home"
MP_DRAFT_LIST_URL = f"{MP_BASE_URL}/cgi-bin/appmsg?t=media/appmsg_edit_v2"
MP_PUBLISH_RECORD_URL = f"{MP_BASE_URL}/cgi-bin/appmsgpublish?sub=list"

# —— 🛡 安全红线：URL 请求路径白名单（只读 + 发表）——
# 浏览器导航与 CGI 请求仅允许命中以下模式，其余一律告警并阻断。
ALLOWED_URL_PATTERNS: tuple[str, ...] = (
    "mp.weixin.qq.com/cgi-bin/home",          # 后台首页
    "mp.weixin.qq.com/cgi-bin/appmsg",        # 草稿列表/发表记录（只读列表）
    "mp.weixin.qq.com/cgi-bin/appmsgpublish", # 发表记录列表（只读）
    "mp.weixin.qq.com/cgi-bin/freepublish",   # 发布提交
    "mp.weixin.qq.com/cgi-bin/loginpage",     # 登录页
    "mp.weixin.qq.com/",                      # 根导航与静态资源
)

# 任何命中以下关键词的 URL 绝对禁止访问（大小写不敏感）。
FORBIDDEN_URL_KEYWORDS: tuple[str, ...] = (
    "delete", "del_", "remove", "batchdel",
    "appmsg_edit", "modify", "update_appmsg", "trash",
)

# —— 🛡 安全红线：按钮文本 ——
# "发表"按钮候选文本（必须精确匹配其一，且按钮文本不得含编辑/删除字样）。
PUBLISH_BUTTON_TEXTS: tuple[str, ...] = ("发表", "发 表", "群发", "发布")
# 按钮文本中出现以下任一关键词则视为危险按钮，禁止点击。
DANGEROUS_BUTTON_KEYWORDS: tuple[str, ...] = (
    "删除", "移除", "编辑", "修改", "撤回", "放弃", "不发表",
)

# —— LLM 免费模型硬白名单（杜绝任何收费模型）——
ALLOWED_FREE_MODELS: frozenset[str] = frozenset({
    "glm-4-flashx",
    "glm-4-flash",
    "glm-4-flash-250414",
})

# —— 内容类型 ——
CONTENT_TYPE_DRAFT = "draft"       # 草稿箱文章
CONTENT_TYPE_PICPOST = "picpost"   # 自动生成贴图

# —— 登录页特征 ——
LOGIN_PAGE_MARKERS: tuple[str, ...] = ("/cgi-bin/loginpage", "登录公众平台", "扫码登录")
HOME_LOGGED_MARKER = "/cgi-bin/home?t=home"
