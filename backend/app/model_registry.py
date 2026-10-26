"""Provider credentials and model catalogs, with an immutable active snapshot."""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import os
import re
from threading import RLock
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings

ModelApi = Literal["openai-completions", "openai-responses", "openai-codex-responses", "anthropic-messages", "google-generative-ai", "mistral-conversations"]
ThinkingLevel = Literal["default", "off", "minimal", "low", "medium", "high", "xhigh", "max"]
ENV_KEYS = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "google": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY", "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY", "mistral": "MISTRAL_API_KEY", "moonshotai": "MOONSHOT_API_KEY",
    "moonshotai-cn": "MOONSHOT_API_KEY", "zai": "ZAI_API_KEY", "zai-coding-cn": "ZAI_CODING_CN_API_KEY",
    "minimax": "MINIMAX_API_KEY", "minimax-cn": "MINIMAX_CN_API_KEY", "together": "TOGETHER_API_KEY",
    "nvidia": "NVIDIA_API_KEY", "kimi-coding": "KIMI_API_KEY", "cerebras": "CEREBRAS_API_KEY",
    "fireworks": "FIREWORKS_API_KEY", "baseten": "BASETEN_API_KEY", "huggingface": "HF_TOKEN",
    "vercel-ai-gateway": "AI_GATEWAY_API_KEY", "opencode": "OPENCODE_API_KEY",
    "opencode-go": "OPENCODE_API_KEY", "ant-ling": "ANT_LING_API_KEY",
}
ENV_KEYS.update({"github-copilot": "COPILOT_GITHUB_TOKEN", "meta": "META_API_KEY"})
OAUTH_PROVIDERS = {"openai", "openai-codex", "anthropic", "github-copilot", "kimi-coding", "xai", "meta", "openrouter"}
PROVIDERS = {"custom", *ENV_KEYS, *OAUTH_PROVIDERS}
OAUTH_ENDPOINTS = {
    "openai": "https://api.openai.com/v1", "openai-codex": "https://chatgpt.com/backend-api",
    "anthropic": "https://api.anthropic.com", "github-copilot": "https://api.individual.githubcopilot.com",
    "kimi-coding": "https://api.kimi.com/coding", "xai": "https://api.x.ai/v1",
    "meta": "https://api.meta.ai/v1", "openrouter": "https://openrouter.ai/api/v1",
}


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ManagedModelInput(ConfigInput):
    id: str = Field(default="", max_length=200)
    name: str = Field(default="", max_length=200)
    api: ModelApi = "openai-completions"
    context_window: int = Field(default=32768, ge=4096, le=2000000)
    max_tokens: int = Field(default=8192, ge=256, le=200000)
    reasoning: bool = False
    supports_images: bool = False
    thinking_level: ThinkingLevel = "default"
    temperature: float | None = Field(default=None, ge=0, le=2)
    compat: dict[str, bool | str] = Field(default_factory=dict)


class ProviderInput(ConfigInput):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(default="custom", max_length=100)
    base_url: str = Field(default="", max_length=2000)
    credential_mode: Literal["api_key", "environment", "none", "oauth"] = "api_key"
    # Null retains a credential for this identity/endpoint; empty removes it.
    api_key: str | None = Field(default=None, max_length=8000)
    headers: dict[str, str] | None = None
    timeout_seconds: float = Field(default=90, ge=10, le=600)
    models: list[ManagedModelInput] = Field(default_factory=list, max_length=200)


class ModelSelection(ConfigInput):
    provider_id: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=200)


class ModelRegistryInput(ConfigInput):
    providers: list[ProviderInput] = Field(default_factory=list, max_length=50)
    active: ModelSelection | None = None
    vision: ModelSelection | None = None
    auto_select: bool = True


class ModelProbeInput(ConfigInput):
    provider: ProviderInput
    model: ManagedModelInput


class OAuthAnswerInput(ConfigInput):
    prompt_id: str = Field(min_length=1, max_length=100)
    value: str = Field(default="", max_length=8000)


class ModelRemovalInput(ConfigInput):
    model_id: str = Field(min_length=1, max_length=200)


class ProviderLogoutInput(ConfigInput):
    provider: str = Field(max_length=100)


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "custom"
    api: str = "openai-completions"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    allow_without_key: bool = False
    timeout_seconds: float = 90
    context_window: int = 32768
    max_tokens: int = 8192
    reasoning: bool = False
    supports_images: bool = False
    thinking_level: str = "default"
    temperature: float | None = None
    compat: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    provider_id: str = ""
    provider_name: str = ""
    model_name: str = ""
    credential_source: str = "none"
    credential_mode: str = "api_key"
    oauth_credentials_path: str = ""
    oauth_logged_in: bool = False

    @property
    def configured(self):
        return bool(self.base_url and self.model and (self.api_key or self.allow_without_key or self.oauth_logged_in))

    def public(self):
        values = asdict(self)
        values.pop("api_key")
        values.pop("headers")
        values.pop("oauth_credentials_path")
        return {**values, "has_api_key": bool(self.api_key), "configured": self.configured}


