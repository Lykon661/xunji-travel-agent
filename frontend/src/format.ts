import { safeLink } from './api'

export function imageUrl(image: string) {
  return image.startsWith('local:') ? '/api/uploads/' + image.slice(6) : safeLink(image)
}

export function published(value: string | number | null) {
  if (!value) return '发布日期未知'
  const numeric = Number(value)
  const date = Number.isFinite(numeric) ? new Date(numeric > 1e12 ? numeric : numeric * 1000) : new Date(value)
  return Number.isNaN(date.getTime()) ? '发布日期未知' : date.toLocaleDateString()
}

export function money(value: number | null | undefined) {
  return value == null ? '待确认' : '¥' + value.toLocaleString('zh-CN', {maximumFractionDigits: 2})
}
