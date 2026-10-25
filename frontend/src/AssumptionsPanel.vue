<script setup lang="ts">
import { computed, ref } from 'vue'
import { changeAssumption } from './api'
import type { AssumptionAlternative, SystemAssumption } from './types'

const props = defineProps<{
  tripId: string
  baseVersion: number
  draftRevision?: number
  assumptions?: SystemAssumption[]
  stopNames?: Record<string, string>
  disabled?: boolean
}>()

const emit = defineEmits<{
  run: [runId: string, tripId: string]
  busy: [active: boolean, label?: string]
  error: [message: string]
}>()

const busyKey = ref('')
const expandedMobile = ref(false)
const list = computed(() => props.assumptions || [])

function getAlternatives(item: SystemAssumption): AssumptionAlternative[] {
  return item.alternatives || item.options || []
}

function getActionText(action: SystemAssumption['current_action']): string {
  return typeof action === 'string' ? action.trim() : ''
}

function getAlternativeDescription(alt: AssumptionAlternative): string {
  const desc = alt.description?.trim() || ''
  if (!desc || desc === (alt.label?.trim() || '')) return ''
  return desc
}

function getStopLabels(item: SystemAssumption): string[] {
  if (item.stop_names?.length) return item.stop_names
  if (!item.stop_ids?.length) return []
  return item.stop_ids.map(id => {
    if (props.stopNames?.[id]) return props.stopNames[id]
    if (id.startsWith('wish:')) return id.slice(5)
    return id
  })
}

async function applyAlternative(item: SystemAssumption, alternativeId: string) {
  if (props.disabled || busyKey.value) return
  const key = `${item.id}:${alternativeId}`
  busyKey.value = key
  emit('busy', true, '正在按你的选择调整系统决定')
  try {
    const result = await changeAssumption(props.tripId, item.id, {
      alternative_id: alternativeId,
      assumption_revision: item.assumption_revision ?? item.revision ?? 0,
      draft_revision: props.draftRevision ?? 0,
      base_version: props.baseVersion,
    })
    emit('run', result.run_id, props.tripId)
  } catch (e) {
    emit('error', e instanceof Error ? e.message : '修改系统决定失败')
  } finally {
    busyKey.value = ''
    emit('busy', false)
  }
}
</script>

<template>
  <section v-if="list.length" class="assumptions-panel" aria-label="系统替你做的决定">
    <header class="assumptions-head">
      <div>
        <h4>系统替你做的决定</h4>
        <p class="subtle">以下非核心事项由系统按常规经验默认处理；如需换一种做法，点击下方按钮即可局部重算。</p>
      </div>
      <span class="badge">{{ list.length }} 项</span>
    </header>

    <div class="assumptions-list">
      <article
        v-for="(item, index) in list"
        :key="item.id"
        class="assumption-card"
        :class="{ 'mobile-collapsed': index >= 2 && !expandedMobile }"
      >
        <div class="assumption-title-row">
          <strong>{{ item.title }}</strong>
          <div v-if="item.dates?.length || getStopLabels(item).length" class="decision-scopes">
            <span v-for="d in item.dates || []" :key="`date-${d}`" class="scope-tag">{{ d }}</span>
            <span v-for="stop in getStopLabels(item)" :key="`stop-${stop}`" class="scope-tag stop">{{ stop }}</span>
          </div>
        </div>
        <p v-if="getActionText(item.current_action)" class="assumption-action">当前做法：{{ getActionText(item.current_action) }}</p>
        <p v-if="item.reason" class="assumption-reason subtle">{{ item.reason }}</p>

        <div v-if="getAlternatives(item).length" class="assumption-alternatives">
          <button
            v-for="alt in getAlternatives(item)"
            :key="alt.id"
            type="button"
            class="secondary small assumption-alt-btn"
            :disabled="disabled || !!busyKey"
            :aria-busy="busyKey === `${item.id}:${alt.id}`"
            @click="applyAlternative(item, alt.id)"
          >
            <span v-if="busyKey === `${item.id}:${alt.id}`" class="loading-spinner" aria-hidden="true"></span>
            <span>{{ alt.label }}</span>
            <small v-if="getAlternativeDescription(alt)" class="assumption-alt-desc">· {{ getAlternativeDescription(alt) }}</small>
          </button>
        </div>
      </article>
    </div>

    <button
      v-if="list.length > 2"
      type="button"
      class="text-button assumptions-mobile-toggle"
      :aria-expanded="expandedMobile"
      @click="expandedMobile = !expandedMobile"
    >
      {{ expandedMobile ? '收起其余项' : `展开其余 ${list.length - 2} 项系统决定` }}
    </button>
  </section>
</template>
