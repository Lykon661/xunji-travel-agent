"""The same local provider/model configuration API as the review Agent."""
from fastapi import APIRouter, HTTPException, Request

from .llm import ModelRuntimeError, StructuredOutputError, model_failure_message
from .model_registry import (ModelProbeInput, ModelRegistryInput, ModelRemovalInput,
                             OAuthAnswerInput, ProviderInput, ProviderLogoutInput)

router = APIRouter(prefix="/system")


def services(request):
    return request.app.state.planner.models


def ensure_idle(request):
    planner = request.app.state.planner
    if any(not task.done() for task in planner.tasks.values()) or any(lock.locked() for lock in planner.gates.values()):
        raise HTTPException(409, "正在处理旅行任务，请结束后再保存、切换模型或修改订阅登录")


@router.get("/model-registry")
def registry(request: Request):
    return services(request).config_store.public_registry()


@router.get("/model-catalog")
def catalog(request: Request):
    try:
        return services(request).client.catalog()
    except ModelRuntimeError as exc:
        raise HTTPException(503, model_failure_message(exc)) from exc


@router.put("/model-registry")
async def save(request: Request, body: ModelRegistryInput):
    ensure_idle(request)
    try:
        return services(request).config_store.save_registry(body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, "无法保存配置，请检查 backend/data 目录权限") from exc


@router.post("/model-registry/check")
def check(request: Request, body: ModelProbeInput):
    models = services(request)
    try:
        config = models.config_store.probe_candidate(body.provider.model_dump(), body.model.model_dump())
        return models.client.check(config)
    except StructuredOutputError as exc:
        raise HTTPException(502, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ModelRuntimeError as exc:
        raise HTTPException(502, model_failure_message(exc)) from exc


@router.post("/model-registry/discover")
def discover(request: Request, body: ModelProbeInput):
    models = services(request)
    try:
        config = models.config_store.probe_candidate(body.provider.model_dump(), body.model.model_dump(), require_model=False)
        return models.client.discover(config)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except ModelRuntimeError as exc:
        raise HTTPException(502, model_failure_message(exc)) from exc


@router.post("/model-registry/oauth/start")
async def login(request: Request, body: ProviderInput):
    ensure_idle(request)
    try:
        return request.app.state.model_oauth.start(body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ModelRuntimeError) as exc:
        raise HTTPException(503, "无法启动订阅登录，请检查 Node.js 和 model-runtime 依赖") from exc


@router.get("/model-registry/oauth/{identity}")
def login_state(request: Request, identity: str):
    return request.app.state.model_oauth.public(identity)


@router.post("/model-registry/oauth/{identity}/answer")
def login_answer(request: Request, identity: str, body: OAuthAnswerInput):
    try:
        return request.app.state.model_oauth.answer(identity, body.prompt_id, body.value)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(503, "登录进程已结束，请重新登录") from exc


@router.delete("/model-registry/oauth/{identity}")
def cancel_login(request: Request, identity: str):
    return request.app.state.model_oauth.cancel(identity)


@router.post("/model-registry/providers/{provider_id}/logout")
async def logout(request: Request, provider_id: str, body: ProviderLogoutInput):
    ensure_idle(request)
    models = services(request)
    try:
        existing = next((item for item in models.config_store.registry["providers"] if item["id"] == provider_id), None)
        models.config_store.oauth_path(provider_id)
        request.app.state.model_oauth.logout(provider_id, existing["provider"] if existing else body.provider)
        return models.config_store.deactivate_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ModelRuntimeError) as exc:
        raise HTTPException(503, "无法退出订阅登录，请检查运行依赖和数据目录权限") from exc


@router.delete("/model-registry/providers/{provider_id}")
async def remove_provider(request: Request, provider_id: str, body: ProviderLogoutInput):
    ensure_idle(request)
    models = services(request)
    try:
        models.config_store.oauth_path(provider_id)
        existing = next((item for item in models.config_store.registry["providers"] if item["id"] == provider_id), None)
        request.app.state.model_oauth.logout(provider_id, existing["provider"] if existing else body.provider)
        return models.config_store.remove_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (OSError, ModelRuntimeError) as exc:
        raise HTTPException(503, "无法删除服务商，请检查运行依赖和数据目录权限") from exc


@router.delete("/model-registry/providers/{provider_id}/models")
async def remove_model(request: Request, provider_id: str, body: ModelRemovalInput):
    ensure_idle(request)
    try:
        return services(request).config_store.remove_model(provider_id, body.model_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, "无法移除模型，请检查数据目录权限") from exc
