// One request per process, or with --worker one long-lived process multiplexing JSON lines by id.
// Secrets arrive on stdin, never in command-line arguments.

import readline from 'node:readline';

import { profileCredentials } from './credential-store.mjs';
const send = value => process.stdout.write(`${JSON.stringify(value)}\n`);
const allowedProviders = new Set([
  'openai', 'anthropic', 'google', 'deepseek', 'openrouter', 'groq', 'xai',
  'mistral', 'moonshotai', 'moonshotai-cn', 'zai', 'zai-coding-cn',
  'minimax', 'minimax-cn', 'together', 'nvidia', 'kimi-coding',
  'cerebras', 'fireworks', 'baseten', 'huggingface', 'vercel-ai-gateway',
  'opencode', 'opencode-go', 'ant-ling',
  'openai-codex', 'github-copilot', 'meta',
]);
const apiFactories = {
  'openai-completions': ['openai-completions', 'openAICompletionsApi'],
  'openai-responses': ['openai-responses', 'openAIResponsesApi'],
  'openai-codex-responses': ['openai-codex-responses', 'openAICodexResponsesApi'],
  'anthropic-messages': ['anthropic-messages', 'anthropicMessagesApi'],
  'google-generative-ai': ['google-generative-ai', 'googleGenerativeAIApi'],
  'mistral-conversations': ['mistral-conversations', 'mistralConversationsApi'],
};

// pi-ai input excludes cache reads/writes, while output includes reasoning.
// Its default all-zero object also occurs when a provider omits usage entirely.
function tokenUsage(value) {
  const integer = number => Number.isSafeInteger(number) && number >= 0;
  if (!value || !['input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens'].every(key => integer(value[key]))) return null;
  const input = value.input + value.cacheRead + value.cacheWrite;
  if (input + value.output === 0 || value.totalTokens !== input + value.output) return null;
  return { input_tokens: input, output_tokens: value.output, total_tokens: value.totalTokens,
    cache_read_tokens: value.cacheRead, cache_write_tokens: value.cacheWrite,
    reasoning_tokens: integer(value.reasoning) && value.reasoning <= value.output ? value.reasoning : null };
}

