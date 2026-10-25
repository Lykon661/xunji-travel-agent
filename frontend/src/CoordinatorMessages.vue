<script setup lang="ts">
import { computed } from 'vue'
import type { CoordinatorMessage } from './types'

const props = defineProps<{
  messages?: CoordinatorMessage[]
  disabled?: boolean
  historical?: boolean
}>()

const emit = defineEmits<{
  reopen: [index: number]
}>()

const visibleMessages = computed(() =>
  (props.messages || []).filter((item): item is CoordinatorMessage & { decision_index: number } =>
    item.decision_index !== undefined && item.decision_index !== null
  )
)
</script>

<template>
  <div v-if="visibleMessages.length" class="coordinator-messages" aria-label="主理人消息">
    <article
      v-for="(item, idx) in visibleMessages"
      :key="item.id || `${item.time || ''}-${idx}`"
      class="coordinator-message-item"
    >
      <div class="coordinator-message-meta">
        <strong>主理人</strong>
        <small v-if="item.time" class="subtle">{{ item.time }}</small>
        <button
          v-if="!historical"
          type="button"
          class="text-button coordinator-reopen-btn"
          :disabled="disabled"
          @click="emit('reopen', item.decision_index)"
        >改答</button>
      </div>
      <p>{{ item.text }}</p>
    </article>
  </div>
</template>
