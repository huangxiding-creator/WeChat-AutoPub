"""INI 配置加载（中文键名）→ 不可变 AppConfig。

规则：
- 配置全部来自 config.ini（中文键名），零硬编码
- 加载即校验（间隔上下限、模型白名单、数值范围），非法直接抛异常
- frozen dataclass，全程序只读共享
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path

from .constants import ALLOWED_FREE_MODELS, PROJECT_ROOT


class ConfigError(ValueError):
    """配置非法。"""


@dataclass(frozen=True)
class AccountConfig:
    单账号单日最大发布数: int = 20
    登录等待扫码超时分钟: int = 30
    开启新账号扫码窗口: bool = False


@dataclass(frozen=True)
class DraftConfig:
    发布最近天数: int = 3
    每篇间隔最小秒: int = 90
    每篇间隔最大秒: int = 150
    贴图间隔最小秒: int = 5       # 2026-09-02 用户明令：贴图间隔 5 秒左右
    贴图间隔最大秒: int = 10
    选择弹窗等待秒: int = 25     # 自复盘可调（界 12~40，实测弹窗 5~25s 慢加载）
    安静期秒: int = 8            # 自复盘可调（界 5~15，换屏空窗实测 2~5s）
    空轮重试上限: int = 2        # 自复盘可调（界 1~3，真空轮解析重试次数）
    贴图渲染宽限秒: int = 8      # 贴图tab XHR 异步渲染：切tab后轮询等卡片上限（界 3~15）


@dataclass(frozen=True)
class PicPostConfig:
    翻页数: int = 5
    草稿加载等待最长分钟: int = 6


@dataclass(frozen=True)
class ScheduleConfig:
    启用: bool = True
    运行时间: str = "09:00"
    最晚运行时间: str = "12:00"      # 每天 [运行时间, 最晚运行时间] 窗口内随机启动
    错过补跑: bool = True
    仅工作日: bool = False


@dataclass(frozen=True)
class EngineConfig:
    优先模式: str = "自动"          # 自动 / 浏览器 / 接口
    反检测: bool = True


@dataclass(frozen=True)
class LLMConfig:
    智谱Key: str = ""
    免费模型: tuple[str, ...] = field(default=("glm-4-flashx", "glm-4-flash"))


@dataclass(frozen=True)
class NotifyConfig:
    企微Webhook: str = ""
    通知开关: bool = True


@dataclass(frozen=True)
class RetroConfig:
    开关: bool = True            # 每日收官后自动自复盘
    观察天数: int = 3            # 自调参参考的历史天数
    报告目录: str = "data/retro" # 复盘报告与趋势数据落盘位置
    告警阈值分: int = 80         # 四维总分低于此值发企微预警（0=关闭）


@dataclass(frozen=True)
class BrowserConfig:
    Profile根目录: str = "data/browser_profiles"
    浏览器路径: str = ""              # 留空自动探测 Chrome/Edge
    无头模式: bool = False            # 需扫码场景必须可见
    运行结束关闭浏览器: bool = True   # 收工收口：只关本工具 profile 的浏览器


@dataclass(frozen=True)
class AppConfig:
    账号: AccountConfig = field(default_factory=AccountConfig)
    草稿: DraftConfig = field(default_factory=DraftConfig)
    贴图: PicPostConfig = field(default_factory=PicPostConfig)
    定时: ScheduleConfig = field(default_factory=ScheduleConfig)
    引擎: EngineConfig = field(default_factory=EngineConfig)
    大模型: LLMConfig = field(default_factory=LLMConfig)
    通知: NotifyConfig = field(default_factory=NotifyConfig)
    浏览器: BrowserConfig = field(default_factory=BrowserConfig)
    复盘: RetroConfig = field(default_factory=RetroConfig)

    @property
    def profile_root(self) -> Path:
        return _resolve_path(self.浏览器.Profile根目录)


def _resolve_path(raw: str) -> Path:
    p = Path(raw)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _get_str(cp: configparser.ConfigParser, section: str, key: str, default: str) -> str:
    try:
        val = cp.get(section, key)
        return val.strip()
    except (configparser.NoSectionError, configparser.NoOptionError, KeyError):
        return default


def _get_int(cp: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    raw = _get_str(cp, section, key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"[{section}] {key} = {raw!r} 不是整数") from exc


def _get_bool(cp: configparser.ConfigParser, section: str, key: str, default: bool) -> bool:
    raw = _get_str(cp, section, key, "是" if default else "否")
    if raw in ("是", "true", "True", "1", "yes", "on"):
        return True
    if raw in ("否", "false", "False", "0", "no", "off"):
        return False
    raise ConfigError(f"[{section}] {key} = {raw!r} 无法识别（应为 是/否）")


def _bounds(value: int, lo: int, hi: int, label: str) -> None:
    """自复盘可调参数的硬边界（护栏：任何自动调整都不得越界）。"""
    if not (lo <= value <= hi):
        raise ConfigError(f"{label} 越界：{value}（允许 {lo}~{hi}）")


def _validate(cfg: AppConfig) -> None:
    d = cfg.草稿
    if not (0 <= d.每篇间隔最小秒 <= d.每篇间隔最大秒):
        raise ConfigError(
            f"[草稿] 间隔非法：最小 {d.每篇间隔最小秒}s > 最大 {d.每篇间隔最大秒}s"
        )
    if not (1 <= d.贴图间隔最小秒 <= d.贴图间隔最大秒 <= 600):
        raise ConfigError(
            f"[草稿] 贴图间隔非法：{d.贴图间隔最小秒}~{d.贴图间隔最大秒}s"
            "（须 1 ≤ 最小 ≤ 最大 ≤ 600）"
        )
    _bounds(d.选择弹窗等待秒, 12, 40, "[草稿] 选择弹窗等待秒")
    _bounds(d.安静期秒, 5, 15, "[草稿] 安静期秒")
    _bounds(d.空轮重试上限, 1, 3, "[草稿] 空轮重试上限")
    _bounds(d.贴图渲染宽限秒, 3, 15, "[草稿] 贴图渲染宽限秒")
    _bounds(cfg.复盘.观察天数, 1, 14, "[复盘] 观察天数")
    _bounds(cfg.复盘.告警阈值分, 0, 100, "[复盘] 告警阈值分")
    if d.发布最近天数 < 1:
        raise ConfigError("[草稿] 发布最近天数 至少为 1")
    if cfg.贴图.翻页数 < 1:
        raise ConfigError("[贴图] 翻页数 至少为 1")
    if cfg.贴图.草稿加载等待最长分钟 < 1:
        raise ConfigError("[贴图] 草稿加载等待最长分钟 至少为 1")
    if cfg.账号.单账号单日最大发布数 < 1:
        raise ConfigError("[账号] 单账号单日最大发布数 至少为 1")

    bad = [m for m in cfg.大模型.免费模型 if m not in ALLOWED_FREE_MODELS]
    if bad:
        raise ConfigError(
            f"[大模型] 免费模型含非白名单项 {bad}；允许：{sorted(ALLOWED_FREE_MODELS)}"
            "（红线：杜绝任何收费模型）"
        )

    def _minutes(spec: str, label: str) -> int:
        hh, _, mm = spec.partition(":")
        try:
            h, m = int(hh), int(mm)
        except ValueError as exc:
            raise ConfigError(f"[定时] {label} {spec!r} 非法（应为 HH:MM）") from exc
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ConfigError(f"[定时] {label} {spec!r} 超出范围")
        return h * 60 + m

    start = _minutes(cfg.定时.运行时间, "运行时间")
    end = _minutes(cfg.定时.最晚运行时间, "最晚运行时间")
    if start >= end:
        raise ConfigError(
            f"[定时] 最晚运行时间 {cfg.定时.最晚运行时间!r} 必须晚于 "
            f"运行时间 {cfg.定时.运行时间!r}")

    if cfg.引擎.优先模式 not in ("自动", "浏览器", "接口"):
        raise ConfigError("[引擎] 优先模式 仅支持 自动/浏览器/接口")


def load_config(path: Path | str | None = None) -> AppConfig:
    """读取 INI → 校验 → 返回不可变配置。文件不存在时用默认值。"""
    cfg_path = Path(path) if path else (PROJECT_ROOT / "config.ini")
    cp = configparser.ConfigParser(interpolation=None)
    if cfg_path.exists():
        cp.read(cfg_path, encoding="utf-8-sig")

    models_raw = _get_str(cp, "大模型", "免费模型", "glm-4-flashx,glm-4-flash")
    models = tuple(m.strip() for m in models_raw.split(",") if m.strip())

    cfg = AppConfig(
        账号=AccountConfig(
            单账号单日最大发布数=_get_int(cp, "账号", "单账号单日最大发布数", 20),
            登录等待扫码超时分钟=_get_int(cp, "账号", "登录等待扫码超时分钟", 30),
            开启新账号扫码窗口=_get_bool(cp, "账号", "开启新账号扫码窗口", False),
        ),
        草稿=DraftConfig(
            发布最近天数=_get_int(cp, "草稿", "发布最近天数", 3),
            每篇间隔最小秒=_get_int(cp, "草稿", "每篇间隔最小秒", 90),
            每篇间隔最大秒=_get_int(cp, "草稿", "每篇间隔最大秒", 150),
            贴图间隔最小秒=_get_int(cp, "草稿", "贴图间隔最小秒", 5),
            贴图间隔最大秒=_get_int(cp, "草稿", "贴图间隔最大秒", 10),
            选择弹窗等待秒=_get_int(cp, "草稿", "选择弹窗等待秒", 25),
            安静期秒=_get_int(cp, "草稿", "安静期秒", 8),
            空轮重试上限=_get_int(cp, "草稿", "空轮重试上限", 2),
            贴图渲染宽限秒=_get_int(cp, "草稿", "贴图渲染宽限秒", 8),
        ),
        贴图=PicPostConfig(
            翻页数=_get_int(cp, "贴图", "翻页数", 5),
            草稿加载等待最长分钟=_get_int(cp, "贴图", "草稿加载等待最长分钟", 6),
        ),
        定时=ScheduleConfig(
            启用=_get_bool(cp, "定时", "启用", True),
            运行时间=_get_str(cp, "定时", "运行时间", "09:00"),
            最晚运行时间=_get_str(cp, "定时", "最晚运行时间", "12:00"),
            错过补跑=_get_bool(cp, "定时", "错过补跑", True),
            仅工作日=_get_bool(cp, "定时", "仅工作日", False),
        ),
        引擎=EngineConfig(
            优先模式=_get_str(cp, "引擎", "优先模式", "自动"),
            反检测=_get_bool(cp, "引擎", "反检测", True),
        ),
        大模型=LLMConfig(
            智谱Key=_get_str(cp, "大模型", "智谱Key", ""),
            免费模型=models,
        ),
        通知=NotifyConfig(
            企微Webhook=_get_str(cp, "通知", "企微Webhook", ""),
            通知开关=_get_bool(cp, "通知", "通知开关", True),
        ),
        浏览器=BrowserConfig(
            Profile根目录=_get_str(cp, "浏览器", "Profile根目录", "data/browser_profiles"),
            浏览器路径=_get_str(cp, "浏览器", "浏览器路径", ""),
            无头模式=_get_bool(cp, "浏览器", "无头模式", False),
            运行结束关闭浏览器=_get_bool(cp, "浏览器", "运行结束关闭浏览器", True),
        ),
        复盘=RetroConfig(
            开关=_get_bool(cp, "复盘", "开关", True),
            观察天数=_get_int(cp, "复盘", "观察天数", 3),
            报告目录=_get_str(cp, "复盘", "报告目录", "data/retro"),
            告警阈值分=_get_int(cp, "复盘", "告警阈值分", 80),
        ),
    )
    _validate(cfg)
    return cfg
