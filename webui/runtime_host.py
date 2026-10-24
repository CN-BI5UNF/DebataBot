"""Runtime 宿主 —— 把 ``core.Runtime`` 的生命周期收敛到 WebUI 里。

原 GUI 用 qasync 把 Qt 事件循环和 asyncio 合成一个，Runtime 就跑在 UI 线程的
事件循环上。WebUI 换成 uvicorn：Runtime 直接跑在 **uvicorn 自己的事件循环**里
（由 FastAPI lifespan 驱动 start/shutdown），所以不存在跨线程 / 跨 loop 访问
asyncio 对象的问题，页面接口只是普通的 async 调用。

宿主还负责三件 WebUI 特有的事：
    - 首次配置：没有 config.yaml 时给出 ``setup_required`` 状态而不是崩溃
    - 配置落盘：统一的 ``save_config`` 入口（等价 GUI 的 ``_save_now``）
    - 重启：改完需要重启的配置后原地重启 Runtime
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

logger = logging.getLogger(__name__)

STATE_SETUP_REQUIRED = "setup_required"
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"
STATE_MAINTENANCE = "maintenance"


class RuntimeHost:
    """持有唯一的 Runtime 实例，并暴露给各 API 路由。"""

    def __init__(self, project_root: Path, config_file: Path | None = None) -> None:
        self.project_root = Path(project_root)
        self.config_file = Path(config_file) if config_file else None
        self.rt: Any = None
        self.paths: Any = None
        self.state: str = STATE_STOPPED
        self.last_error: str = ""
        self.started_at: float = 0.0
        self._lock = asyncio.Lock()
        # 单独一把锁：保证「stop 再 start」这一对不会被另一个重启请求插进来，
        # 否则两次并发重启可能交错成 stop/stop/start/start。
        self._restart_lock = asyncio.Lock()
        # 启动过程中产生的非致命问题（密钥缺失、feature 加载失败等）
        self.warnings: list[str] = []
        self._stop_requested = False

    # ============================================================
    # 路径 / 配置
    # ============================================================

    def build_paths(self) -> Any:
        """构造 AppPaths 并建好目录；不依赖 Runtime。"""
        from app_config import AppPaths, initialize_runtime_data

        if self.paths is None:
            self.paths = AppPaths(project_root=self.project_root, config_file=self.config_file)
            self.paths.ensure_data_dirs()
            initialize_runtime_data(self.paths, project_root=self.project_root)
        return self.paths

    @property
    def config_file_path(self) -> Path:
        return self.build_paths().CONFIG_FILE

    def has_config(self) -> bool:
        try:
            return self.config_file_path.exists()
        except Exception:  # noqa: BLE001
            return False

    def load_config(self) -> Any:
        from app_config.loader import load_config

        return load_config(self.build_paths())

    def save_config(self, backup: bool = True) -> None:
        """把内存里的 ``rt.config`` 写回 yaml（等价 GUI 的 ``_save_now``）。

        落盘前再校验一次：调用方漏校验时，宁可这次保存失败，也不能把一份
        Runtime 下次启动才炸的配置写出去。
        """
        from app_config.loader import ConfigError, save_config, user_error_text
        from webui.config_edit import validate_config

        if self.rt is None or self.rt.config is None:
            raise RuntimeError("Runtime 未启动，无法保存配置")
        try:
            validate_config(self.rt.config)
        except ValidationError as e:
            raise ConfigError(f"配置不合法，已拒绝写入：{user_error_text(e)}") from e
        save_config(self.rt.paths, self.rt.config, backup=backup)

    def secrets(self) -> Any:
        """取 SecretsManager（已 initialize）。"""
        from app_config import SecretsManager

        sm = SecretsManager(self.build_paths())
        sm.initialize()
        return sm

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> bool:
        """启动 Runtime。返回是否成功（失败时 ``state=error``，不抛异常）。"""
        async with self._lock:
            if self.state == STATE_RUNNING:
                return True
            self._stop_requested = False
            self.warnings = []

            if not self.has_config():
                self.state = STATE_SETUP_REQUIRED
                self.last_error = ""
                logger.info("未找到 config.yaml，WebUI 进入首次配置模式：%s", self.config_file_path)
                return False

            # 外部维护控制器与 WebUI 内 start/restart 共用同一 gate；不能
            # 因为 launcher 被阻止、WebUI 仍能直接拉起 Runtime。
            from core.maintenance import startup_gate

            gate = startup_gate(self.build_paths())
            if not gate.get("ok"):
                self.state = STATE_MAINTENANCE
                self.last_error = str(gate.get("message") or "维护状态禁止启动")
                logger.warning(
                    "Runtime 启动被维护门禁阻止：%s", self.last_error
                )
                return False

            compat = self._runtime_compat_gate()
            if not compat.get("ok"):
                self.state = STATE_MAINTENANCE
                self.last_error = str(compat.get("message") or "数据兼容门禁阻止启动")
                logger.warning(
                    "Runtime 启动被兼容门禁阻止：%s", self.last_error
                )
                return False

            self.state = STATE_STARTING
            self.last_error = ""
            from core import Runtime

            rt = Runtime(project_root=self.project_root, config_file=self.config_file)
            self.rt = rt
            try:
                await rt.start()
            except Exception as e:  # noqa: BLE001
                self.state = STATE_ERROR
                self.last_error = str(e)
                logger.exception("Runtime 启动失败")
                try:
                    await rt.shutdown()
                except Exception:  # noqa: BLE001
                    logger.debug("启动失败后清理 Runtime 出错", exc_info=True)
                self.rt = None
                return False

            self.state = STATE_RUNNING
            self.started_at = time.time()
            self._collect_warnings()
            logger.info("Runtime 已就绪（persona=%s）", getattr(rt.persona, "name", "?"))
            return True

    async def stop(self) -> None:
        async with self._lock:
            rt = self.rt
            self.rt = None
            self.state = STATE_STOPPED
            if rt is None:
                return
            try:
                await rt.shutdown()
            except Exception:  # noqa: BLE001
                logger.exception("Runtime 关闭失败")

    async def restart(self) -> bool:
        """原地重启（改完需要重启的配置后调用）。"""
        async with self._restart_lock:
            logger.info("正在重启 Runtime ...")
            await self.stop()
            return await self.start()

    def _runtime_compat_gate(self) -> dict[str, Any]:
        """同 launcher 的目标 manifest + 本地数据门禁，防 WebUI 绕过。"""
        from core.compat import (
            CompatError,
            evaluate,
            parse_manifest,
            read_local_state,
        )

        manifest_path = Path(self.project_root) / "compatibility.json"
        try:
            manifest_path = manifest_path.resolve()
            manifest_path.relative_to(Path(self.project_root).resolve())
        except (OSError, ValueError):
            return {"ok": False, "code": "manifest_path_escape",
                    "message": "compatibility.json 不在 project root 内"}
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return {"ok": False, "code": "manifest_missing",
                    "message": "目标源码缺少 compatibility.json"}
        try:
            manifest = parse_manifest(manifest_path.read_text(encoding="utf-8"))
        except (OSError, CompatError) as exc:
            return {"ok": False, "code": "manifest_invalid",
                    "message": f"compatibility.json 非法：{exc}"}
        paths = self.build_paths()
        local = read_local_state(paths.DATA_DIR, config_path=paths.CONFIG_FILE)
        # 刚完成向导、还没有任何库的实例允许创建 v1 schema；一旦已有任一
        # 数据库，仍走逐库/迁移门禁，不能用“空”掩盖旧库。
        if (
            not local.databases
            and not local.first_run
            and not local.config_missing
            and all(
                local.planes.get(name, 0) == 0
                for name in ("persona_db", "maintenance", "rag", "projection")
            )
            and local.planes.get("root_config", 0)
            >= manifest.planes["root_config"].target_version
        ):
            return {
                "ok": True,
                "code": "compatible_empty",
                "message": "空实例，允许初始化数据库",
                "snapshot": local.snapshot(),
            }
        result = evaluate(manifest, local)
        return {
            "ok": result.get("code") == "compatible",
            "code": result.get("code"),
            "message": result.get("message"),
            "snapshot": result.get("data_snapshot"),
        }

    def request_stop(self) -> None:
        """供系统级接口请求退出整个 WebUI。"""
        self._stop_requested = True
        if self.rt is not None:
            try:
                self.rt.request_stop()
            except Exception:  # noqa: BLE001
                logger.debug("request_stop 失败", exc_info=True)

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def _collect_warnings(self) -> None:
        rt = self.rt
        if rt is None:
            return
        for name, reason in (getattr(rt, "feature_failures", None) or {}).items():
            self.warnings.append(f"功能 {name} 未就绪：{reason}")
        try:
            missing = _find_missing_secrets(rt)
        except Exception:  # noqa: BLE001
            missing = []
        for item in missing:
            self.warnings.append(f"密钥缺失：{item}")

    # ============================================================
    # 状态快照
    # ============================================================

    def status(self) -> dict[str, Any]:
        """顶栏 / 总览页要用的运行状态。"""
        rt = self.rt
        info: dict[str, Any] = {
            "state": self.state,
            "last_error": self.last_error,
            "warnings": list(self.warnings),
            "config_file": str(self.config_file_path),
            "data_dir": str(self.build_paths().DATA_DIR),
            "uptime_seconds": (time.time() - self.started_at) if self.started_at else 0.0,
            "adapter_connected": False,
            "adapter": None,
            "persona": "",
            "persona_id": "",
        }
        if rt is None:
            return info

        cfg = getattr(rt, "config", None)
        if cfg is not None:
            info["persona_id"] = cfg.persona.active
            try:
                name, adapter_cfg = next(iter(cfg.adapters.items()))
                info["adapter"] = {
                    "name": name,
                    "mode": adapter_cfg.mode,
                    "host": adapter_cfg.host,
                    "port": adapter_cfg.port,
                    "path": adapter_cfg.path,
                    "enabled": adapter_cfg.enabled,
                }
            except StopIteration:
                pass
        persona = getattr(rt, "persona", None)
        if persona is not None:
            info["persona"] = getattr(persona, "name", "") or info["persona_id"]
        adapter = getattr(rt, "adapter", None)
        if adapter is not None:
            try:
                info["adapter_connected"] = bool(adapter.is_connected)
            except Exception:  # noqa: BLE001
                info["adapter_connected"] = False
        return info

    # ============================================================
    # 首次配置：用表单数据生成一份 RootConfig
    # ============================================================

    def write_initial_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """按向导 payload 写入 config.yaml 与密钥。

        返回 ``{"ok": bool, "message": str, "provider_id": str, "model": str}``。
        payload 结构见 ``webui/api/system.py`` 的 ``SetupPayload``。

        两处容易踩的地方：

        - **providers 是按 id 合并，不是整段替换。** 用户完全可能在设置页加过一个
          中转站，之后重跑向导只是想改主模型；整段替换会把那个 provider 连同
          引用它的 Agent 一起打回原形。要真正重置得显式传 ``replace_providers``。
        - **写盘前跑一次 RootConfig 校验。** Agent 引用不存在的 provider 这类错误
          必须在这里拦住，否则会写出一份启动时才炸的配置。
        """
        from app_config.loader import load_config, save_config
        from app_config.schema import (
            AgentConfig,
            AgentsConfig,
            BehaviorConfig,
            EmbeddingFeatureConfig,
            FeaturesConfig,
            LongTermMemoryConfig,
            NapCatAdapterConfig,
            PersonaConfig,
            ProviderConfig,
            ReasoningConfig,
            RootConfig,
            TTSFeatureConfig,
            VisionFeatureConfig,
            WeatherFeatureConfig,
            WebSearchFeatureConfig,
            WhitelistConfig,
        )

        paths = self.build_paths()
        providers_in = payload.get("providers") or []
        if not providers_in:
            raise ValueError("至少需要配置一个模型提供商")

        existing = None
        if paths.CONFIG_FILE.exists():
            try:
                existing = load_config(paths, set_global=False)
            except Exception:  # noqa: BLE001
                logger.warning("现有配置无法解析，将直接覆盖", exc_info=True)

        secrets = self.secrets()
        pending_secrets: dict[str, str] = {}
        # 不走 provider 的密钥（天气 / TTS 等）先收集，完整配置校验通过后再写。
        for key_id, value in (payload.get("secrets") or {}).items():
            if value:
                pending_secrets[str(key_id)] = str(value)

        providers: dict[str, ProviderConfig] = {}
        for item in providers_in:
            pid = str(item.get("id") or "").strip()
            if not pid:
                raise ValueError("Provider ID 不能为空")
            requested_key_id = str(item.get("api_key_id") or f"{pid}_key").strip()
            api_key = (item.get("api_key") or "").strip()
            existing_provider = (
                existing.providers.get(pid) if existing is not None else None
            )
            if api_key:
                key_id = requested_key_id
                pending_secrets[key_id] = api_key
            elif existing_provider is not None and existing_provider.api_key_id:
                key_id = existing_provider.api_key_id
            elif secrets.has(requested_key_id):
                key_id = requested_key_id
            else:
                key_id = ""
            providers[pid] = ProviderConfig(
                preset=item.get("preset") or None,
                display_name=item.get("display_name") or pid,
                protocol=item.get("protocol") or None,
                base_url=item.get("base_url") or None,
                api_key_id=key_id or None,
                extra_headers=dict(item.get("extra_headers") or {}),
                timeout_seconds=float(item.get("timeout_seconds") or 120.0),
                reasoning_style=item.get("reasoning_style") or None,
            )

        if existing is not None and not payload.get("replace_providers"):
            all_providers = {
                pid: provider.model_copy(deep=True)
                for pid, provider in existing.providers.items()
            }
            all_providers.update(providers)
        else:
            all_providers = dict(providers)

        chat_in = payload.get("chat") or {}
        chat_provider = str(chat_in.get("provider") or "").strip()
        if chat_provider not in all_providers:
            chat_provider = next(iter(providers))
        chat_model = str(chat_in.get("model") or "").strip() or _default_model_for(
            paths, all_providers[chat_provider], chat_provider
        )

        def _reasoning(agent_in: dict[str, Any]) -> ReasoningConfig | None:
            """向导里思考是个开关 + 档位；三项都缺就不配 reasoning。"""
            if not any(k in agent_in for k in ("reasoning", "reasoning_budget")):
                return None
            return ReasoningConfig(
                enabled=bool(agent_in.get("reasoning")),
                budget=agent_in.get("reasoning_budget") or None,
                max_tokens=agent_in.get("reasoning_max_tokens") or None,
            )

        def _agent(
            agent_in: dict[str, Any],
            *,
            provider: str,
            model: str,
            temperature: float,
            max_tokens: int,
        ) -> AgentConfig:
            kwargs: dict[str, Any] = {
                "provider": str(agent_in.get("provider") or provider).strip() or provider,
                "model": str(agent_in.get("model") or model).strip() or model,
                "temperature": float(agent_in.get("temperature", temperature)),
                "max_tokens": int(agent_in.get("max_tokens", max_tokens)),
                "top_p": float(agent_in.get("top_p", 1.0)),
                "first_token_timeout_seconds": float(
                    agent_in.get("first_token_timeout_seconds", 30.0)
                ),
            }
            reasoning = _reasoning(agent_in)
            if reasoning is not None:
                kwargs["reasoning"] = reasoning
            return AgentConfig(**kwargs)

        chat_agent = _agent(
            chat_in,
            provider=chat_provider,
            model=chat_model,
            temperature=0.6,
            max_tokens=16384,
        )
        # 子 Agent 各自可以有自己的 provider/model；缺省时跟随主模型
        agents_in = payload.get("agents") or {}
        sub_agents: dict[str, AgentConfig | None] = {}
        for name, default_temp, default_tokens in (
            ("proactive", 0.3, 64),
            ("summary", 0.1, 8192),
            ("persona_gen", 0.7, 8192),
        ):
            item = agents_in.get(name)
            if item is False:          # 老前端用布尔表示开关
                sub_agents[name] = None
                continue
            if item is None:
                item = {}
            if isinstance(item, dict) and item.get("enabled") is False:
                sub_agents[name] = None
                continue
            if not isinstance(item, dict):
                item = {}
            sub_agents[name] = _agent(
                item,
                provider=chat_provider,
                model=chat_model,
                temperature=default_temp,
                max_tokens=default_tokens,
            )

        adapter_in = payload.get("adapter") or {}
        if existing is not None and "whitelist" not in adapter_in:
            existing_adapter = existing.adapters.get("default")
            whitelist = (
                existing_adapter.whitelist.model_copy(deep=True)
                if existing_adapter is not None
                else WhitelistConfig()
            )
        else:
            whitelist_in = adapter_in.get("whitelist") or {}
            # 名单以前被前端写死成空数组——选了 whitelist 却没有名单 = 谁都进不来
            whitelist = WhitelistConfig(
                mode=whitelist_in.get("mode") or "verify",
                qq_ids=_int_list(whitelist_in.get("qq_ids")),
                group_ids=_int_list(whitelist_in.get("group_ids")),
            )
        adapter = NapCatAdapterConfig(
            enabled=bool(adapter_in.get("enabled", True)),
            mode=adapter_in.get("mode") or "client",
            host=adapter_in.get("host") or "127.0.0.1",
            port=int(adapter_in.get("port") or 3000),
            path=adapter_in.get("path") or "/",
            manage_process=bool(adapter_in.get("manage_process", False)),
            process_path=str(adapter_in.get("process_path") or ""),
            auto_restart=bool(adapter_in.get("auto_restart", True)),
            whitelist=whitelist,
        )
        token = (adapter_in.get("access_token") or "").strip()
        if token:
            pending_secrets["napcat_default_token"] = token
            adapter.access_token_id = "napcat_default_token"

        raw_features = copy.deepcopy(payload.get("features") or {})
        vision_raw = raw_features.get("vision")
        if isinstance(vision_raw, dict):
            vision_api_key = str(vision_raw.pop("api_key", "") or "").strip()
            if vision_api_key:
                # 独立视觉密钥使用专用 secret ID，绝不写进主 provider 的 key。
                vision_raw["api_key_id"] = "vision_key"
                pending_secrets["vision_key"] = vision_api_key
            existing_vision = (
                existing.features.vision if existing is not None else None
            )
            if (
                vision_raw.get("enabled")
                and not str(vision_raw.get("api_key_id") or "").strip()
                and not (
                    existing_vision is not None
                    and existing_vision.api_key_id
                )
            ):
                target_pid = str(
                    vision_raw.get("provider")
                    or (existing_vision.provider if existing_vision else "")
                    or chat_provider
                ).strip()
                target_cfg = all_providers.get(target_pid)
                if target_cfg is not None and target_cfg.api_key_id:
                    vision_raw["api_key_id"] = target_cfg.api_key_id

        if existing is not None:
            features_cfg = _merge_features(existing.features, raw_features)
        else:
            features_cfg = _build_features(
                raw_features,
                FeaturesConfig,
                {
                    "vision": VisionFeatureConfig,
                    "weather": WeatherFeatureConfig,
                    "tts": TTSFeatureConfig,
                    "web_search": WebSearchFeatureConfig,
                    "embedding": EmbeddingFeatureConfig,
                    "long_term_memory": LongTermMemoryConfig,
                },
            )

        cfg = RootConfig(
            providers=all_providers,
            agents=AgentsConfig(
                chat=chat_agent,
                proactive=sub_agents["proactive"],
                summary=sub_agents["summary"],
                persona_gen=sub_agents["persona_gen"],
            ),
            features=features_cfg,
            behavior=BehaviorConfig(**_behavior_overrides(payload.get("behavior") or {})),
            persona=PersonaConfig(active=payload.get("persona") or "debata"),
            adapters={"default": adapter},
        )

        # 写盘前跑一次完整校验：Agent/视觉引用不存在的 provider 这类错误
        # 必须在这里拦住，否则会写出一份启动时才炸的配置。
        try:
            RootConfig.model_validate(cfg.model_dump())
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"配置校验未通过：{e}") from e

        # 完整配置验证通过后才写 secret，避免半套配置/密钥落盘。
        for key_id, value in pending_secrets.items():
            secrets.set(str(key_id), str(value))

        save_config(paths, cfg)
        _stash_persona_brief(paths, payload.get("persona_brief"))
        logger.info("首次配置已写入 %s", paths.CONFIG_FILE)
        return {
            "ok": True,
            "message": "配置已写入",
            "provider_id": chat_provider,
            "model": chat_model,
        }


def _find_missing_secrets(rt: Any) -> list[str]:
    """cfg 引用的 *_key_id / access_token_id 是否都在 secrets 里。"""
    missing: list[str] = []
    secrets = getattr(rt, "secrets", None)
    cfg = getattr(rt, "config", None)
    if secrets is None or cfg is None:
        return missing
    for pid, p in (cfg.providers or {}).items():
        if p.api_key_id and secrets.get(p.api_key_id) is None:
            missing.append(f"providers.{pid}.api_key_id = '{p.api_key_id}'")
    for aid, a in (cfg.adapters or {}).items():
        token_id = getattr(a, "access_token_id", None)
        if token_id and secrets.get(token_id) is None:
            missing.append(f"adapters.{aid}.access_token_id = '{token_id}'")
    return missing


PERSONA_BRIEF_FILE = "persona_brief.json"
"""向导里填的角色描述暂存处。

