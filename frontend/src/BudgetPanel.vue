<script setup lang="ts">
import { computed } from 'vue'
import { money } from './format'
import type { Itinerary } from './types'

const props = defineProps<{itinerary: Itinerary; history: boolean; disabled: boolean}>()
const emit = defineEmits<{refresh: []}>()
const budget = computed(() => props.itinerary.budget)
const ratio = computed(() => budget.value.limit ? Math.min(1, budget.value.known_plus_estimates / budget.value.limit) : 0)
const statusText = computed(() => budget.value.status === 'over' ? '已知费用与各项估算已超过预算，需要调整。'
  : budget.value.status === 'incomplete' ? '仍有费用未知，当前不能确认是否满足总预算。' : '当前已知费用满足预算。')
</script>

<template>
  <section class="budget-panel side-card">
    <div class="side-card-head">
      <h2>费用记录</h2>
      <button v-if="!history" class="secondary small" :disabled="disabled" @click="emit('refresh')">更新报价与路线</button>
    </div>
    <span class="subtle">已知费用小计</span>
    <strong class="budget-total">{{ money(budget.known_subtotal) }}</strong>
    <div v-if="budget.limit" class="budget-meter" :class="budget.status" role="img" :aria-label="`已知费用与估算占预算 ${Math.round(ratio * 100)}%`">
      <span :style="{width: ratio * 100 + '%'}"></span>
    </div>
    <dl>
      <div v-for="line in budget.lines" :key="line.label">
        <dt>{{ line.label }}</dt>
        <dd>{{ line.amount != null ? money(line.amount) : line.estimated_amount != null ? '约 ' + money(line.estimated_amount) + '（估算）' : '待查询' }}</dd>
      </div>
      <div><dt>餐饮估算</dt><dd>{{ money(budget.estimated_meals) }}</dd></div>
      <div v-if="budget.estimated_local_transport"><dt>市内交通参考估算</dt><dd>{{ money(budget.estimated_local_transport) }}</dd></div>
      <div class="budget-sum"><dt>已知费用＋各项估算</dt><dd>{{ money(budget.known_plus_estimates) }}</dd></div>
      <div class="budget-limit"><dt>整趟预算</dt><dd>{{ money(budget.limit) }}</dd></div>
    </dl>
    <p class="budget-status" :class="budget.status">{{ statusText }}</p>
    <details v-if="budget.unknown.length">
      <summary>{{ budget.unknown.length }} 项费用待补全或核对</summary>
      <ul><li v-for="item in budget.unknown" :key="item">{{ item }}</li></ul>
    </details>
  </section>
</template>