function context(messages, model) {
  return { messages: messages.map(message => {
    const timestamp = Date.now();
    if (message.role !== 'assistant') return { ...message, timestamp };
    return {
      role: 'assistant', content: [{ type: 'text', text: message.content }],
      api: model.api, provider: model.provider, model: model.id, stopReason: 'stop', timestamp,
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
  }) };
}

async function handle(request, send, signal) {
  let sdk, builtinModels;
  try {
    sdk = await import('@earendil-works/pi-ai');
    ({ builtinModels } = await import('@earendil-works/pi-ai/providers/all'));
  } catch {
    send({ type: 'error', message: '模型运行依赖未安装。请在项目目录执行 npm.cmd install --prefix model-runtime。' });
    return;
  }
  const config = request.config;
  const subscription = config?.credential_mode === 'oauth';
  const builtins = builtinModels(subscription ? {
    credentials: profileCredentials(config.oauth_credentials_path, config.provider),
    // Selected subscription auth must never silently use an ambient API key after logout.
    authContext: { env: async () => undefined, fileExists: async () => false },
  } : undefined);
  if (request.action === 'catalog') {
    send({ type: 'catalog', providers: builtins.getProviders()
      .filter(provider => allowedProviders.has(provider.id))
      .map(provider => ({ id: provider.id, name: provider.name || provider.id,
        supports_oauth: !!provider.auth.oauth, subscription: provider.auth.oauth?.isSubscription === true,
        oauth_name: provider.auth.oauth?.name || '', oauth_label: provider.auth.oauth?.loginLabel || '',
        base_url: builtins.getModels(provider.id)[0]?.baseUrl || '',
        models: builtins.getModels(provider.id).filter(model => apiFactories[model.api]).map(model => ({ id: model.id, name: model.name,
          api: model.api, base_url: model.baseUrl, context_window: model.contextWindow,
          max_tokens: model.maxTokens, reasoning: model.reasoning,
          supports_images: model.input.includes('image'), thinking_levels: sdk.getSupportedThinkingLevels(model),
          compat: model.compat || {}, cost: model.cost })) })).filter(provider => provider.models.length) });
    return;
  }
  const catalogModel = config.provider === 'custom' ? undefined : builtins.getModel(config.provider, config.model);
  if (subscription && (!catalogModel || !builtins.getProvider(config.provider)?.auth.oauth
    || catalogModel.api !== config.api || catalogModel.baseUrl.replace(/\/+$/, '') !== config.base_url.replace(/\/+$/, ''))) {
    send({ type: 'error', code: 'invalid_model_options', message: '订阅接入请使用该服务商内置目录中的模型、协议和官方接口地址。' });
    return;
  }
  // Protocol-specific metadata must not follow a model into a different API or gateway.
  const known = catalogModel?.api === config.api && catalogModel.baseUrl.replace(/\/+$/, '') === config.base_url.replace(/\/+$/, '')
    ? catalogModel : undefined;
  const api = config.api || known?.api;
  const factory = apiFactories[api];
  if (!factory) throw new Error('当前页面不支持该接口协议，请选择其他模型或自定义接口。');
  const hasImages = request.messages.some(message => Array.isArray(message.content)
    && message.content.some(block => block.type === 'image'));
  const supportsImages = config.supports_images === true;
  if (hasImages && !supportsImages) {
    send({ type: 'error', code: 'image_not_supported' });
    return;
  }
  const module = await import(`@earendil-works/pi-ai/api/${factory[0]}.lazy`);
  const model = {
    ...(known || {}), id: config.model, name: known?.name || config.model,
    api, provider: config.provider, baseUrl: config.base_url || known?.baseUrl,
    reasoning: config.reasoning, input: supportsImages ? ['text', 'image'] : ['text'],
    contextWindow: config.context_window, maxTokens: config.max_tokens,
    cost: known?.cost || { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    headers: config.headers || {},
    ...(api.startsWith('openai-') ? { compat: { ...known?.compat, ...config.compat } } : {}),
  };
  const thinking = config.thinking_level || 'default';
  if (config.reasoning && thinking !== 'default' && !sdk.getSupportedThinkingLevels(model).includes(thinking)) {
    send({ type: 'error', code: 'invalid_model_options', message: '该模型不支持所选思考强度，请选择服务商默认或其他强度。' });
    return;
  }
  // An explicit provider prevents inherited environment keys from overriding UI credentials.
  const models = subscription ? builtins : sdk.createModels();
  if (!subscription) models.setProvider(sdk.createProvider({ id: model.provider, name: model.provider,
    baseUrl: model.baseUrl, models: [model], api: module[factory[1]](),
    auth: { apiKey: { name: model.provider, resolve: async () => ({ auth: config.api_key
      ? { apiKey: config.api_key } : {} }) } },
  }));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeout_seconds * 1000);
  if (signal?.aborted) controller.abort();
  signal?.addEventListener('abort', () => controller.abort(), { once: true });
  let status;
  let includeUsage = true;
  let usageReported = false;
  let emittedText = false;
  const options = {
    ...(!subscription ? { apiKey: config.api_key || (api.startsWith('openai-') ? 'local-no-key' : undefined) } : {}),
    signal: controller.signal, timeoutMs: config.timeout_seconds * 1000, maxRetries: 0,
    maxTokens: Math.min(request.max_tokens || config.max_tokens, config.max_tokens),
    ...(config.temperature != null ? { temperature: config.temperature } : !config.reasoning ? { temperature: 0.15 } : {}),
    ...(config.reasoning && thinking !== 'default' && thinking !== 'off' ? { reasoning: thinking } : {}),
    onResponse: response => { status = response.status; },
    onPayload: payload => {
      if (request.json_mode && api === 'openai-completions') payload.response_format = { type: 'json_object' };
      if (api === 'openai-completions' && !includeUsage) delete payload.stream_options;
      if (new URL(model.baseUrl).hostname === 'api.deepseek.com' && !config.reasoning)
        payload.thinking = { type: 'disabled' };
      return payload;
    },
  };
  const input = context(request.messages, model);
  const imageErrorCode = message => hasImages
    && /image|vision|multimodal|图片|图像/i.test(message || '')
    && /unsupported|not support|does not support|text.only|only text|不支持/i.test(message || '')
      ? 'image_not_supported' : undefined;
  if (request.json_mode) input.messages.unshift({ role: 'system',
    content: '请仅输出一个完整的 JSON 对象，不要使用 Markdown 代码块。', timestamp: Date.now() });
  try {
    let response;
    for (;;) {
      usageReported = false;
      if (request.action === 'stream') {
        const stream = thinking === 'default' ? models.stream(model, input, options) : models.streamSimple(model, input, options);
        for await (const event of stream) {
          if (event.type === 'text_delta') {
            if (event.delta) emittedText = true;
            send({ type: 'delta', text: event.delta });
          }
        }
        response = await stream.result();
      } else {
        response = thinking === 'default' ? await models.complete(model, input, options) : await models.completeSimple(model, input, options);
      }
      send({ type: 'usage', usage: tokenUsage(response.usage) });
      usageReported = true;
      if (api === 'openai-completions' && response.stopReason === 'error' && !emittedText
        && /unsupported|not support(?:ed)?|not allowed|unknown|unrecognized|unexpected|invalid|extra (?:fields|inputs)|不支持/i.test(response.errorMessage || '')) {
        // Narrow parameter fallbacks: some compatible gateways reject usage or JSON options.
        // Each actual attempt reports its own usage, including missing usage on rejections.
        if (includeUsage && /stream_options|include_usage/i.test(response.errorMessage || '')) {
          includeUsage = false;
          continue;
        }
        if (request.json_mode && /response_format|json_object/i.test(response.errorMessage || '')) {
          request.json_mode = false;
          continue;
        }
      }
      break;
    }
    if (response.stopReason === 'error' || response.stopReason === 'aborted') {
      send({ type: 'error', code: imageErrorCode(response.errorMessage), status, timeout: controller.signal.aborted,
        message: subscription ? '订阅模型请求失败，请确认账号权限；认证失败时请重新登录。' : response.errorMessage || '模型请求失败。' });
      return;
    }
    const text = response.content.filter(block => block.type === 'text').map(block => block.text).join('');
    send({ type: 'done', text, stop_reason: response.stopReason });
  } catch (error) {
    if (!usageReported) send({ type: 'usage', usage: null });
    send({ type: 'error', code: imageErrorCode(error.message), status: error.status || status, timeout: controller.signal.aborted,
      message: subscription ? '订阅模型调用失败，请确认账号权限并尝试重新登录。' : error.message || '模型请求失败。' });
  } finally { clearTimeout(timer); }
}

function worker() {
  // The SDK modules load once; each request keeps its own credentials, abort signal and events.
  const controllers = new Map();
  const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on('line', line => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (typeof message?.id !== 'string') return;
    if (message.cancel) { controllers.get(message.id)?.abort(); return; }
    const controller = new AbortController();
    controllers.set(message.id, controller);
    const reply = event => send({ ...event, id: message.id });
    handle(message.request, reply, controller.signal)
      .catch(error => reply({ type: 'error', message: error.message || '模型接入失败。' }))
      .finally(() => { controllers.delete(message.id); reply({ type: 'end' }); });
  });
  // The Python service owns this process; a closed stdin or stdout means it has gone away.
  lines.on('close', () => process.exit(0));
  process.stdout.on('error', () => process.exit(0));
}

async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  await handle(JSON.parse(Buffer.concat(chunks).toString('utf8')), send);
}

if (process.argv.includes('--worker')) worker();
else main().catch(error => send({ type: 'error', message: error.message || '模型接入失败。' }));
