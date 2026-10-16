<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import type { Capabilities } from './types'

export type SettingsKind = 'model' | 'crawler' | 'flyai' | 'amap'
defineProps<{capabilities: Partial<Capabilities>}>()
const emit = defineEmits<{open: [kind: SettingsKind]}>()
const open = ref(false)
const root = ref<HTMLElement | null>(null)

function outside(event: Event) {
  if (root.value && !root.value.contains(event.target as Node)) open.value = false
}
function escape(event: Event) {
  if ((event as KeyboardEvent).key === 'Escape') open.value = false
}
watch(open, value => {
  if (value) {
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
  } else {
    document.removeEventListener('pointerdown', outside)
    document.removeEventListener('keydown', escape)
  }
})
onUnmounted(() => {
  document.removeEventListener('pointerdown', outside)
  document.removeEventListener('keydown', escape)
})
function choose(kind: SettingsKind) {
  open.value = false
  emit('open', kind)
}
</script>

<template>
  <div ref="root" class="settings-menu">
    <button class="settings-trigger" :aria-expanded="open" aria-haspopup="true" @click="open = !open">
      <span class="settings-dots" aria-hidden="true">
        <i :class="{on: capabilities.model_configured}"></i>
        <i :class="{on: capabilities.crawler_enabled}"></i>
        <i :class="{on: capabilities.flyai_ready || capabilities.flyai_configured}"></i>
        <i :class="{on: capabilities.amap_configured}"></i>
      </span>
      接入与设置
      <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6 8l4 4 4-4"/></svg>
    </button>
    <div v-if="open" class="settings-popover" role="menu" aria-label="接入与设置">
      <p class="settings-heading">当前接入状态</p>
      <button class="settings-item" role="menuitem" @click="choose('model')">
        <i class="dot" :class="{on: capabilities.model_configured}" aria-hidden="true"></i>
        <span><strong>模型配置</strong>
          <small>{{ capabilities.model_configured ? '已配置' : '未配置' }}<template v-if="capabilities.model_name"> · {{ capabilities.model_name }}</template><template v-else-if="capabilities.model_configured">，待选择使用模型</template></small>
        </span>
      </button>
      <button class="settings-item" role="menuitem" @click="choose('crawler')">
        <i class="dot" :class="{on: capabilities.crawler_enabled}" aria-hidden="true"></i>
        <span><strong>小红书采集器</strong><small>{{ capabilities.crawler_enabled ? '已启动' : '未启动' }}</small></span>
      </button>
      <button class="settings-item" role="menuitem" @click="choose('flyai')">
        <i class="dot" :class="{on: capabilities.flyai_ready || capabilities.flyai_configured}" aria-hidden="true"></i>
        <span><strong>飞猪配置</strong><small>{{ capabilities.flyai_message || '正在读取接入状态' }}</small></span>
      </button>
      <button class="settings-item" role="menuitem" @click="choose('amap')">
        <i class="dot" :class="{on: capabilities.amap_configured}" aria-hidden="true"></i>
        <span><strong>高德配置</strong><small>{{ capabilities.amap_configured ? '已配置，实际权限待查询' : '未配置，需申请并填写高德 Key' }}</small></span>
      </button>
      <p class="settings-foot">配置状态不代表接口已验证可用。</p>
    </div>
  </div>
</template>
