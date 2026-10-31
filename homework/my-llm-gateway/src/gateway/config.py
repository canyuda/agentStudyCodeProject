"""网关配置体系：YAML 加载、环境变量展开、启动期 fail-fast 校验。

【本模块在教学架构中的位置】
配置是整个网关的"方向盘"——供应商表、路由表、韧性参数全部由它驱动。
本模块回答三个问题：

1. 配置从哪来？ —— 三级回退：显式路径 > 环境变量 GATEWAY_CONFIG > 内置示例配置
   （保证 clone 下来不开箱即跑，教学演示零门槛）。

2. 密钥怎么保证不入库？ —— 密钥只存在于项目根目录 .env（被 .gitignore 忽略），
   YAML 里只写引用 `${DEEPSEEK_API_KEY}`，加载时才在内存中展开成真实值。
   代码、配置样例、git 历史里永远不会出现真实密钥。

3. 坏配置怎么办？ —— fail fast 原则：任何校验失败（YAML 语法错、路由引用了
   不存在的供应商、协议类型拼错……）都在启动时抛 ConfigError，进程以非零码退出，
   绝不"带病运行"。这比运行到第 N 个请求才炸要好排查得多。

教学要点：
- `${VAR}` / `${VAR:-默认值}` 展开用正则 + 回调实现，本质是一次受控的字符串替换
- 用 Pydantic 做两层校验：字段级（field_validator，如协议枚举）与跨字段级
  （model_validator，如"路由引用的供应商必须存在"）
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# 协议类型白名单：新增第三种上游协议时（如 Google Gemini），在这里注册，
# 再实现对应的适配器即可——路由表中的 protocol 字段就指向这些名字。
PROTOCOLS = ("openai_responses", "anthropic_messages")

# 路由策略白名单：priority（严格按序）与 weighted（加权轮询）
STRATEGIES = ("priority", "weighted")

# 默认可重试的上游状态码集合：超时类(408)、冲突(409)、限流(429)、服务端错误(5xx)。
# 注意：普通 4xx（如 400 参数错误）不在其中——重试同样的坏请求没有意义。
RETRYABLE_STATUSES = (408, 409, 429, 500, 502, 503, 504)


class ConfigError(Exception):
    """配置缺失、格式错误或前后不一致时抛出；调用方必须中止启动。"""


# 环境变量引用的两种形式（与 shell 语法一致，降低学习成本）：
#   ${VAR}          —— 变量必须已设置，否则报错
#   ${VAR:-默认值}   —— 变量未设置时使用默认值（.env 缺失也能开箱跑）
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_string(value: str, origin: str) -> str:
    """对单个字符串做 ${VAR} 展开。

    用 re.sub + 回调而不是字符串 split：正则一次性匹配两种形式，
    回调里决定"取环境变量 / 取默认值 / 直接报错"三种分支。
    报错信息带上配置文件路径（origin），方便定位到具体文件。
    """

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        if default is not None:
            return default
        raise ConfigError(f"{origin}: environment variable '{name}' is not set and has no default")

    return _ENV_PATTERN.sub(replace, value)


def _expand_tree(node: Any, origin: str) -> Any:
    """递归遍历整棵 YAML 树，对每个字符串值做环境变量展开。

    YAML 解析出来是嵌套的 dict/list/标量，用递归处理最自然：
    dict 逐 key 展开、list 逐项展开、str 调用 _expand_string、其余原样返回。
    """
    if isinstance(node, dict):
        return {key: _expand_tree(value, origin) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand_tree(item, origin) for item in node]
    if isinstance(node, str):
        return _expand_string(node, origin)
    return node


# --------------------------------------------------------------------------
# 配置模式（Pydantic 模型）：每一节对应 YAML 里的一个顶层键。
# Field 的 ge/le/min_length 等约束本身就是"文档 + 校验"二合一。
# --------------------------------------------------------------------------


class ServiceCfg(BaseModel):
    """服务基本信息：名称、监听地址与端口。"""

    name: str = "my-llm-gateway"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)  # 端口范围校验


class AuthCfg(BaseModel):
    """网关鉴权：调用方持有的 Bearer 密钥列表（支持多把密钥共存，便于轮换）。"""

    gateway_keys: list[str] = Field(min_length=1)  # 至少一把，否则启动失败


class DatabaseCfg(BaseModel):
    """嵌入式 SQLite 数据库路径（用量账本与提示词模板共用一个文件）。"""

    path: str = "data/gateway.db"


class RetryCfg(BaseModel):
    """指数退避重试参数（对应韧性需求的第 1 项）。

    语义消歧：max_attempts 指"同一候选路由的总尝试次数上限"（默认 3 =
    首次请求 + 最多 2 次重试），而不是"重试 3 次"。
    退避节奏：第 n 次重试前等待 base * 2^(n-1) 秒（0.25s → 0.5s → 1s…），
    封顶 max_delay_seconds，再叠加 ±jitter 比例的随机抖动——
    抖动是为了避免多个客户端在同一时刻"齐步"重试（惊群效应）。
    """

    max_attempts: int = Field(default=3, ge=1)
    base_delay_seconds: float = Field(default=0.25, gt=0)
    max_delay_seconds: float = Field(default=4.0, gt=0)
    jitter: float = Field(default=0.25, ge=0, le=1)
    retryable_statuses: list[int] = Field(default_factory=lambda: list(RETRYABLE_STATUSES))


class BreakerCfg(BaseModel):
    """按供应商熔断参数：连续失败多少次打开熔断、打开多久后放行试探。"""

    failure_threshold: int = Field(default=5, ge=1)
    cooldown_seconds: float = Field(default=30.0, gt=0)


class RateLimitCfg(BaseModel):
    """令牌桶限流参数（维度 = 调用方密钥 + 模型别名）。

    requests_per_minute：每分钟补充的令牌数（平稳速率）
    burst：桶容量，即允许的瞬时突发量
    """

    requests_per_minute: int = Field(default=120, ge=1)
    burst: int = Field(default=20, ge=1)


class StructuredCfg(BaseModel):
    """结构化输出修复闭环的最大轮次：默认 1 轮，硬上限 3 轮（防成本失控）。"""

    max_repair_rounds: int = Field(default=1, ge=0, le=3)


class CheckpointCfg(BaseModel):
    """流检查点（可选能力，默认关闭！）。

    开启会持久化"模型正文"到内存/磁盘——解决断线找回，但涉及隐私，
    必须先确定加密、权限与保留策略再打开（README 有警示）。
    max_chars 是硬上限，防止超长流把内存/存储撑爆。
    """

    enabled: bool = False
    interval_seconds: float = Field(default=1.0, gt=0)
    max_chars: int = Field(default=20_000, gt=0)


class MaskRuleCfg(BaseModel):
    """单条脱敏规则的启停开关（表驱动：加规则不改代码，只改配置）。"""

    enabled: bool = True


class MaskingCfg(BaseModel):
    """数据脱敏总开关（默认关闭）+ 各规则的独立启停。"""

    enabled: bool = False
    rules: dict[str, MaskRuleCfg] = Field(default_factory=dict)


class TimeoutCfg(BaseModel):
    """每个供应商独立的超时：连接超时（TCP 握手阶段）与总超时（整次请求）。"""

    connect_seconds: float = Field(default=5.0, gt=0)
    total_seconds: float = Field(default=120.0, gt=0)


class ProviderCfg(BaseModel):
    """一个上游供应商的完整声明。

    教学要点：protocol 字段决定该供应商由哪个适配器处理——
    "协议类型"与"供应商"解耦，是适配器模式能扩展到第三种协议的关键。
    api_key 在 YAML 里是 ${VAR} 引用，展开后只存在于进程内存中。
    """

    protocol: str
    base_url: str
    api_key: str = ""
    enabled: bool = True
    timeout: TimeoutCfg = Field(default_factory=TimeoutCfg)
    headers: dict[str, str] = Field(default_factory=dict)  # 按供应商覆盖的附加请求头

    @field_validator("protocol")
    @classmethod
    def _known_protocol(cls, value: str) -> str:
        """字段级校验：协议类型必须在白名单内，拼错立即启动失败（场景 15）。"""
        if value not in PROTOCOLS:
            raise ValueError(f"unknown protocol type '{value}' (expected one of {PROTOCOLS})")
        return value


class RouteCfg(BaseModel):
    """一条候选路由：走哪个供应商 + 该供应商侧的真实模型 ID + 权重。

    注意：model 是"真实上游模型 ID"，只进适配器和账本，
    绝不出现在任何北向响应里（对外只暴露别名）。
    """

    provider: str
    model: str
    weight: float = Field(default=1.0, gt=0)


class AliasCfg(BaseModel):
    """一个公开别名及其候选路由列表（故障转移按此顺序/权重进行）。"""

    strategy: str = "priority"
    routes: list[RouteCfg] = Field(min_length=1)  # 至少一条路由

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        """字段级校验：路由策略必须在白名单内。"""
        if value not in STRATEGIES:
            raise ValueError(f"unknown routing strategy '{value}' (expected one of {STRATEGIES})")
        return value


class PricingCfg(BaseModel):
    """按别名的价格表：每百万 Token 的输入/输出单价（USD）。

    cache_input_per_million 为 None 时缓存命中按普通输入价计算；
    未出现在 pricing 表里的别名，成本一律记 0（而不是报错）。
    """

    input_per_million: float = 0.0
    output_per_million: float = 0.0
    cache_input_per_million: float | None = None


class GatewayConfig(BaseModel):
    """顶层配置对象：全部子配置节的聚合根。"""

    service: ServiceCfg = Field(default_factory=ServiceCfg)
    auth: AuthCfg  # 唯一必填节：没有密钥列表的网关没有意义
    database: DatabaseCfg = Field(default_factory=DatabaseCfg)
    retry: RetryCfg = Field(default_factory=RetryCfg)
    circuit_breaker: BreakerCfg = Field(default_factory=BreakerCfg)
    rate_limit: RateLimitCfg = Field(default_factory=RateLimitCfg)
    structured_output: StructuredCfg = Field(default_factory=StructuredCfg)
    stream_checkpoint: CheckpointCfg = Field(default_factory=CheckpointCfg)
    masking: MaskingCfg = Field(default_factory=MaskingCfg)
    providers: dict[str, ProviderCfg] = Field(min_length=1)  # 供应商表（至少一家）
    aliases: dict[str, AliasCfg] = Field(min_length=1)  # 别名路由表
    pricing: dict[str, PricingCfg] = Field(default_factory=dict)  # 价格表（可选）

    @model_validator(mode="after")
    def _cross_references(self) -> GatewayConfig:
        """跨字段校验：每条路由引用的供应商必须在 providers 表中存在。

        这是"fail fast"的典型场景——如果等到运行时才发现路由指向
        不存在的供应商，排查成本远高于启动时一行报错（场景 15）。
        """
        for alias, cfg in self.aliases.items():
            for route in cfg.routes:
                if route.provider not in self.providers:
                    raise ValueError(
                        f"alias '{alias}' routes to unknown provider '{route.provider}' "
                        f"(known providers: {sorted(self.providers)})"
                    )
        return self


# --------------------------------------------------------------------------
# 加载入口
# --------------------------------------------------------------------------


def load_config(path: str | None = None) -> GatewayConfig:
    """加载 -> 展开 -> 校验，三步完成配置装配。

    配置路径解析优先级：显式参数 > 环境变量 GATEWAY_CONFIG > 内置示例配置。
    任何一步失败都抛 ConfigError，由调用方决定中止（教学：错误要尽早暴露）。
    """
    load_dotenv()  # 读取项目根目录 .env 注入环境变量；密钥只进内存，绝不打印

    cfg_path = path or os.environ.get("GATEWAY_CONFIG")
    if cfg_path is None:
        # 回退到内置示例配置：仓库内的 config/config.example.yaml
        # parents[2] = src/gateway/config.py 向上三级 = 仓库根目录
        built_in = Path(__file__).resolve().parents[2] / "config" / "config.example.yaml"
        if built_in.exists():
            cfg_path = str(built_in)
        else:
            raise ConfigError("no config path given and built-in example config not found")
    cfg_file = Path(cfg_path)
    if not cfg_file.exists():
        raise ConfigError(f"config file not found: {cfg_path}")

    try:
        raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {cfg_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping, got {type(raw).__name__}")

    expanded = _expand_tree(raw, cfg_path)  # 第二步：展开所有 ${VAR} 引用
    try:
        return GatewayConfig.model_validate(expanded)  # 第三步：结构校验
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {cfg_path}: {exc}") from exc


def load_config_or_exit(path: str | None = None) -> GatewayConfig:
    """命令行入口用的包装：出错时打印可定位的报错并以退出码 2 结束进程。

    "打印到 stderr + 非零退出码"是 Unix 惯例，容器编排系统
    （Docker/K8s）据此判定进程启动失败并重启。
    """
    try:
        return load_config(path)
    except ConfigError as exc:
        print(f"[gateway] configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
