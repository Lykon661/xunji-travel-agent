<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import type { Candidate, RouteChatMessage } from './types'

const props = defineProps<{days:Candidate['days'];messages:RouteChatMessage[];disabled:boolean;selectedActivities:{id:string;name:string;date:string}[]}>()
const emit = defineEmits<{revise:[request:{dates:string[];content:string;selected_stop_ids:string[]}];unselect:[id:string]}>()
const content = ref(''), dates = ref<string[]>([]), history = ref<HTMLElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const allDates = computed(() => props.days.map(day => day.date))
const targetDates = computed(() => props.selectedActivities.length ? [...new Set(props.selectedActivities.map(activity => activity.date))] : dates.value)
watch(allDates, values => {
  dates.value = dates.value.length ? dates.value.filter(day => values.includes(day)) : [...values]
}, {immediate:true})
watch(() => props.messages.length, async () => {
  if (props.messages.at(-1)?.role === 'user') content.value = ''
  await nextTick()
  if (history.value) history.value.scrollTop = history.value.scrollHeight
}, {immediate:true})
function submit() {
  if (props.disabled || !content.value.trim() || !targetDates.value.length) return
  emit('revise',{dates:[...targetDates.value],content:content.value.trim(),selected_stop_ids:props.selectedActivities.map(activity => activity.id)})
}
async function editDates(values: string[]) {
  dates.value = values.filter(day => allDates.value.includes(day))
  await nextTick()
  input.value?.focus()
  input.value?.scrollIntoView({block:'center',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'})
}
defineExpose({editDates})
</script>

<template>
  <section class="route-chat" aria-label="行程调整对话">
    <header><h2>调整行程</h2><p>在左侧选择行程项，再告诉我想怎么调整。</p></header>
    <div v-if="selectedActivities.length" class="route-chat-selection"><strong>已选择 {{ selectedActivities.length }} 项</strong><ul><li v-for="activity in selectedActivities" :key="activity.id"><span>{{ activity.date.slice(5) }} · {{ activity.name }}</span><button type="button" class="text-button" :disabled="disabled" :aria-label="`取消选择：${activity.name}`" @click="emit('unselect',activity.id)">取消</button></li></ul></div>
    <div ref="history" class="route-chat-history" role="log" aria-live="polite" aria-relevant="additions text">
      <p v-if="!messages.length" class="subtle">例如：“第二天减少一个景点，留更多时间逛老街。”</p>
      <article v-for="(entry,index) in messages" :key="entry.time+index" :class="['route-chat-message',entry.role]">
        <strong>{{ entry.role === 'user' ? '你' : '行程助手' }}</strong><small v-if="entry.selected_names?.length">选择：{{ entry.selected_names.join('、') }}</small><small v-else-if="entry.dates?.length">{{ entry.dates.join('、') }}</small><p>{{ entry.content }}</p>
      </article>
    </div>
    <form @submit.prevent="submit"><fieldset :disabled="disabled">
      <p v-if="selectedActivities.length" class="subtle">本次调整选中的行程项，其余安排保留。</p>
      <details v-else class="route-chat-dates"><summary>调整范围：{{ dates.length === days.length ? '整趟行程' : `已选 ${dates.length} 天` }}</summary>
        <button type="button" class="text-button" @click="dates = [...allDates]">选择全部日期</button>
        <label v-for="(day,index) in days" :key="day.date"><input v-model="dates" type="checkbox" :value="day.date">第 {{ index+1 }} 天 · {{ day.date.slice(5) }} · {{ day.city }}</label>
      </details>
      <label>想怎么调整？<textarea ref="input" v-model="content" rows="5" maxlength="4000" :placeholder="selectedActivities.length ? '例如：把选中的景点换到下午，或减少游玩时长' : '先选择行程项，或直接说明整天要怎么调整'" required></textarea></label>
      <button class="primary full-width" :disabled="!targetDates.length || !content.trim()">发送，调整行程</button>
    </fieldset></form>
    <p class="subtle">调整后的路线会重新查证；需要你决定的取舍会回到需求追问。</p>
  </section>
</template>