向导阶段 Runtime 还没起来，没法当场调生成器；但把用户填的东西直接丢掉更糟。
先存到数据目录，配置完成后「角色 → 新建角色」会读它做预填，用过即删。
"""


def _stash_persona_brief(paths: Any, brief: Any) -> None:
    """把向导里填的角色描述写到数据目录，供「新建角色」预填。"""
    import json

    if not isinstance(brief, dict):
        return
    text = {k: v for k, v in brief.items() if isinstance(v, str) and v.strip()}
    if not text:
        return
    try:
        (paths.DATA_DIR / PERSONA_BRIEF_FILE).write_text(
            json.dumps(text, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("已暂存向导里的角色描述，配置完成后可在「角色 → 新建角色」里接着用")
    except OSError:
        logger.warning("暂存角色描述失败（不影响配置写入）", exc_info=True)


def _int_list(value: Any) -> list[int]:
    """把向导传来的名单统一成 int 列表。

    前端可能是 ``[123, 456]``、``"123,456"`` 或 ``"123\\n456"``（用户从别处粘贴），
    三种都要认；空值给空列表。
    """
    if value in (None, ""):
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\s，、]+", value)
        return [int(p) for p in parts if p.strip()]
    return [int(x) for x in value]


def _build_features(payload: dict[str, Any], container_cls: Any, section_classes: dict[str, Any]):
    """按向导 payload 构造 FeaturesConfig。

    只处理 payload 里出现过的段——没出现的保持 schema 默认值，
    这样向导不必把每个字段都列出来。
    """
    kwargs: dict[str, Any] = {}
    for key, cls in section_classes.items():
        raw = payload.get(key)
        if isinstance(raw, dict):
            allowed = set(type(cls).__annotations__) | set(getattr(cls, "model_fields", {}))
            kwargs[key] = cls(**{k: v for k, v in raw.items() if k in allowed})
    return container_cls(**kwargs)


def _patch_mapping(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """递归 patch：只覆盖 updates 中实际出现的字段。"""
    merged = dict(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _patch_mapping(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merge_features(existing: Any, raw_features: dict[str, Any]) -> Any:
    """按 payload 中显式出现的字段 patch 现有 features。

    - 整个 section 缺省：保留；
    - section 内字段缺省：保留；
    - 显式 False/0/"auto"/"" 都是有效提交，不能按“等于默认值”忽略；
    - 不整段替换，避免只关独立 vision 就清空 provider/model/key。
    """
    merged = existing.model_copy(deep=True)
    if not isinstance(raw_features, dict):
        return merged
    for key, raw in raw_features.items():
        if not isinstance(raw, dict):
            continue
        target = getattr(merged, key, None)
        if target is None:
            continue
        allowed = set(type(target).model_fields)
        updates = {k: v for k, v in raw.items() if k in allowed}
        if not updates:
            continue
        section = _patch_mapping(target.model_dump(), updates)
        setattr(merged, key, type(target)(**section))
    return merged


def _behavior_overrides(payload: dict[str, Any]) -> dict[str, Any]:
    """向导里允许调的 behavior 字段（目前只有上下文上限）。"""
    out: dict[str, Any] = {}
    if "max_context_tokens" in payload:
        value = payload["max_context_tokens"]
        out["context"] = {"max_context_tokens": None if value in (None, "") else int(value)}
    return out


def _default_model_for(paths: Any, provider: Any, provider_id: str) -> str:
    """provider 没写 model 时，从预设里挑第一个。"""
    preset_id = getattr(provider, "preset", None) or provider_id
    try:
        from providers.presets_loader import load_presets_for

        presets = load_presets_for(paths)
    except Exception:  # noqa: BLE001
        return "deepseek-flash"
    preset = presets.get(preset_id)
    if preset is not None and getattr(preset, "models", None):
        return preset.models[0].id
    return "deepseek-flash"


__all__ = [
    "STATE_ERROR",
    "STATE_RUNNING",
    "STATE_SETUP_REQUIRED",
    "STATE_STARTING",
    "STATE_STOPPED",
    "RuntimeHost",
]
