export class ApiError extends Error {
  constructor(message: string, public readonly status: number, public readonly code?: string) { super(message); this.name = 'ApiError' }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {
    ...init,
    headers: init.body instanceof FormData ? init.headers : {'Content-Type': 'application/json', ...init.headers},
  })
  let data
  try { data = await response.json() } catch {
    throw new ApiError('服务未返回可识别的响应，请检查后端连接',response.status,'response_format')
  }
  if (!response.ok) {
    const detail = Array.isArray(data.detail) ? data.detail.map((e: {msg: string}) => e.msg).join('；') : data.detail
    throw new ApiError(detail || '请求失败，请检查后端是否已启动', response.status, data.code)
  }
  return data as T
}

export function safeLink(value?: string | null): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? value : undefined
  } catch { return undefined }
}

export function safeMapLink(value?: string | null): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    if (url.username || url.password) return undefined
    if (url.protocol === 'amapuri:') return value
    return ['http:', 'https:'].includes(url.protocol) && ['amap.com', 'gaode.com'].some(host => url.hostname === host || url.hostname.endsWith('.' + host)) ? value : undefined
  } catch {return undefined}
}