def validate_endpoint(value: str, required=False):
    value = value.strip().rstrip("/")
    if not value and not required:
        return value
    endpoint = urlparse(value)
    if (endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment):
        raise ValueError("接口地址必须是完整的 http(s) 地址，不能包含密钥、查询参数或片段")
    if endpoint.path.endswith(("/chat/completions", "/responses", "/messages")):
        raise ValueError("请填写接口根地址，不要包含 /chat/completions、/responses 或 /messages")
    return value


def validate_compat(model: dict):
    booleans = {"supportsStore", "supportsDeveloperRole", "supportsReasoningEffort", "supportsUsageInStreaming", "supportsStrictMode"}
    for key, value in model["compat"].items():
        if key in booleans and type(value) is bool:
            continue
        if (key == "maxTokensField" and value in ("max_tokens", "max_completion_tokens")
                and model["api"] == "openai-completions"):
            continue
        if (key == "thinkingFormat" and value in ("openai", "openrouter", "deepseek", "together", "zai", "qwen", "chat-template", "qwen-chat-template", "string-thinking", "ant-ling")
                and model["api"] == "openai-completions"):
            continue
        raise ValueError(f"不支持的兼容参数：{key}")
    if model["compat"] and not model["api"].startswith("openai-"):
        raise ValueError("兼容参数仅用于 Chat Completions 或 Responses 协议")
    if model["api"] == "openai-responses" and any(key not in {"supportsDeveloperRole", "supportsStrictMode"} for key in model["compat"]):
        raise ValueError("Responses 协议不支持所选 Chat Completions 兼容参数")


