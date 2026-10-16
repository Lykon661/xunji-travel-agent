<script setup lang="ts">
import { computed } from 'vue'
import type { WorkflowTab } from './workflow'

const props = defineProps<{
  steps: readonly {key: WorkflowTab; label: string}[]
  active: WorkflowTab
  disabled: boolean
  guideCount: number
}>()
const emit = defineEmits<{navigate: [key: WorkflowTab]}>()
const flow = computed(() => props.steps.filter(step => step.key !== 'resources'))
const tool = computed(() => props.steps.find(step => step.key === 'resources'))
const activeIndex = computed(() => flow.value.findIndex(step => step.key === props.active))
</script>

<template>
  <nav class="stepper" aria-label="旅行规划步骤">
    <ol>
      <li v-for="(step, index) in flow" :key="step.key">
        <button
          :class="{active: step.key === active, passed: activeIndex > index}"
          :aria-current="step.key === active ? 'step' : undefined"
          :disabled="disabled"
          @click="emit('navigate', step.key)"
        >
          <span class="step-index" aria-hidden="true">
            <svg v-if="activeIndex > index" viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7"/></svg>
            <template v-else>{{ index + 1 }}</template>
          </span>
          <span class="step-label">{{ step.label }}</span>
          <span v-if="step.key === 'guides'" class="step-count">{{ guideCount }}</span>
        </button>
      </li>
    </ol>
    <button
      v-if="tool"
      class="stepper-tool"
      :class="{active: tool.key === active}"
      :aria-current="tool.key === active ? 'page' : undefined"
      :disabled="disabled"
      @click="emit('navigate', tool.key)"
    >
      <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 15V8l7-4 7 4v7"/><path d="M3 15h14M8 15v-4h4v4"/></svg>
      {{ tool.label }}
    </button>
  </nav>
</template>
