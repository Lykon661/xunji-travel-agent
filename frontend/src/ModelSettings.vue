<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, ApiError } from './api'
import type { CatalogModel, CatalogProvider, ManagedModel, ManagedProvider, ModelApi, ModelConfiguration, ModelRegistry, ModelSelection, ModelOAuthSession, ThinkingLevel } from './types'

type ModelDraft = ManagedModel & { savedId?: string }
type ProviderDraft = Omit<ManagedProvider, 'models'> & { models: ModelDraft[]; keyInput: string; clearKey: boolean; headerInput: string; clearHeaders: boolean }
const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ close: []; saved: [configuration: ModelConfiguration] }>()
const panel = ref<HTMLFormElement | null>(null)
const loading = ref(true)
const catalogLoading = ref(false)
const busy = ref<'save' | 'check' | 'discover' | 'remove' | 'login' | 'logout' | 'answer' | ''>('')
const loadError = ref('')
const catalogError = ref('')
const error = ref('')
const feedback = ref('')
const catalog = ref<CatalogProvider[]>([])
const stored = ref<ModelRegistry | null>(null)
const providers = ref<ProviderDraft[]>([])
const selectedProviderId = ref('')
const selectedModelIndex = ref(0)
const selection = ref<ModelSelection | null>(null)
const visionSelection = ref<ModelSelection | null>(null)
const autoSelect = ref(true)
const newProviderType = ref('custom')
const modelSearch = ref('')
const catalogChoice = ref('')
const showKey = ref(false)
const dirty = ref(false)
const confirmClose = ref(false)
const removal = ref<'provider' | 'model' | ''>('')
const removalProviderId = ref('')
const removalModel = ref<ModelDraft | null>(null)
const authSession = ref<ModelOAuthSession | null>(null)
const authInput = ref('')
const authError = ref('')
const authPending = computed(() => !!authSession.value && ['pending', 'waiting_input'].includes(authSession.value.status))
const subscriptionProviders = computed(() => catalog.value.filter(provider => provider.subscription && provider.supports_oauth))
const removalName = computed(() => {
  const provider = providers.value.find(item => item.id === removalProviderId.value)
  return removal.value === 'provider' ? provider?.name : removalModel.value?.name || removalModel.value?.id || '待填写模型'
})
const discovered = ref<{ id: string; name: string }[]>([])
const discoveredFor = ref('')
const selectedProvider = computed(() => providers.value.find(item => item.id === selectedProviderId.value))
const selectedModel = computed(() => selectedProvider.value?.models[selectedModelIndex.value])
const disabled = computed(() => loading.value || !!busy.value || !!loadError.value)
const builtin = computed(() => catalog.value.find(item => item.id === selectedProvider.value?.provider))
const catalogMatches = computed(() => (builtin.value?.models || []).filter(item =>
  `${item.id} ${item.name}`.toLowerCase().includes(modelSearch.value.toLowerCase())))
const discoveryIdentity = computed(() => `${selectedProvider.value?.id}|${selectedProvider.value?.base_url}`)
const remoteModels = computed(() => discoveredFor.value === discoveryIdentity.value ? discovered.value : [])
const discoveryApi = computed(() => selectedModel.value?.api || builtin.value?.models[0]?.api || 'openai-completions')
const chosenModels = computed(() => {
  const result: { id: string; name: string; known?: CatalogModel }[] = catalogMatches.value.map(item => ({ id: item.id, name: item.name, known: item }))
  for (const item of remoteModels.value) {
    if (!result.some(existing => existing.id === item.id) && `${item.id} ${item.name}`.toLowerCase().includes(modelSearch.value.toLowerCase())) result.push(item)
  }
  return result
})
const knownModel = computed(() => builtin.value?.models.find(item => item.id === selectedModel.value?.id && item.api === selectedModel.value?.api
  && item.base_url.replace(/\/+$/, '') === selectedProvider.value?.base_url.trim().replace(/\/+$/, '')))
const thinkingOptions = computed(() => {
  const labels: Record<ThinkingLevel, string> = { default: '服务商默认', off: '关闭思考', minimal: '最低', low: '低', medium: '中', high: '高', xhigh: '更高', max: '最高' }
  const levels: ThinkingLevel[] = ['default', ...(knownModel.value?.thinking_levels || ['off', 'minimal', 'low', 'medium', 'high'] as ThinkingLevel[])]
  if (selectedModel.value && !levels.includes(selectedModel.value.thinking_level)) levels.push(selectedModel.value.thinking_level)
  return [...new Set(levels)].map(value => ({ value, label: labels[value] }))
})
const activeName = computed(() => {
  if (!selection.value) return '暂不使用模型'
  const provider = providers.value.find(item => item.id === selection.value?.provider_id)
  const model = provider?.models.find(item => item.id === selection.value?.model_id)
  return model ? `${provider?.name} / ${model.name || model.id}` : '所选模型已移除，请重新选择'
})
const isSelectedActive = computed(() => !!selectedModel.value && !!selectedModel.value.id
  && selection.value?.provider_id === selectedProvider.value?.id && selection.value?.model_id === selectedModel.value.id.trim())
const protocolLabels: Record<ModelApi, string> = {
  'openai-completions': 'OpenAI Chat Completions（兼容接口）', 'openai-responses': 'OpenAI Responses',
  'openai-codex-responses': 'ChatGPT / Codex 订阅接口',
  'anthropic-messages': 'Anthropic Messages', 'google-generative-ai': 'Google Gemini', 'mistral-conversations': 'Mistral Conversations',
}
const compatBooleans = computed(() => selectedModel.value?.api === 'openai-responses'
  ? [{ key: 'supportsDeveloperRole', name: 'Developer 角色' }, { key: 'supportsStrictMode', name: '严格工具定义' }]
  : [{ key: 'supportsDeveloperRole', name: 'Developer 角色' }, { key: 'supportsReasoningEffort', name: 'reasoning_effort 参数' },
    { key: 'supportsUsageInStreaming', name: '流式 token 统计' }, { key: 'supportsStore', name: 'store 参数' }, { key: 'supportsStrictMode', name: '严格工具定义' }])
const thinkingFormats = ['openai', 'openrouter', 'deepseek', 'together', 'zai', 'qwen', 'chat-template', 'qwen-chat-template', 'string-thinking', 'ant-ling']
let previousFocus: HTMLElement | null = null
let previousOverflow = ''
let applying = false
let authPoll: ReturnType<typeof setTimeout> | undefined
const controller = new AbortController()

function blankModel(apiType: ModelApi = 'openai-completions'): ManagedModel {
  return { id: '', name: '', api: apiType, context_window: 32768, max_tokens: 8192, reasoning: false,
    supports_images: false, thinking_level: 'default', temperature: null, compat: {} }
}
function requestClose() {
  if (busy.value) return
  if (dirty.value || authPending.value) confirmClose.value = true
  else emit('close')
}
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') { if (confirmClose.value) confirmClose.value = false; else requestClose() }
  if (event.key !== 'Tab') return
  const elements = panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary')
  const targets = Array.from(elements || []).filter(element => element.getClientRects().length && !element.closest('fieldset:disabled'))
  const first = targets[0], last = targets[targets.length - 1]
  if (!first || !last) { event.preventDefault(); return }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}
