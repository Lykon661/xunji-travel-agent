<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'
import type { CrawlerState } from './types'

const emit = defineEmits<{ close: []; changed: [] }>()
const state = ref<CrawlerState | null>(null), loading = ref(false), error = ref('')
const loadError = ref('')
const panel = ref<HTMLElement | null>(null)
const disabled = computed(() => loading.value || !!state.value?.busy || !!state.value?.collecting)
const status = computed(() => !state.value ? '读取中' : state.value.blocked ? '账号保护：已锁定' : state.value.stage === 'installing' ? '安装中' : state.value.stage === 'logging_in' ? '等待扫码登录' : state.value.stage === 'failed' ? '操作失败' : !state.value.installed ? (state.value.present ? '安装未完成' : '尚未安装') : state.value.enabled ? '已启动' : '已安装，未启动')
let timer: ReturnType<typeof setTimeout> | undefined
let closed = false
let revision = 0
let previousFocus: HTMLElement | null = null
let previousOverflow = ''
const controller = new AbortController()

async function load() {
  if (closed) return
  if (loading.value) {timer = setTimeout(() => {void load()},1500);return}
  const epoch = revision
  try {
    const previous = state.value
    const value = await api<CrawlerState>('/system/crawler',{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(10000)])})
    if (closed || epoch !== revision) return
    state.value = value
    if (!previous || previous.enabled !== state.value.enabled || previous.busy !== state.value.busy) emit('changed')
    loadError.value = ''
  } catch (cause) { if (!closed && epoch === revision) loadError.value = cause instanceof Error ? cause.message : '无法读取采集器状态' }
  finally { if (!closed) timer = setTimeout(() => {void load()},1500) }
}
async function command(action: 'install' | 'login' | 'start' | 'stop' | 'unblock') {
  revision++
  loading.value = true; error.value = ''
  try {
    state.value = await api<CrawlerState>(`/system/crawler/${action}`,{method:'POST',signal:controller.signal})
    emit('changed')
  } catch (cause) { if (!closed) error.value = cause instanceof Error ? cause.message : '采集器操作失败' }
  finally { loading.value = false }
}
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
  if (event.key !== 'Tab') return
  const targets = Array.from(panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]') || [])
  const first = targets[0], last = targets[targets.length-1]
  if (!first || !last) { event.preventDefault(); return }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {event.preventDefault();last.focus()}
  else if (!event.shiftKey && document.activeElement === last) {event.preventDefault();first.focus()}
}
onMounted(async () => {
  previousFocus = document.activeElement as HTMLElement | null
  previousOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'
  document.addEventListener('keydown',key)
  await nextTick(); panel.value?.focus()
  void load()
})
onUnmounted(() => {
  closed = true; clearTimeout(timer); controller.abort()
  document.removeEventListener('keydown',key); document.body.style.overflow = previousOverflow
  if (previousFocus?.isConnected) previousFocus.focus()
})
</script>

<template>
  <div class="modal-backdrop crawler-overlay" @click.self="emit('close')">
    <section ref="panel" class="modal crawler-modal" role="dialog" aria-modal="true" aria-labelledby="crawler-title" tabindex="-1">
      <header><h2 id="crawler-title">小红书采集器</h2><button class="text-button" @click="emit('close')">关闭</button></header>
      <p class="crawler-status" role="status"><span class="status-dot"></span>{{ status }}</p>
      <p>{{ state?.message || '正在读取本机采集器状态…' }}</p>
      <p v-if="error || loadError" class="pending-note" role="alert">{{ error || loadError }}</p>
      <div class="crawler-actions">
        <button class="secondary" :disabled="disabled || !state" @click="command('install')">{{ state?.present ? '精简并修复安装' : '安装轻量采集器' }}</button>
        <button class="primary" :disabled="disabled || !state?.installed || state?.enabled || state?.blocked" @click="command('start')">启动采集器</button>
        <button class="secondary" :disabled="disabled || !state?.installed || state?.blocked || !!state?.cooldown_seconds" @click="command('login')">打开登录窗口</button>
        <button class="text-button" :disabled="loading || (!state?.enabled && !state?.busy && !state?.collecting)" @click="command('stop')">{{ state?.busy || state?.collecting ? '取消操作并停用' : '停止采集器' }}</button>
        <button v-if="state?.blocked" class="secondary" :disabled="disabled" @click="command('unblock')">账号已恢复，解除保护</button>
      </div>
      <p v-if="state?.blocked" class="pending-note">请先在官方客户端处理账号异常。确认恢复后再解除保护；解除后仍保持停用，低频采集也不能保证账号安全。</p>
      <p v-if="state?.cooldown_seconds" class="subtle">本轮冷却还剩 {{ state.cooldown_seconds }} 秒；冷却结束不会自动恢复采集。</p>
      <ol class="crawler-steps"><li>首次使用点击“安装轻量采集器”，安装完成且账号正常时再打开登录窗口。</li><li>登录完成后保持停用，由你手动启动；账号异常时不要反复扫码重试。</li><li>在“参考攻略”中手动搜索。每轮一个关键词、最多三篇，接口请求间隔至少六秒，两轮开始至少间隔一分钟。</li></ol>
      <p class="subtle">规划过程只使用已保存攻略。验证码或访问限制会立即终止采集并锁定，保护状态和冷却时间保存在本机，重启后仍有效。</p>
      <p class="subtle">安装需要 Git 和网络，使用独立 Python 3.11。轻量环境仅用于小红书搜索、详情和扫码登录，保存为 JSONL；不安装词云、图像分析、数据库和其他平台依赖。已有环境点击“精简并修复安装”可移除可选大包，登录资料保留。缺少 Python 3.11 时会通过 Python Install Manager 下载。</p>
    </section>
  </div>
</template>

<style scoped>
.crawler-overlay { z-index: 90; }
.crawler-modal { max-width: 620px; }
.crawler-status { color: var(--primary-text); font-weight: 600; background: var(--primary-soft); padding: 12px 14px; border-radius: 7px; }
.crawler-actions { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 22px 0; }
.crawler-steps { padding-left: 22px; line-height: 1.9; font-size: 13px; color: var(--text-2); }
.crawler-steps li { margin-bottom: 9px; }
</style>
