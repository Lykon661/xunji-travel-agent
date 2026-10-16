<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { api } from './api'

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const state = ref<{ configured: boolean } | null>(null)
const webState = ref<{configured: boolean; has_js_key: boolean; has_security_code: boolean} | null>(null)
const jsKey = ref(''), securityCode = ref(''), webSaving = ref(false)
const apiKey = ref(''), loading = ref(false), saving = ref(false), error = ref(''), message = ref('')
const panel = ref<HTMLElement | null>(null)
const controller = new AbortController()
let closed = false
let previousFocus: HTMLElement | null = null
let previousOverflow = ''

async function load() {
  loading.value = true; error.value = ''
  try {
    const [value,web] = await Promise.all([
      api<{ configured: boolean }>('/system/amap', { cache: 'no-store', signal: controller.signal }),
      api<{configured: boolean; has_js_key: boolean; has_security_code: boolean}>('/system/amap/web',{cache:'no-store',signal:controller.signal}),
    ])
    if (!closed) {state.value = value;webState.value = web}
  } catch (cause) { if (!closed) error.value = cause instanceof Error ? cause.message : '无法读取高德配置' }
  finally { loading.value = false }
}
async function save(clear = false) {
  if (saving.value || webSaving.value || props.active || closed) return
  saving.value = true; error.value = ''; message.value = ''
  try {
    const value = await api<{ configured: boolean }>('/system/amap', {
      method: clear ? 'DELETE' : 'PUT',
      ...(clear ? {} : { body: JSON.stringify({ api_key: apiKey.value.trim() }) }),
      signal: controller.signal,
    })
    if (closed) return
    state.value = value; apiKey.value = ''
    message.value = clear ? 'Key 已清除。' : '已保存并生效，实际权限将在规划查询时确认。'
    emit('saved')
  } catch (cause) { if (!closed) error.value = cause instanceof Error ? cause.message : '无法保存高德配置' }
  finally { saving.value = false }
}
async function saveWeb(clear = false) {
  if (saving.value || webSaving.value || props.active || closed) return
  webSaving.value = true;error.value = '';message.value = ''
  try {
    const value = await api<NonNullable<typeof webState.value>>('/system/amap/web',{
      method:clear ? 'DELETE' : 'PUT',signal:controller.signal,
      ...(clear ? {} : {body:JSON.stringify({js_key:jsKey.value.trim(),security_code:securityCode.value.trim()})}),
    })
    if (closed) return
    webState.value = value;jsKey.value = '';securityCode.value = ''
    message.value = clear ? '网页地图配置已清除。' : '网页地图配置已保存，安全密钥仅由后端代理使用。'
    emit('saved')
  } catch (cause) {if (!closed) error.value = cause instanceof Error ? cause.message : '无法保存网页地图配置'}
  finally {webSaving.value = false}
}
function close() { if (!saving.value && !webSaving.value) emit('close') }
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
  closed = true; controller.abort(); apiKey.value = '';jsKey.value = '';securityCode.value = ''
  document.removeEventListener('keydown', key); document.body.style.overflow = previousOverflow
  if (previousFocus?.isConnected) previousFocus.focus()
})
</script>

<template>
  <div class="modal-backdrop amap-overlay" @click.self="close">
    <section ref="panel" class="modal" role="dialog" aria-modal="true" aria-labelledby="amap-title" tabindex="-1">
      <header><h2 id="amap-title">高德配置</h2><button class="text-button" :disabled="saving || webSaving" @click="close">关闭</button></header>
      <h3>地点查询与手机高德导出</h3>
      <p class="subtle">{{ loading ? '正在读取配置…' : state ? (state.configured ? '已配置 Key，可以填写新 Key 替换。' : '尚未配置 Key。') : '暂时无法读取配置。' }}</p>
      <p class="subtle">在<a href="https://developer.amap.com/api/mcp-server/create-project-and-key" target="_blank" rel="noopener noreferrer">高德开放平台</a>创建应用，添加服务平台为“Web 服务”的 Key。</p>
      <p v-if="active" class="pending-note">正在处理旅行任务，结束后可以保存或清除 Key。</p>
      <form @submit.prevent="save()">
        <label>高德 Web 服务 Key<input v-model="apiKey" type="password" autocomplete="new-password" :disabled="loading || saving || webSaving || active || !state" :placeholder="state?.configured ? '填写新 Key，已保存的值不会回显' : '粘贴你的高德 Key'" maxlength="256" spellcheck="false" required></label>
        <p v-if="error" class="pending-note" role="alert">{{ error }}</p>
        <p v-if="message" class="subtle" role="status">{{ message }}</p>
        <div class="amap-actions">
          <button type="submit" class="primary" :disabled="loading || saving || webSaving || active || !state || !apiKey.trim()">{{ saving ? '正在保存…' : '保存 Key' }}</button>
          <button v-if="state?.configured" type="button" class="text-button" :disabled="loading || saving || webSaving || active" @click="save(true)">清除 Key</button>
          <button v-if="!state && !loading" type="button" class="text-button" @click="load">重新读取</button>
        </div>
      </form>
      <section class="amap-web-settings">
        <h3>电脑网页地图</h3><p class="subtle">{{ webState?.configured ? '已配置网页地图，填写新配置可替换。' : '尚未配置网页地图。' }}</p>
        <p class="subtle">在高德应用中另建服务平台为“Web端(JSAPI)”的 Key，填写与它配套的安全密钥。域名白名单应允许你使用的 localhost 或 127.0.0.1。</p>
        <form @submit.prevent="saveWeb()"><fieldset :disabled="loading || saving || webSaving || active || !webState">
          <label>Web端(JSAPI) Key<input v-model="jsKey" type="password" autocomplete="new-password" maxlength="256" spellcheck="false" required placeholder="网页地图的 Key，与 Web 服务 Key 分开"></label>
          <label>配套安全密钥（jscode）<input v-model="securityCode" type="password" autocomplete="new-password" maxlength="256" spellcheck="false" required placeholder="只保存在本机后端，不回显"></label>
          <div class="amap-actions"><button class="primary" :disabled="!jsKey.trim() || !securityCode.trim()">{{ webSaving ? '正在保存…' : '保存网页地图配置' }}</button><button v-if="webState?.configured" type="button" class="text-button" @click="saveWeb(true)">清除网页地图配置</button></div>
        </fieldset></form>
      </section>
      <p class="subtle amap-footnote">Key 仅保存在本机后端，保存后立即生效，无需重启。已配置状态不代表接口已验证可用。</p>
    </section>
  </div>
</template>

<style scoped>
.amap-overlay { z-index: 90; }
.amap-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 18px; }
.amap-footnote { margin: 22px 0 0; }
.amap-web-settings {margin-top:24px;padding-top:20px;border-top:1px solid var(--line)}
.amap-web-settings label {margin-bottom:16px}
</style>