function errorMessage(cause: unknown) {
  if (cause instanceof ApiError && [404, 405].includes(cause.status)) return '当前后端尚未加载新版模型配置接口。请结束旅行规划任务后，用 start.bat 重启后端，再重新读取。'
  if (cause instanceof Error && ['TimeoutError', 'AbortError'].includes(cause.name)) return '请求超时，请确认后端与模型服务可用后重试。'
  return cause instanceof Error ? cause.message : String(cause)
}
function selectProvider(identity: string) {
  selectedProviderId.value = identity; selectedModelIndex.value = 0
  modelSearch.value = ''; catalogChoice.value = ''; removal.value = ''; showKey.value = false
}
function addProvider() {
  const subscription = newProviderType.value.startsWith('oauth:')
  const type = newProviderType.value.replace(/^oauth:/, '')
  const preset = catalog.value.find(item => item.id === type)
  const local = type === 'ollama' || type === 'lmstudio'
  const provider: ProviderDraft = { id: `provider-${crypto.randomUUID()}`, name: subscription ? preset?.oauth_name || preset?.name || '订阅连接' : preset?.name || (type === 'ollama' ? 'Ollama' : type === 'lmstudio' ? 'LM Studio' : '自定义服务'),
    provider: preset?.id || 'custom', base_url: preset?.base_url || (type === 'ollama' ? 'http://127.0.0.1:11434/v1' : type === 'lmstudio' ? 'http://127.0.0.1:1234/v1' : ''),
    credential_mode: subscription ? 'oauth' : local ? 'none' : 'api_key', timeout_seconds: 90, models: [],
    supports_oauth: preset?.supports_oauth || false, oauth_logged_in: false,
    has_api_key: false, has_headers: false, env_key: preset?.env_key || null, credential_source: 'none', configured: false,
    keyInput: '', clearKey: false, headerInput: '', clearHeaders: false }
  providers.value.push(provider); selectProvider(provider.id)
}
function addModel(known?: CatalogModel, identity = '') {
  const provider = selectedProvider.value
  if (!provider) return
  const id = known?.id || identity
  const existing = id ? provider.models.findIndex(model => model.id === id) : -1
  if (existing >= 0) { selectedModelIndex.value = existing; return }
  const model = blankModel(known?.api || provider.models[0]?.api || builtin.value?.models[0]?.api)
  Object.assign(model, { id, name: known?.name || id })
  if (known) Object.assign(model, { context_window: Math.min(known.context_window, 2000000), max_tokens: Math.min(known.max_tokens, 8192),
    reasoning: known.reasoning, supports_images: known.supports_images })
  // Preserve the connection URL and all existing models when selecting from a catalog.
  provider.models.push(model); selectedModelIndex.value = provider.models.length - 1
  catalogChoice.value = ''; removal.value = ''
}
function importChoice() {
  const item = chosenModels.value.find(model => model.id === catalogChoice.value)
  if (item) addModel(item.known, item.id)
}
function askRemoval(kind: 'provider' | 'model') {
  if (!selectedProvider.value) return
  removalProviderId.value = selectedProvider.value.id
  removalModel.value = selectedModel.value || null
  removal.value = kind
}
async function removeSelected() {
  const provider = providers.value.find(item => item.id === removalProviderId.value), model = removalModel.value
  const kind = removal.value
  if (!provider || !kind || disabled.value || props.active) return
  busy.value = 'remove'; error.value = ''; feedback.value = ''
  try {
    let result: ModelRegistry | undefined
    if (kind === 'provider') {
      // Also remove tokens belonging to a newly added, not-yet-saved subscription.
      if (storedProvider(provider) || provider.oauth_logged_in || authSession.value?.provider_id === provider.id) {
        result = await api<ModelRegistry>(`/system/model-registry/providers/${encodeURIComponent(provider.id)}`, {
          method: 'DELETE', body: JSON.stringify({ provider: provider.provider }), signal: controller.signal,
        })
      }
      if (authSession.value?.provider_id === provider.id) { stopAuthPoll(); authSession.value = null }
      if (selection.value?.provider_id === provider.id) selection.value = null
      if (visionSelection.value?.provider_id === provider.id) visionSelection.value = null
      providers.value = providers.value.filter(item => item.id !== provider.id)
      selectProvider(providers.value[0]?.id || '')
    } else if (model) {
      if (model.savedId !== undefined) {
        result = await api<ModelRegistry>(`/system/model-registry/providers/${encodeURIComponent(provider.id)}/models`, {
          method: 'DELETE', body: JSON.stringify({ model_id: model.savedId }), signal: controller.signal,
        })
      }
      if (selection.value?.provider_id === provider.id && [model.id.trim(), model.savedId].includes(selection.value.model_id)) selection.value = null
      if (visionSelection.value?.provider_id === provider.id && [model.id.trim(), model.savedId].includes(visionSelection.value.model_id)) visionSelection.value = null
      const index = provider.models.indexOf(model)
      if (index >= 0) provider.models.splice(index, 1)
      selectedModelIndex.value = Math.min(Math.max(0, selectedModelIndex.value), Math.max(0, provider.models.length - 1))
    }
    if (result) { stored.value = result; emit('saved', result.current) }
    dirty.value = hasDraftChanges(); removal.value = ''; removalModel.value = null
    feedback.value = kind === 'provider' ? '服务商已删除。其他未保存修改仍保留。' : '模型已移除。其他未保存修改仍保留。'
  } catch (cause) { if (!controller.signal.aborted) error.value = errorMessage(cause) }
  finally { busy.value = '' }
}
function activateSelected() {
  autoSelect.value = true
  if (selectedProvider.value && selectedModel.value?.id.trim()) selection.value = {
    provider_id: selectedProvider.value.id, model_id: selectedModel.value.id.trim(),
  }
}
function activateVision() {
  if (selectedProvider.value && selectedModel.value?.id.trim() && selectedModel.value.supports_images) visionSelection.value = {
    provider_id: selectedProvider.value.id, model_id: selectedModel.value.id.trim(),
  }
}
function renameModel(identity: string) {
  const model = selectedModel.value
  if (!model) return
  if (isSelectedActive.value && selection.value) selection.value.model_id = identity.trim()
  if (visionSelection.value?.provider_id === selectedProvider.value?.id && visionSelection.value?.model_id === model.id.trim()) visionSelection.value.model_id = identity.trim()
  model.id = identity
}
function changeProtocol() { if (selectedModel.value) selectedModel.value.compat = {} }
function compatValue(name: string) {
  const value = selectedModel.value?.compat[name]
  return value === undefined ? '' : String(value)
}
function setCompat(name: string, value: string) {
  if (!selectedModel.value) return
  if (!value) delete selectedModel.value.compat[name]
  else selectedModel.value.compat[name] = value === 'true' ? true : value === 'false' ? false : value
}
function storedProvider(provider: ProviderDraft) { return stored.value?.providers.find(item => item.id === provider.id) }
function canRetain(provider: ProviderDraft) {
  const old = storedProvider(provider)
  return !!old && old.provider === provider.provider && old.base_url.replace(/\/+$/, '') === provider.base_url.trim().replace(/\/+$/, '')
}
function modelPayload(model: ModelDraft) {
  const { savedId, ...values } = model
  return { ...values, id: model.id.trim(), name: model.name.trim() }
}
function hasDraftChanges() {
  if (!stored.value) return providers.value.length > 0
  if (providers.value.some(item => item.keyInput.trim() || item.clearKey || item.headerInput.trim() || item.clearHeaders)) return true
  const editable = (items: ManagedProvider[] | ProviderDraft[]) => items.map(item => ({ id: item.id, name: item.name,
    provider: item.provider, base_url: item.base_url, credential_mode: item.credential_mode,
    timeout_seconds: item.timeout_seconds, models: item.models.map(modelPayload) }))
  return JSON.stringify({ providers: editable(providers.value), active: selection.value, vision: visionSelection.value, auto_select: autoSelect.value })
    !== JSON.stringify({ providers: editable(stored.value.providers), active: stored.value.active, vision: stored.value.vision || null, auto_select: stored.value.auto_select ?? true })
}
function providerPayload(provider: ProviderDraft) {
  let headers: Record<string, string> | null = provider.clearHeaders ? {} : null
  if (provider.headerInput.trim() && !provider.clearHeaders) {
    const parsed: unknown = JSON.parse(provider.headerInput)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) || Object.values(parsed).some(value => typeof value !== 'string')) throw new Error('自定义请求头必须是 JSON 对象，所有值均为字符串。')
    headers = parsed as Record<string, string>
  }
  if (headers === null && !canRetain(provider)) headers = {}
  return { id: provider.id, name: provider.name.trim(), provider: provider.provider, base_url: provider.base_url.trim(),
    credential_mode: provider.credential_mode, timeout_seconds: provider.timeout_seconds,
    api_key: provider.clearKey ? '' : provider.keyInput.trim() || (canRetain(provider) ? null : ''), headers,
    models: provider.models.map(modelPayload) }
}
function adopt(registry: ModelRegistry) {
  applying = true
  const previous = selectedProviderId.value, previousModel = selectedModel.value?.id
  stored.value = registry
  providers.value = registry.providers.map(provider => ({ ...provider, models: provider.models.map(model => ({ ...model, compat: { ...model.compat }, savedId: model.id })), keyInput: '', clearKey: false, headerInput: '', clearHeaders: false }))
  selection.value = registry.active ? { ...registry.active } : null
  visionSelection.value = registry.vision ? { ...registry.vision } : null
  autoSelect.value = registry.auto_select ?? true
  selectProvider(providers.value.some(provider => provider.id === previous) ? previous : registry.active?.provider_id || providers.value[0]?.id || '')
  if (previousModel) selectedModelIndex.value = Math.max(0, selectedProvider.value?.models.findIndex(model => model.id === previousModel) ?? 0)
  dirty.value = false; applying = false
}
async function load() {
  if (busy.value || dirty.value) return
  loading.value = true; loadError.value = ''
  try { adopt(await api<ModelRegistry>('/system/model-registry', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]) })) }
  catch (cause) { if (!controller.signal.aborted) loadError.value = errorMessage(cause) }
  finally { loading.value = false }
}
async function loadCatalog() {
  catalogLoading.value = true; catalogError.value = ''
  try { catalog.value = await api<CatalogProvider[]>('/system/model-catalog', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(45000)]) }) }
  catch (cause) { if (!controller.signal.aborted) catalogError.value = errorMessage(cause) }
  finally { catalogLoading.value = false }
}
async function submit(action: 'save' | 'check' | 'discover') {
  if (disabled.value || (action === 'save' && props.active)) return
  // Check only the selected provider/model for probes; a draft elsewhere need not be complete.
  if (action === 'save' && !panel.value?.reportValidity()) return
  busy.value = action; error.value = ''; feedback.value = ''
  try {
    if (action === 'save') {
      const result = await api<ModelRegistry>('/system/model-registry', { method: 'PUT', body: JSON.stringify({
        providers: providers.value.map(providerPayload), active: selection.value, vision: visionSelection.value, auto_select: autoSelect.value,
      }), signal: controller.signal })
      adopt(result); feedback.value = result.current.configured ? '配置已保存，使用中的模型已生效。' : '配置已保存，目前暂不使用模型。'; emit('saved', result.current)
    } else {
      const provider = selectedProvider.value
      if (!provider) return
      const model = selectedModel.value || blankModel(discoveryApi.value)
      const body = JSON.stringify({ provider: { ...providerPayload(provider), models: [] }, model: modelPayload(model) })
      if (action === 'check') {
        if (!selectedModel.value) throw new Error('先添加或选择一个模型，再检查连接。')
        const result = await api<{ message: string }>('/system/model-registry/check', { method: 'POST', body, signal: controller.signal })
        feedback.value = `${result.message} 此操作不会保存或切换模型。`
      } else {
        const result = await api<{ models: { id: string; name: string }[]; message: string }>('/system/model-registry/discover', { method: 'POST', body, signal: controller.signal })
        discovered.value = result.models; discoveredFor.value = discoveryIdentity.value
        feedback.value = result.models.length ? result.message : '服务返回了空模型列表，仍可手动填写模型 ID。'
      }
    }
  } catch (cause) { if (!controller.signal.aborted) error.value = errorMessage(cause) }
  finally { busy.value = '' }
}
function stopAuthPoll() { if (authPoll !== undefined) clearTimeout(authPoll); authPoll = undefined }
function safeAuthUrl(value?: string) {
  if (!value) return ''
  try { const url = new URL(value); return url.protocol === 'https:' ? url.href : '' }
  catch { return '' }
}
function setAuthState(session: ModelOAuthSession) {
  if (session.prompt?.id !== authSession.value?.prompt?.id) authInput.value = session.prompt?.options?.[0]?.id || ''
  authSession.value = session
}
async function refreshAfterAuth(identity: string) {
  const result = await api<ModelRegistry>('/system/model-registry', { signal: controller.signal })
  if (authSession.value?.id !== identity || controller.signal.aborted) return
  stored.value = result
  const provider = providers.value.find(item => item.id === authSession.value?.provider_id)
  if (provider) { provider.oauth_logged_in = true; provider.configured = true }
  dirty.value = hasDraftChanges()
  emit('saved', result.current)
}
async function pollAuth(identity: string) {
  if (controller.signal.aborted || authSession.value?.id !== identity) return
  try {
    const result = await api<ModelOAuthSession>(`/system/model-registry/oauth/${identity}`, {
      signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
    })
    if (authSession.value?.id !== identity || controller.signal.aborted) return
    setAuthState(result); authError.value = ''
    if (result.status === 'connected') { await refreshAfterAuth(identity); return }
    if (!['pending', 'waiting_input'].includes(result.status)) return
  } catch (cause) {
    if (controller.signal.aborted || authSession.value?.id !== identity) return
    if (cause instanceof ApiError && cause.status === 404) {
      authSession.value = { ...authSession.value, status: 'failed', prompt: null, events: [], message: '登录会话已结束，请重新登录。' }
      return
    }
    authError.value = errorMessage(cause)
  }
  authPoll = setTimeout(() => { void pollAuth(identity) }, 1200)
}
async function startLogin() {
  const provider = selectedProvider.value
  if (!provider || disabled.value || props.active || authPending.value) return
  busy.value = 'login'; authError.value = ''; error.value = ''; stopAuthPoll()
  try {
    const session = await api<ModelOAuthSession>('/system/model-registry/oauth/start', { method: 'POST',
      body: JSON.stringify({ ...providerPayload(provider), models: [] }), signal: controller.signal })
    setAuthState(session); void pollAuth(session.id)
  } catch (cause) { if (!controller.signal.aborted) authError.value = errorMessage(cause) }
  finally { busy.value = '' }
}
async function answerAuth() {
  const session = authSession.value
  if (!session?.prompt || disabled.value) return
  busy.value = 'answer'; authError.value = ''
  try {
    const result = await api<ModelOAuthSession>(`/system/model-registry/oauth/${session.id}/answer`, {
      method: 'POST', body: JSON.stringify({ prompt_id: session.prompt.id, value: authInput.value }), signal: controller.signal,
    })
    if (authSession.value?.id === session.id && !['connected', 'failed', 'cancelled'].includes(authSession.value.status)) setAuthState(result)
    authInput.value = ''
  } catch (cause) { if (!controller.signal.aborted) authError.value = errorMessage(cause) }
  finally { busy.value = '' }
}
async function cancelAuth() {
  if (!authSession.value || disabled.value) return
  stopAuthPoll(); authError.value = ''
  const identity = authSession.value.id
  try {
    const result = await api<ModelOAuthSession>(`/system/model-registry/oauth/${identity}`, { method: 'DELETE', signal: controller.signal })
    if (authSession.value?.id === identity) setAuthState(result)
  } catch (cause) {
    if (!controller.signal.aborted) { authError.value = errorMessage(cause); void pollAuth(identity) }
  }
  authInput.value = ''
}
async function logoutProvider() {
  const provider = selectedProvider.value
  if (!provider || disabled.value || props.active) return
  busy.value = 'logout'; error.value = ''; authError.value = ''
  try {
    const result = await api<ModelRegistry>(`/system/model-registry/providers/${encodeURIComponent(provider.id)}/logout`, {
      method: 'POST', body: JSON.stringify({ provider: provider.provider }), signal: controller.signal,
    })
    stored.value = result; provider.oauth_logged_in = false; provider.configured = false
    if (selection.value?.provider_id === provider.id) selection.value = null
    if (visionSelection.value?.provider_id === provider.id) visionSelection.value = null
    if (authSession.value?.provider_id === provider.id) { stopAuthPoll(); authSession.value = null }
    dirty.value = hasDraftChanges(); feedback.value = '已退出订阅登录。'; emit('saved', result.current)
  } catch (cause) { if (!controller.signal.aborted) error.value = errorMessage(cause) }
  finally { busy.value = '' }
}
watch([providers, selection, visionSelection, autoSelect], () => {
  if (!applying) { dirty.value = hasDraftChanges(); feedback.value = ''; error.value = ''; confirmClose.value = false }
}, { deep: true, flush: 'sync' })
watch(selectedProviderId, () => { modelSearch.value = ''; catalogChoice.value = '' })
onMounted(async () => {
  previousFocus = document.activeElement as HTMLElement | null
  previousOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'
  document.addEventListener('keydown', key)
  panel.value?.focus()
  await Promise.allSettled([load(), loadCatalog()])
  await nextTick()
})
onUnmounted(() => {
  stopAuthPoll()
  if (authPending.value && authSession.value) void api(`/system/model-registry/oauth/${authSession.value.id}`, { method: 'DELETE', keepalive: true }).catch(() => {})
  controller.abort(); document.removeEventListener('keydown', key); document.body.style.overflow = previousOverflow
  authInput.value = ''
  for (const provider of providers.value) { provider.keyInput = ''; provider.headerInput = '' }
  if (previousFocus?.isConnected) previousFocus.focus()
})
</script>