class ModelConfigStore:
    def __init__(self, settings: Settings):
        self.path = settings.data / "model-config.json"
        self.lock = RLock()
        self.source = "none"
        self.registry = {"version": 2, "providers": [], "active": None, "vision": None}
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            self.registry = saved if saved.get("version") == 2 else self._legacy_registry(ModelConfig(**saved))
            self.source = "saved"
        else:
            for role, url, model, key in (
                ("active", settings.text_url, settings.text_model, settings.text_key),
                ("vision", settings.vision_url, settings.vision_model, settings.vision_key),
            ):
                if not (url or model or key):
                    continue
                legacy = self._legacy_registry(ModelConfig(base_url=url, model=model, api_key=key,
                    allow_without_key=not bool(key), timeout_seconds=settings.model_timeout,
                    supports_images=role == "vision"))
                provider = legacy["providers"][0]
                provider["id"] = "environment-" + role
                provider["name"] = "原有图文模型" if role == "vision" else "原有文本模型"
                self.registry["providers"].append(provider)
                self.registry[role] = {"provider_id": provider["id"], "model_id": model} if model else None
                self.source = "environment"
        self.registry.setdefault("vision", None)
        self.registry.setdefault("auto_select", True)
        if self._select_default(self.registry) and self.source == "saved":
            self._write(self.registry)

    def _select_default(self, registry):
        if registry.get("active") or not registry.get("auto_select", True):
            return False
        available = [config for provider in registry["providers"] for model in provider["models"]
                     if (config := self._resolve(provider, model)).configured]
        if len(available) != 1:
            return False
        config = available[0]
        registry["active"] = {"provider_id":config.provider_id,"model_id":config.model}
        return True

    @staticmethod
    def _legacy_registry(config):
        model = ManagedModelInput(id=config.model, name=config.model, api=config.api,
            context_window=config.context_window, max_tokens=config.max_tokens, reasoning=config.reasoning,
            supports_images=config.supports_images, thinking_level=config.thinking_level,
            temperature=config.temperature, compat=config.compat).model_dump()
        provider = {"id": "migrated", "name": "原有模型配置", "provider": config.provider,
            "base_url": config.base_url.rstrip("/"), "credential_mode": "none" if config.allow_without_key else "api_key",
            "api_key": config.api_key, "headers": config.headers, "timeout_seconds": config.timeout_seconds, "models": [model]}
        return {"version": 2, "providers": [provider],
            "active": {"provider_id": provider["id"], "model_id": config.model} if config.model else None}

    @staticmethod
    def _credential(provider):
        if provider["credential_mode"] == "oauth":
            return "", "OAuth"
        if provider["credential_mode"] == "environment":
            variable = ENV_KEYS.get(provider["provider"], "")
            return os.getenv(variable, "").strip(), variable
        if provider["credential_mode"] == "none":
            return "", "none"
        return provider.get("api_key") or "", "saved"

    def _resolve(self, provider, model):
        key, source = self._credential(provider)
        return ModelConfig(provider=provider["provider"], base_url=provider["base_url"], model=model["id"], api_key=key,
            allow_without_key=provider["credential_mode"] == "none", timeout_seconds=provider["timeout_seconds"],
            headers=deepcopy(provider.get("headers") or {}), provider_id=provider["id"], provider_name=provider["name"],
            model_name=model["name"] or model["id"], credential_source=source, credential_mode=provider["credential_mode"],
            oauth_credentials_path=str(self.oauth_path(provider["id"])) if provider["credential_mode"] == "oauth" else "",
            oauth_logged_in=self.oauth_status(provider),
            **{key: deepcopy(model[key]) for key in ("api", "context_window", "max_tokens", "reasoning", "supports_images", "thinking_level", "temperature", "compat")})

    def _snapshot(self, registry, role="active"):
        active = registry.get(role)
        if active:
            for provider in registry["providers"]:
                if provider["id"] == active["provider_id"]:
                    for model in provider["models"]:
                        if model["id"] == active["model_id"]:
                            return self._resolve(provider, model)
        return ModelConfig()

    def snapshot(self, vision=False):
        with self.lock:
            if vision and self.registry.get("vision"):
                return self._snapshot(self.registry, "vision")
            config = self._snapshot(self.registry)
            return config if not vision or config.supports_images else ModelConfig()

    def public(self):
        with self.lock:
            return {**self.snapshot().public(), "source": self.source}

    def public_registry(self):
        with self.lock:
            result = deepcopy(self.registry)
            for provider in result["providers"]:
                key, source = self._credential(provider)
                provider.pop("api_key", None)
                headers = provider.pop("headers", {})
                logged_in = self.oauth_status({**provider, "credential_mode": "oauth"})
                provider.update(has_api_key=bool(key), has_headers=bool(headers), credential_source=source,
                    supports_oauth=provider["provider"] in OAUTH_PROVIDERS, oauth_logged_in=logged_in,
                    env_key=ENV_KEYS.get(provider["provider"]), configured=bool(provider["base_url"] and (key or provider["credential_mode"] == "none" or (logged_in and provider["credential_mode"] == "oauth"))))
            return {**result, "current": self.public(), "vision_current": self.snapshot(vision=True).public(), "source": self.source}

    def _provider_candidate(self, values):
        provider = ProviderInput.model_validate(values).model_dump()
        provider["name"] = provider["name"].strip()
        if not provider["name"] or not re.fullmatch(r"[a-zA-Z0-9_-]+", provider["id"]):
            raise ValueError("服务商名称不能为空，标识只能包含字母、数字、下划线或短横线")
        if provider["provider"] not in PROVIDERS:
            raise ValueError("请选择支持的服务商，其他服务使用自定义接口")
        provider["base_url"] = validate_endpoint(provider["base_url"])
        if provider["credential_mode"] == "oauth":
            if provider["provider"] not in OAUTH_PROVIDERS:
                raise ValueError("该服务商不支持订阅登录")
            expected = OAUTH_ENDPOINTS[provider["provider"]]
            if provider["base_url"] and provider["base_url"] != expected:
                raise ValueError("订阅登录使用服务商官方接口，请恢复默认地址或改用 API Key")
            provider["base_url"] = expected
        old = next((item for item in self.registry["providers"] if item["id"] == provider["id"]), None)
        retain = old and (old["provider"], old["base_url"]) == (provider["provider"], provider["base_url"])
        if provider["api_key"] is None:
            provider["api_key"] = old.get("api_key", "") if retain else ""
        provider["api_key"] = provider["api_key"].strip()
        if provider["headers"] is None:
            provider["headers"] = deepcopy(old.get("headers") or {}) if retain else {}
        if len(provider["headers"]) > 30:
            raise ValueError("自定义请求头不能超过 30 项")
        for name, value in provider["headers"].items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-zA-Z-]+", name) or len(value) > 8000 or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("请求头名称或值无效")
            if name.lower() in {"authorization", "x-api-key", "x-goog-api-key", "host", "content-length", "content-type"}:
                raise ValueError("认证请求头由 API Key 自动生成，请勿在自定义请求头重复填写")
        if provider["credential_mode"] == "environment" and provider["provider"] not in ENV_KEYS:
            raise ValueError("自定义接口请填写 API Key 或选择无需密钥")
        if provider["credential_mode"] == "none" and provider["provider"] != "custom":
            raise ValueError("免密钥连接仅适用于自定义或本地模型服务")
        identities = set()
        for model in provider["models"]:
            if model["api"] == "openai-codex-responses" and (provider["provider"] != "openai-codex" or provider["credential_mode"] != "oauth"):
                raise ValueError("Codex 订阅协议仅用于 ChatGPT / Codex 的订阅连接")
            model["id"], model["name"] = model["id"].strip(), model["name"].strip()
            if not model["id"] or model["id"] in identities:
                raise ValueError("同一服务商下的模型 ID 必须非空且不能重复")
            identities.add(model["id"])
            if model["max_tokens"] > model["context_window"]:
                raise ValueError(f"模型 {model['id']} 的最大输出不能超过上下文窗口")
            if not model["reasoning"] and model["thinking_level"] not in ("default", "off"):
                raise ValueError("启用推理能力后才能指定思考强度")
            validate_compat(model)
        return provider

    def registry_candidate(self, values):
        with self.lock:
            values = ModelRegistryInput.model_validate(values).model_dump()
            providers = [self._provider_candidate(provider) for provider in values["providers"]]
            if len({provider["id"] for provider in providers}) != len(providers):
                raise ValueError("服务商标识不能重复")
            registry = {"version": 2, "providers": providers, "active": values["active"], "vision": values["vision"], "auto_select":values["auto_select"]}
            self._select_default(registry)
            if registry["active"] and not self._snapshot(registry).configured:
                raise ValueError("使用中的模型必须存在，并填写有效凭据或完成订阅登录；也可暂不选择使用中的模型")
            if registry["vision"]:
                config = self._snapshot(registry, "vision")
                if not config.configured or not config.supports_images:
                    raise ValueError("图文模型必须存在、支持图片，并配置有效凭据或完成订阅登录")
            return registry

    def probe_candidate(self, provider_values, model_values, require_model=True):
        with self.lock:
            model = ManagedModelInput.model_validate(model_values).model_dump()
            provider = self._provider_candidate({**provider_values, "models": [model] if require_model else []})
            config = self._resolve(provider, model)
            validate_endpoint(config.base_url, required=True)
            if not (config.api_key or config.allow_without_key or config.oauth_logged_in):
                raise ValueError("请填写 API Key、配置环境变量或完成订阅登录；本地服务可选择无需密钥")
            if require_model and not config.configured:
                raise ValueError("请先填写模型 ID")
            return config

    def _write(self, registry):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as output:
                os.chmod(temporary, 0o600)
                json.dump(registry, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.registry, self.source = registry, "saved"

    def save_registry(self, values):
        with self.lock:
            self._write(self.registry_candidate(values))
            return self.public_registry()

    def oauth_path(self, provider_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", provider_id):
            raise ValueError("服务商标识无效")
        return self.path.parent / "model-auth" / f"{provider_id}.json"

    def oauth_status(self, provider):
        if provider["credential_mode"] != "oauth":
            return False
        try:
            value = json.loads(self.oauth_path(provider["id"]).read_text(encoding="utf-8"))
            credential = value.get("credential") or {}
            return bool(value.get("provider") == provider["provider"] and credential.get("type") == "oauth" and credential.get("access"))
        except (OSError, ValueError, TypeError):
            return False

    def oauth_candidate(self, values):
        with self.lock:
            provider = self._provider_candidate({**values, "models": []})
            if provider["credential_mode"] != "oauth":
                raise ValueError("请先选择订阅登录作为凭据方式")
            return provider

    def remove_provider(self, provider_id):
        with self.lock:
            self.oauth_path(provider_id)  # Validate the identifier even for an unsaved connection.
            registry = deepcopy(self.registry)
            registry["providers"] = [item for item in registry["providers"] if item["id"] != provider_id]
            if registry["active"] and registry["active"]["provider_id"] == provider_id:
                registry["active"] = None
            vision = registry.get("vision")
            if vision:
                config = self._snapshot(registry, "vision")
                if not config.configured or not config.supports_images:
                    registry["vision"] = None
            self._write(registry)
            return self.public_registry()

    def remove_model(self, provider_id, model_id):
        with self.lock:
            registry = deepcopy(self.registry)
            provider = next((item for item in registry["providers"] if item["id"] == provider_id), None)
            if not provider:
                raise ValueError("服务商已不存在，请重新读取配置")
            provider["models"] = [item for item in provider["models"] if item["id"] != model_id]
            if registry["active"] == {"provider_id": provider_id, "model_id": model_id}:
                registry["active"] = None
            vision = registry.get("vision")
            if vision:
                config = self._snapshot(registry, "vision")
                if not config.configured or not config.supports_images:
                    registry["vision"] = None
            self._write(registry)
            return self.public_registry()

    def deactivate_provider(self, provider_id):
        with self.lock:
            registry = deepcopy(self.registry)
            provider = next((item for item in registry["providers"] if item["id"] == provider_id), None)
            if provider and provider["credential_mode"] == "oauth":
                for role in ("active", "vision"):
                    if registry.get(role) and registry[role]["provider_id"] == provider_id:
                        registry[role] = None
                self._write(registry)
            return self.public_registry()
