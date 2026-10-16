<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'

defineProps<{ active: boolean }>()
const emit = defineEmits<{ close: []; saved: [] }>()
interface Configuration { configured: boolean; enabled: boolean; ready: boolean; message: string }
const state = ref<Configuration | null>(null)
const apiKey = ref(''), loading = ref(false), saving = ref(false), error = ref(''), message = ref('')
const panel = ref<HTMLElement | null>(null)
const controller = new AbortController()
let closed = false
let previousFocus: HTMLElement | null = null
let previousOverflow = ''

async function load() {
  loading.value = true; error.value = ''
  try {
    const value = await api<Configuration>('/system/flyai', { cache: 'no-store', signal: controller.signal })
    if (!closed) state.value = value
  } catch (cause) { if (!closed) error.value = cause instanceof Error ? cause.message : '无法读取飞猪配置' }
  finally { loading.value = false }
}
async function save(clear = false) {
  saving.value = true; error.value = ''; message.value = ''
  try {
    const value = await api<Configuration>('/system/flyai', {
      method: clear ? 'DELETE' : 'PUT',
      ...(clear ? {} : { body: JSON.stringify({ api_key: apiKey.value.trim() }) }),
      signal: controller.signal,
    })
    if (closed) return
    state.value = value; apiKey.value = ''
    message.value = clear ? 'Key 已清除，飞猪查询已停用。' : '已保存并生效，后续查询将使用此 Key。'
    emit('saved')
  } catch (cause) { if (!closed) error.value = cause instanceof Error ? cause.message : '无法保存飞猪配置' }
  finally { saving.value = false }
}
function close() { if (!saving.value) emit('close') }
function key(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
  if (event.key !== 'Tab') return
  const targets = Array.from(panel.value?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href]') || [])
  const first = targets[0], last = targets[targets.length - 1]
  if (!first || !last) { event.preventDefault(); return }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && (document.activeElement === last || document.activeElement === panel.value)) { event.preventDefault(); first.focus() }
}
onMounted(async () => {
  previousFocus = document.activeElement as HTMLElement | null
  previousOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'
  document.addEventListener('keydown', key)
  await nextTick(); panel.value?.focus()
  void load()
})
onUnmounted(() => {
  closed = true; controller.abort(); apiKey.value = ''
  document.removeEventListener('keydown', key); document.body.style.overflow = previousOverflow
  if (previousFocus?.isConnected) previousFocus.focus()
})
</script>

<template>
  <div class="modal-backdrop flyai-overlay" @click.self="close">
    <section ref="panel" class="modal" role="dialog" aria-modal="true" aria-labelledby="flyai-title" tabindex="-1">
      <header><h2 id="flyai-title">飞猪配置</h2><button class="text-button" :disabled="saving" @click="close">关闭</button></header>
      <p class="subtle">{{ loading ? '正在读取配置…' : state ? (state.configured ? '已配置 API Key，可以填写新 Key 替换。' : '尚未配置 API Key。') : '暂时无法读取配置。' }}</p>
      <p v-if="state" class="subtle">{{ state.message }}</p>
      <p v-if="active" class="pending-note">正在处理旅行任务，结束后可以保存或清除 Key。</p>
      <form @submit.prevent="save()">
        <label>飞猪 API Key<input v-model="apiKey" type="password" autocomplete="new-password" :disabled="loading || saving || active || !state" :placeholder="state?.configured ? '填写新 Key，已保存的值不会回显' : '粘贴你申请到的飞猪 API Key'" maxlength="4096" spellcheck="false" required></label>
        <p v-if="error" class="pending-note" role="alert">{{ error }}</p>
        <p v-if="message" class="subtle" role="status">{{ message }}</p>
        <div class="flyai-actions">
          <button type="submit" class="primary" :disabled="loading || saving || active || !state || !apiKey.trim()">{{ saving ? '正在保存…' : '保存 Key' }}</button>
          <button v-if="state?.configured" type="button" class="text-button" :disabled="loading || saving || active" @click="save(true)">清除 Key</button>
          <button v-if="!state && !loading" type="button" class="text-button" @click="load">重新读取</button>
        </div>
      </form>
      <p class="subtle flyai-footnote">Key 仅保存在本机后端，保存后立即生效，无需重启。实际接口权限和库存将在规划查询时确认。</p>
      <p class="subtle">已有行程不会自动重查。保存后可在行程中发送“重新查询住宿和交通”，获取新的查询结果。</p>
    </section>
  </div>
</template>

<style scoped>
.flyai-overlay { z-index: 90; }
.flyai-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 18px; }
.flyai-footnote { margin: 22px 0 0; }
</style>