<template>
  <div class="management-overlay" @click.self="requestClose">
    <form ref="panel" class="management-dialog model-settings provider-settings" role="dialog" aria-modal="true" aria-labelledby="model-settings-title" tabindex="-1" @submit.prevent="submit('save')">
      <header class="model-dialog-header">
        <div><h2 id="model-settings-title">模型配置</h2><p class="muted">保存服务商连接，为旅行规划选择合适的模型</p></div>
        <button type="button" class="icon-button" aria-label="关闭模型配置" :disabled="!!busy" @click="requestClose">×</button>
      </header>
      <div class="model-live"><span class="model-status-dot" :class="{ connected: stored?.current.configured }" aria-hidden="true"></span><div><small>正在使用</small><strong>{{ loading ? '正在读取…' : stored?.current.configured ? `${stored.current.provider_name} / ${stored.current.model_name || stored.current.model}` : '尚未接入模型' }}</strong></div><span v-if="dirty" class="model-draft-tag">有未保存修改</span></div>
      <div v-if="loadError" class="model-load-error" role="alert"><p>{{ loadError }}</p><button type="button" class="button subtle small" :disabled="loading" @click="load">重新读取配置</button></div>
      <p v-if="loading" class="model-loading" role="status">正在读取本机配置…</p>
      <div class="model-workspace-scroll" role="region" aria-label="服务商和模型配置" tabindex="0">
      <fieldset class="model-workspace" :disabled="disabled">
        <aside class="model-provider-sidebar">
          <div class="model-group-title"><h3>服务商</h3><span>{{ providers.length }}</span></div>
          <div class="model-provider-list">
            <button v-for="provider in providers" :key="provider.id" type="button" class="model-provider-row" :class="{ selected: provider.id === selectedProviderId }" :aria-pressed="provider.id === selectedProviderId" @click="selectProvider(provider.id)">
              <strong>{{ provider.name }}</strong><span>{{ provider.models.length }} 个模型 <b v-if="selection?.provider_id === provider.id">已选择</b></span>
            </button>
            <p v-if="!providers.length" class="muted">添加一个服务商，开始配置。</p>
          </div>
          <div class="model-add-provider"><label for="new-model-provider">添加连接</label><select id="new-model-provider" v-model="newProviderType"><optgroup v-if="subscriptionProviders.length" label="模型订阅登录"><option v-for="provider in subscriptionProviders" :key="`oauth-${provider.id}`" :value="`oauth:${provider.id}`">{{ provider.oauth_name || provider.name }}</option></optgroup><optgroup label="API Key / 本地服务"><option value="custom">自定义 / 兼容接口</option><option value="ollama">Ollama（本地）</option><option value="lmstudio">LM Studio（本地）</option><option v-for="provider in catalog.filter(item => item.id !== 'openai-codex')" :key="provider.id" :value="provider.id">{{ provider.name }}</option></optgroup></select><button type="button" class="button subtle small" @click="addProvider">添加服务商</button></div>
          <p v-if="catalogLoading" class="muted" role="status">正在加载内置目录…</p>
          <div v-if="catalogError" class="model-catalog-error"><p>内置目录暂不可用，可先添加自定义服务。</p><button type="button" class="text-button" :disabled="catalogLoading" @click="loadCatalog">重新加载目录</button><details><summary>错误详情</summary>{{ catalogError }}</details></div>
        </aside>
        <main v-if="selectedProvider" class="model-provider-content">
          <div class="model-group-title"><h3>连接设置</h3><button type="button" class="text-button danger" :disabled="active" @click="askRemoval('provider')">删除服务商</button></div>
          <div class="model-connection-grid">
            <label>显示名称<input v-model="selectedProvider.name" required maxlength="100" /></label>
            <div class="model-provider-type"><small>服务类型</small><strong>{{ builtin?.name || (selectedProvider.provider === 'custom' ? '自定义 / 本地服务' : selectedProvider.provider) }}</strong></div>
            <label class="model-span-all">接口根地址<input v-model="selectedProvider.base_url" type="url" placeholder="https://api.example.com/v1" maxlength="2000" :readonly="selectedProvider.credential_mode === 'oauth'" /><small>{{ selectedProvider.credential_mode === 'oauth' ? '订阅登录使用官方接口，账号专属地址由服务商认证自动处理。' : '填写根地址，不带 /chat/completions、/responses 或 /messages。' }}</small></label>
            <label>接入方式<select v-model="selectedProvider.credential_mode" @change="selectedProvider.credential_mode === 'oauth' && builtin && (selectedProvider.base_url = builtin.base_url)"><option v-if="selectedProvider.provider !== 'openai-codex'" value="api_key">API Key</option><option v-if="selectedProvider.supports_oauth || builtin?.supports_oauth" value="oauth">模型订阅登录 / OAuth</option><option v-if="selectedProvider.env_key" value="environment">后端环境变量</option><option v-if="selectedProvider.provider === 'custom'" value="none">无需密钥（本地服务）</option></select></label>
            <label>请求超时（秒）<input v-model.number="selectedProvider.timeout_seconds" type="number" min="10" max="600" required /></label>
            <div v-if="selectedProvider.credential_mode === 'api_key'" class="model-span-all model-credential">
              <label>API Key<span class="model-key-input"><input v-model="selectedProvider.keyInput" :type="showKey ? 'text' : 'password'" autocomplete="off" spellcheck="false" :disabled="selectedProvider.clearKey" :placeholder="canRetain(selectedProvider) && selectedProvider.has_api_key ? '已保存密钥，留空保留；输入新密钥替换' : '填写此服务商的 API Key'" /><button type="button" class="text-button" @click="showKey = !showKey">{{ showKey ? '隐藏' : '显示' }}</button></span></label>
              <label v-if="canRetain(selectedProvider) && selectedProvider.has_api_key" class="model-checkbox"><input v-model="selectedProvider.clearKey" type="checkbox" />清除已保存密钥</label>
              <small v-if="selectedProvider.has_api_key && !canRetain(selectedProvider)">接口地址已修改，请为新地址重新填写密钥。</small>
            </div>
            <p v-if="selectedProvider.credential_mode === 'environment'" class="muted model-span-all">从后端读取 {{ selectedProvider.env_key }}。当前{{ selectedProvider.has_api_key && storedProvider(selectedProvider)?.credential_mode === 'environment' ? '已配置' : '保存或检查连接时确认' }}。</p>
          </div>
          <section v-if="selectedProvider.credential_mode === 'oauth'" class="model-oauth-card" aria-label="模型订阅登录">
            <div class="model-group-title"><h3>{{ builtin?.oauth_name || '模型订阅登录' }}</h3><span :class="selectedProvider.oauth_logged_in ? 'oauth-connected' : ''">{{ selectedProvider.oauth_logged_in ? '已登录' : '尚未登录' }}</span></div>
            <p class="muted">使用你的订阅账号授权，登录凭据保存在本机后端。</p>
            <div class="model-oauth-actions"><button type="button" class="button primary small" :disabled="active || authPending" @click="startLogin">{{ busy === 'login' ? '准备登录…' : selectedProvider.oauth_logged_in ? '重新登录' : builtin?.oauth_label || '登录订阅账号' }}</button><button v-if="selectedProvider.oauth_logged_in" type="button" class="button subtle small" :disabled="active" @click="logoutProvider">{{ busy === 'logout' ? '退出中…' : '退出登录' }}</button></div>
            <div v-if="authSession?.provider_id === selectedProvider.id" class="model-oauth-flow" aria-live="polite">
              <p>{{ authSession.message }}</p>
              <div v-for="(event, index) in authSession.events" :key="index" class="model-oauth-event">
                <template v-if="event.type === 'auth_url'"><a v-if="safeAuthUrl(event.url)" class="button subtle small" :href="safeAuthUrl(event.url)" target="_blank" rel="noopener noreferrer">打开授权页面</a><p v-if="event.instructions" class="muted">{{ event.instructions }}</p></template>
                <template v-else-if="event.type === 'device_code'"><p>在授权页面输入设备验证码：<strong class="model-device-code">{{ event.userCode }}</strong></p><a v-if="safeAuthUrl(event.verificationUri)" :href="safeAuthUrl(event.verificationUri)" target="_blank" rel="noopener noreferrer">打开设备授权页面</a></template>
                <template v-else><p v-if="event.message" class="muted">{{ event.message }}</p><a v-for="(link, linkIndex) in event.links || []" :key="linkIndex" :href="safeAuthUrl(link.url) || undefined" target="_blank" rel="noopener noreferrer">{{ link.label || '打开登录说明' }}</a></template>
              </div>
              <div v-if="authSession.prompt" class="model-oauth-prompt">
                <label>{{ authSession.prompt.message }}<select v-if="authSession.prompt.type === 'select'" v-model="authInput"><option v-for="option in authSession.prompt.options" :key="option.id" :value="option.id">{{ option.label }}{{ option.description ? ` — ${option.description}` : '' }}</option></select><textarea v-else-if="authSession.prompt.type === 'manual_code'" v-model="authInput" :placeholder="authSession.prompt.placeholder || '粘贴授权码或浏览器回调地址'" rows="2" autocomplete="off" spellcheck="false" /><input v-else v-model="authInput" :type="authSession.prompt.type === 'secret' ? 'password' : 'text'" :placeholder="authSession.prompt.placeholder" autocomplete="off" spellcheck="false" @keydown.enter.prevent="answerAuth" /></label>
                <button type="button" class="button primary small" @click="answerAuth">{{ busy === 'answer' ? '提交中…' : '继续登录' }}</button>
              </div>
              <button v-if="authPending" type="button" class="text-button" @click="cancelAuth">取消登录</button>
            </div>
            <p v-if="authError" class="document-error" role="alert">{{ authError }}</p>
          </section>
          <details class="model-header-settings"><summary>自定义请求头</summary><label>替换请求头（JSON 对象）<textarea v-model="selectedProvider.headerInput" rows="3" autocomplete="off" spellcheck="false" :disabled="selectedProvider.clearHeaders" :placeholder="selectedProvider.has_headers && canRetain(selectedProvider) ? '已保存请求头，留空保留；值不会回显' : '{&quot;X-Client&quot;: &quot;review-agent&quot;}'" /></label><label class="model-checkbox"><input v-model="selectedProvider.clearHeaders" type="checkbox" />清除已保存请求头</label><small>同一连接下的模型共用；认证信息填写在 API Key 中。</small></details>
          <section class="model-catalog-section">
            <div class="model-group-title"><h3>添加模型</h3><button v-if="selectedProvider.credential_mode !== 'oauth'" type="button" class="text-button" :disabled="!['openai-completions', 'openai-responses'].includes(discoveryApi)" @click="submit('discover')">{{ busy === 'discover' ? '获取中…' : '获取服务模型列表' }}</button></div>
            <div class="model-catalog-controls"><input v-model="modelSearch" type="search" placeholder="搜索内置目录或已获取的模型" aria-label="搜索模型目录" /><select v-model="catalogChoice" aria-label="选择目录中的模型"><option value="">{{ chosenModels.length ? `选择模型（${chosenModels.length}）` : '暂无目录，可手动添加' }}</option><option v-for="model in chosenModels" :key="model.id" :value="model.id">{{ model.name }} · {{ model.id }}{{ model.known?.supports_images ? ' · 视觉' : '' }}</option></select><button type="button" class="button subtle small" :disabled="!catalogChoice" @click="importChoice">添加</button></div>
            <div class="model-catalog-note"><small>{{ selectedProvider.credential_mode === 'oauth' ? '订阅模型从内置目录添加，账号权限由服务商决定。' : '目录不会验证模型权限。获取列表仅请求 /models，不发送问答。' }}</small><button v-if="selectedProvider.credential_mode !== 'oauth'" type="button" class="text-button" @click="addModel()">手动添加模型</button></div>
          </section>
          <section class="model-editor-section">
            <div class="model-group-title"><h3>已添加模型</h3><span>{{ selectedProvider.models.length }}</span></div>
            <div class="model-saved-list"><button v-for="(model, index) in selectedProvider.models" :key="index" type="button" class="model-saved-row" :class="{ selected: selectedModelIndex === index }" :aria-pressed="selectedModelIndex === index" @click="selectedModelIndex = index; removal = ''"><span>{{ model.name || model.id || '待填写模型' }}</span><small>{{ model.supports_images ? '文字 + 图片' : '文字' }}{{ model.reasoning ? ' / 推理' : '' }}</small><b v-if="selection?.provider_id === selectedProvider.id && selection?.model_id === model.id && model.id">已选择</b></button></div>
            <p v-if="!selectedModel" class="model-empty muted">从目录添加一个模型，或手动填写服务商提供的模型 ID。</p>
            <div v-if="selectedModel" class="model-editor">
              <div class="model-editor-title"><h3>{{ selectedModel.name || selectedModel.id || '新模型' }}</h3><button type="button" class="text-button danger" :disabled="active" @click="askRemoval('model')">移除模型</button></div>
              <div class="model-connection-grid">
                <label>模型 ID<input :value="selectedModel.id" required maxlength="200" :readonly="selectedProvider.credential_mode === 'oauth'" placeholder="服务商提供的准确模型 ID" @input="renameModel(($event.target as HTMLInputElement).value)" /></label>
                <label>显示名称<input v-model="selectedModel.name" maxlength="200" placeholder="可选，方便区分模型" /></label>
                <label class="model-span-all">接口协议<select v-model="selectedModel.api" :disabled="selectedProvider.credential_mode === 'oauth'" @change="changeProtocol"><option v-for="(label, value) in protocolLabels" :key="value" :value="value">{{ label }}</option></select></label>
                <label>上下文窗口（tokens）<input v-model.number="selectedModel.context_window" type="number" min="4096" max="2000000" required /></label>
                <label>最大输出（tokens）<input v-model.number="selectedModel.max_tokens" type="number" min="256" :max="Math.min(selectedModel.context_window, 200000)" required /></label>
                <label class="model-checkbox"><input v-model="selectedModel.supports_images" type="checkbox" />支持图片输入</label>
                <label class="model-checkbox"><input v-model="selectedModel.reasoning" type="checkbox" @change="!selectedModel.reasoning && (selectedModel.thinking_level = 'default')" />具备推理能力</label>
                <label v-if="selectedModel.reasoning">思考强度<select v-model="selectedModel.thinking_level"><option v-for="option in thinkingOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select><small>可用强度取决于模型与服务商。</small></label>
                <label>温度（可选）<input :value="selectedModel.temperature ?? ''" type="number" min="0" max="2" step="0.05" placeholder="自动" @input="selectedModel.temperature = ($event.target as HTMLInputElement).value === '' ? null : Number(($event.target as HTMLInputElement).value)" /><small>留空使用应用默认值；部分推理模型不接受温度。</small></label>
              </div>
              <details v-if="['openai-completions', 'openai-responses'].includes(selectedModel.api)" class="model-compat-settings"><summary>兼容参数</summary><p class="muted">自动使用协议与内置模型默认值。服务不支持某个参数时再覆盖。</p><div class="model-connection-grid"><label v-for="item in compatBooleans" :key="item.key">{{ item.name }}<select :value="compatValue(item.key)" @change="setCompat(item.key, ($event.target as HTMLSelectElement).value)"><option value="">自动</option><option value="true">支持</option><option value="false">不支持</option></select></label><template v-if="selectedModel.api === 'openai-completions'"><label>输出上限字段<select :value="compatValue('maxTokensField')" @change="setCompat('maxTokensField', ($event.target as HTMLSelectElement).value)"><option value="">自动</option><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></select></label><label>思考参数格式<select :value="compatValue('thinkingFormat')" @change="setCompat('thinkingFormat', ($event.target as HTMLSelectElement).value)"><option value="">自动</option><option v-for="format in thinkingFormats" :key="format" :value="format">{{ format }}</option></select></label></template></div></details>
              <div class="model-select-action"><small>视觉与推理能力需与服务实际支持一致。</small><button type="button" class="button subtle" :disabled="!selectedModel.id.trim() || !selectedModel.supports_images" @click="activateVision">选择用于攻略图片</button><button type="button" class="button" :class="isSelectedActive ? 'subtle' : 'primary'" :disabled="!selectedModel.id.trim() || isSelectedActive" @click="activateSelected">{{ isSelectedActive ? '已选择用于旅行规划' : '选择用于旅行规划' }}</button></div>
            </div>
          </section>
        </main>
        <div v-else class="model-welcome"><h3>把常用模型留在这里</h3><p>先在左侧添加服务商，再填写连接信息和模型。可以保留多个连接，按旅行规划需要切换。</p></div>
      </fieldset>
      </div>
      <footer class="model-dialog-footer">
        <div v-if="removal" class="model-inline-confirm model-removal-confirm" role="alert"><p>{{ removal === 'provider' ? '删除服务商' : '移除模型' }}“{{ removalName }}”？已保存的配置会立即删除。</p><button type="button" class="button subtle small" :disabled="!!busy" @click="removal = ''">取消</button><button type="button" class="button subtle danger small" :disabled="disabled || active" @click="removeSelected">{{ busy === 'remove' ? '删除中…' : '确认删除' }}</button></div>
        <div v-if="authPending && authSession && selectedProviderId !== authSession.provider_id" class="model-auth-resume"><span>正在登录：{{ authSession.provider_name }}</span><button type="button" class="text-button" :disabled="!!busy" @click="selectProvider(authSession.provider_id)">查看授权步骤</button></div>
        <div class="model-pending"><div><small>保存后使用</small><strong>{{ activeName }}</strong></div><button v-if="selection" type="button" class="text-button" :disabled="disabled" @click="selection = null; autoSelect = false">暂不使用模型</button></div>
        <div class="model-pending model-vision-selection"><label>攻略图片解析模型<select v-model="visionSelection"><option :value="null">跟随旅行规划模型（须支持图片）</option><template v-for="provider in providers" :key="provider.id"><option v-for="model in provider.models.filter(item => item.supports_images && item.id.trim())" :key="model.id" :value="{provider_id:provider.id,model_id:model.id.trim()}">{{ provider.name }} / {{ model.name || model.id }}</option></template></select></label></div>
        <p v-if="active" class="document-error">正在处理旅行规划任务，请完成后再保存或切换模型。</p>
        <p v-if="error" class="document-error" role="alert">{{ error }}</p><p v-if="feedback" class="model-feedback" role="status">{{ feedback }}</p>
        <div v-if="confirmClose" class="model-inline-confirm" role="alert"><p>{{ authPending ? '关闭会取消进行中的登录。未保存修改会丢弃，已删除的配置不会恢复。' : '未保存修改会丢弃，已删除的配置不会恢复。是否关闭？' }}</p><button type="button" class="button subtle small" @click="confirmClose = false">继续编辑</button><button type="button" class="button subtle small" @click="emit('close')">关闭</button></div>
        <div class="model-footer-actions"><p class="muted">密钥只保存在本机后端。检查连接会发送一次文字请求，可能产生少量费用。</p><button type="button" class="button subtle" :disabled="disabled || !selectedModel" @click="submit('check')">{{ busy === 'check' ? '连接中…' : '检查所选模型' }}</button><button class="button primary" :disabled="disabled || active || !dirty">{{ busy === 'save' ? '保存中…' : '保存配置' }}</button></div>
      </footer>
    </form>
  </div>
</template>

<style scoped>
.management-overlay { position: fixed; inset: 0; z-index: 100; background: #253a4066; display: flex; align-items: center; justify-content: center; padding: 24px; }
.management-dialog { border: 1px solid var(--line); border-radius: 12px; background: var(--surface); box-shadow: 0 18px 60px #253a4026; max-height: calc(100dvh - 48px); }
.model-settings { --surface: #fff; --surface-alt: #f5f8fa; --soft: #e8f0f2; --accent: #205b61; --accent-dark: #16464c; --accent-line: #79a5a6; --success: #36776c; --warning: #9c742d; --warning-soft: #fff6e5; --danger: #a04a4a; }
.model-settings .muted { color: var(--muted); font-size: 13px; line-height: 1.75; }
.model-settings .button { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 36px; padding: 9px 14px; border: 1px solid transparent; border-radius: 7px; font-size: 13px; font-weight: 600; line-height: 1.5; }
.model-settings .button.subtle { background: var(--surface); border-color: var(--line); color: var(--sea); }
.model-settings .button.small { min-height: 32px; padding: 6px 10px; font-size: 12px; }
.model-settings .icon-button { width: 36px; height: 36px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); font-size: 23px; flex-shrink: 0; }
.model-settings .danger, .model-settings .document-error { color: var(--danger); }
.model-settings .model-feedback { color: var(--success); }
.model-vision-selection { margin-top: 12px; }
.model-vision-selection label { width: 100%; }
@media (max-width: 560px) { .management-overlay { padding: 16px; } }

.provider-settings { width: min(1040px, 100%); height: min(900px, calc(100dvh - 48px)); min-height: 0; padding: 0; display: flex; flex-direction: column; overflow: hidden; }
.model-dialog-header, .model-live, .model-dialog-footer { flex-shrink: 0; }
.model-dialog-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; padding: 24px 28px 18px; }
.model-dialog-header p { margin-top: 4px; }
.model-live { display: flex; align-items: center; gap: 12px; margin: 0 28px 20px; padding: 12px 0; border-top: 1px solid var(--line); }
.model-live > div { display: flex; gap: 14px; align-items: baseline; min-width: 0; flex: 1; }
.model-live small { flex-shrink: 0; color: var(--muted); font-size: 12px; }
.model-live strong { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.model-status-dot { width: 8px; height: 8px; flex: 0 0 8px; border-radius: 50%; background: #9ba6b9; }
.model-status-dot.connected { background: var(--success); }
.model-draft-tag { color: var(--warning); font-size: 12px; flex-shrink: 0; }
.model-workspace-scroll { flex: 1 1 0; min-height: 0; min-width: 0; overflow-y: auto; scrollbar-gutter: stable; overscroll-behavior: contain; border-top: 1px solid var(--line); }
.model-workspace { display: grid; grid-template-columns: 215px minmax(0, 1fr); align-items: start; min-width: 0; margin: 0; padding: 0; border: 0; }
.model-provider-sidebar { min-width: 0; padding: 20px 16px; background: var(--surface-alt); border-right: 1px solid var(--line); }
.model-group-title { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 14px; }
.model-group-title > span { color: var(--muted); font-size: 12px; }
.model-group-title h3 { font-size: 14px; }
.model-provider-list { display: grid; gap: 5px; margin-bottom: 24px; }
.model-provider-row { display: grid; gap: 5px; width: 100%; padding: 12px; text-align: left; background: transparent; border: 1px solid transparent; border-radius: 8px; color: var(--ink); overflow-wrap: anywhere; }
.model-provider-row strong { font-size: 13px; font-weight: 600; }
.model-provider-row > span { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--muted); }
.model-provider-row b { color: var(--accent); font-weight: 500; }
.model-provider-row.selected { background: var(--surface); border-color: var(--accent-line); box-shadow: inset 3px 0 var(--accent); }
.model-provider-row:not(.selected):hover { background: var(--soft); }
.model-add-provider { display: grid; gap: 10px; }
.model-add-provider label { font-size: 12px; color: var(--muted); }
.model-add-provider select { width: 100%; font-size: 12px; padding-inline: 8px; }
.model-provider-sidebar > p { margin-top: 12px; font-size: 12px; }
.model-catalog-error { margin-top: 12px; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.model-catalog-error details { font-size: 11px; }
.model-provider-content { min-width: 0; padding: 22px 28px 28px; }
.model-connection-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px 20px; }
.model-workspace label { display: flex; flex-direction: column; gap: 7px; font-size: 12px; min-width: 0; }
.model-workspace input:not([type=checkbox]), .model-workspace select, .model-workspace textarea { width: 100%; min-width: 0; font-size: 13px; }
.model-workspace textarea { resize: vertical; }
.model-workspace small { font-size: 11px; color: var(--muted); line-height: 1.7; }
.model-span-all { grid-column: 1 / -1; }
.model-provider-type { padding: 1px 0 0; display: flex; flex-direction: column; justify-content: center; gap: 6px; }
.model-provider-type strong { font-size: 13px; font-weight: 500; }
.model-workspace .model-checkbox { display: flex; flex-direction: row; align-items: center; gap: 8px; }
.model-checkbox input { width: 16px; height: 16px; margin: 0; }
.model-credential > .model-checkbox { margin-top: 10px; }
.model-key-input { gap: 10px; }
.model-header-settings { margin-top: 18px; }
.model-header-settings summary, .model-compat-settings summary { font-size: 12px; cursor: pointer; color: #43516b; }
.model-header-settings > label { margin-top: 12px; }
.model-header-settings > small { display: block; margin-top: 8px; }
.model-oauth-card { margin-top: 18px; padding: 16px; background: var(--surface-alt); border: 1px solid var(--line); border-radius: 8px; }
.model-oauth-card .model-group-title { margin-bottom: 8px; }
.model-oauth-card .oauth-connected { color: var(--success); }
.model-oauth-actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
.model-oauth-flow { display: grid; gap: 12px; margin-top: 14px; padding-top: 14px; border-top: 1px solid var(--line); font-size: 12px; overflow-wrap: anywhere; }
.model-oauth-event { display: grid; justify-items: start; gap: 8px; }
.model-device-code { display: inline-block; margin-inline: 6px; font-size: 17px; letter-spacing: 2px; user-select: all; color: var(--accent-dark); }
.model-oauth-prompt { display: grid; gap: 10px; }
.model-oauth-prompt > button { justify-self: start; }
.model-oauth-card .document-error { font-size: 12px; margin-top: 12px; }
.model-removal-confirm { margin: 0 0 14px; border: 1px solid #ead6b4; }
.model-auth-resume { display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 12px; margin-bottom: 10px; }
.model-catalog-section, .model-editor-section { border-top: 1px solid var(--line); margin-top: 24px; padding-top: 20px; }
.model-catalog-controls { display: grid; grid-template-columns: minmax(100px, .85fr) minmax(140px, 1.25fr) auto; align-items: center; gap: 8px; }
.model-catalog-controls input, .model-catalog-controls select { font-size: 12px; }
.model-catalog-note { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 7px; }
.model-saved-list { display: grid; gap: 4px; max-height: 190px; overflow-y: auto; padding: 3px; margin: -3px; }
.model-saved-row { display: flex; align-items: center; gap: 12px; padding: 9px 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; text-align: left; color: var(--ink); font-size: 12px; }
.model-saved-row > span { flex: 1; overflow-wrap: anywhere; min-width: 0; }
.model-saved-row small, .model-saved-row b { flex-shrink: 0; font-size: 11px; font-weight: 400; }
.model-saved-row b { color: var(--accent); }
.model-saved-row.selected { border-color: var(--accent-line); background: var(--soft); }
.model-empty { padding: 20px 0; }
.model-editor { margin-top: 20px; }
.model-editor-title { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 16px; }
.model-editor-title h3 { font-size: 14px; overflow-wrap: anywhere; }
.model-compat-settings { border-top: 1px dashed var(--line); border-bottom: 1px dashed var(--line); margin-top: 18px; padding: 14px 0; }
.model-compat-settings > p { font-size: 12px; margin: 12px 0; }
.model-select-action { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-top: 18px; }
.model-welcome { display: flex; flex-direction: column; justify-content: center; gap: 12px; padding: 56px 40px; min-height: 320px; }
.model-welcome h3 { font-size: 19px; }
.model-welcome p { max-width: 340px; color: var(--muted); line-height: 1.9; font-size: 13px; }
.model-dialog-footer { max-height: 45%; overflow-y: auto; overscroll-behavior: contain; padding: 16px 28px 22px; border-top: 1px solid var(--line); background: var(--surface); }
.model-pending { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
.model-pending > div { display: flex; gap: 14px; align-items: baseline; min-width: 0; }
.model-pending small { font-size: 11px; color: var(--muted); flex-shrink: 0; }
.model-pending strong { font-size: 12px; font-weight: 500; overflow-wrap: anywhere; }
.model-footer-actions { display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-top: 14px; }
.model-footer-actions > p { margin-right: auto; max-width: 400px; font-size: 11px; }
.model-dialog-footer .document-error, .model-dialog-footer .model-feedback { margin-top: 8px; font-size: 12px; }
.model-inline-confirm { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 10px; padding: 12px; margin-top: 12px; background: var(--warning-soft); border-radius: 6px; }
.model-inline-confirm p { flex: 1; font-size: 12px; min-width: 160px; }
.model-load-error { margin: 0 28px 16px; color: var(--danger); font-size: 12px; }
.model-load-error button { margin-top: 8px; }
.model-loading { padding: 0 28px 12px; font-size: 13px; }
@media (max-width: 760px) {
  .model-workspace { grid-template-columns: 165px minmax(0, 1fr); }
  .model-provider-sidebar { padding: 16px 12px; }
  .model-provider-content { padding: 18px; }
  .model-catalog-controls { grid-template-columns: minmax(0, 1fr) auto; }
  .model-catalog-controls input { grid-column: 1 / -1; }
  .model-connection-grid { gap: 14px; }
  .model-footer-actions { flex-wrap: wrap; }
  .model-footer-actions > p { flex-basis: 100%; max-width: none; }
}
@media (max-width: 560px) {
  .provider-settings { height: calc(100dvh - 32px); max-height: calc(100dvh - 32px); }
  .model-dialog-header { padding: 18px 18px 12px; }
  .model-live { margin: 0 18px 12px; flex-wrap: wrap; gap: 8px; }
  .model-live > div { gap: 8px; }
  .model-workspace { display: block; }
  .model-provider-sidebar { border-right: 0; border-bottom: 1px solid var(--line); overflow: visible; }
  .model-provider-list { display: flex; overflow-x: auto; margin-bottom: 12px; padding: 3px; }
  .model-provider-row { min-width: 145px; max-width: 190px; flex-shrink: 0; }
  .model-add-provider { grid-template-columns: minmax(0, 1fr) auto; align-items: center; }
  .model-add-provider label { grid-column: 1 / -1; }
  .model-provider-content { overflow: visible; }
  .model-connection-grid { grid-template-columns: minmax(0, 1fr); }
  .model-select-action { align-items: flex-start; flex-direction: column; gap: 10px; }
  .model-saved-row { flex-wrap: wrap; gap: 6px 10px; }
  .model-dialog-footer { padding: 12px 18px 16px; }
  .model-pending { gap: 8px; }
  .model-pending > div { flex-direction: column; gap: 2px; }
  .model-footer-actions { gap: 8px; }
  .model-footer-actions > .button { flex: 1; padding-inline: 10px; }
  .model-welcome { min-height: 160px; padding: 28px 18px; }
}
@media (max-height: 480px) {
  .provider-settings { overflow-y: auto; }
  .model-workspace-scroll { flex: 0 0 auto; overflow: visible; }
  .model-dialog-footer { max-height: none; overflow: visible; }
}
</style>
