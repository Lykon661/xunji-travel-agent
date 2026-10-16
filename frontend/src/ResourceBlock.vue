<script setup lang="ts">
import { safeLink } from './api'
import type { ResourceGroup, Resource } from './types'
defineProps<{entry: ResourceGroup; locked: boolean; disabled: boolean}>()
defineEmits<{lock: [id: string]; select: [entryId: string, optionId: string]; refresh: [id: string]}>()
const bases: Record<string,string> = {person_segment:'每人每段',room_night:'每间每晚',segment_total:'整段报价，适用人数/房间数待核对',starting:'起价',unknown:'费用口径待确认'}
function price(option: Resource) {return option.price == null ? '报价待确认' : `${option.currency === 'CNY' ? '¥' : option.currency + ' '}${option.price.toLocaleString()}`}
</script>

<template>
  <section class="resource-block">
    <div class="resource-heading"><h3>{{ entry.label }}</h3><button class="text-button" :disabled="disabled" @click="$emit('lock', entry.id)">{{ locked ? '已锁定 · 解锁' : '锁定安排' }}</button></div>
    <p class="subtle">{{ entry.context.date || `${entry.context.check_in} 至 ${entry.context.check_out}` }}</p>
    <template v-if="entry.selected">
      <img v-if="safeLink(entry.selected.image_url)" class="resource-image" :src="safeLink(entry.selected.image_url)" :alt="entry.selected.name" loading="lazy" referrerpolicy="no-referrer">
      <div class="resource-selected"><strong>{{ entry.selected.name }}</strong><b>{{ price(entry.selected) }}</b></div>
      <p v-if="entry.selected.kind === 'hotel'" class="subtle">{{ entry.selected_by_user ? '你已明确采用的住宿' : '住宿推荐，尚未采用；不参与行程和费用合计' }}</p>
      <button v-if="entry.selected.kind === 'hotel' && !entry.selected_by_user" class="text-button" :disabled="disabled || locked || !!entry.selected.constraint_issues?.length" @click="$emit('select',entry.id,entry.selected.id)">采用这家住宿并重算</button>
      <p v-if="entry.selected.departure || entry.selected.arrival">{{ entry.selected.departure || '出发待确认' }} — {{ entry.selected.arrival || '到达待确认' }}</p>
      <p v-if="entry.selected.departure_place || entry.selected.arrival_place" class="subtle">{{ entry.selected.departure_place }} {{ entry.selected.departure_terminal }} → {{ entry.selected.arrival_place }} {{ entry.selected.arrival_terminal }}<span v-if="entry.selected.seat_class"> · {{ entry.selected.seat_class }}</span></p>
      <p v-if="entry.selected.door_minutes != null">含接驳与进出站缓冲，约 {{ entry.selected.door_minutes }} 分钟</p>
      <p v-if="entry.selected.commute_minutes != null">{{ entry.selected_by_user ? '整个停留段住宿接驳' : '若采用此住宿，接驳参考' }}约 {{ entry.selected.commute_minutes }} 分钟（不含景点之间移动）</p>
      <p v-if="entry.selected.address" class="subtle">{{ entry.selected.address }}<span v-if="entry.selected.rating"> · 评分 {{ entry.selected.rating }}</span></p>
      <p v-if="entry.selected.kind === 'hotel'" class="subtle">{{ entry.selected.bed_type || '床型筛选结果，具体房型待核对' }} · {{ entry.selected.cancellation || '退改与含早待确认' }}</p>
      <p v-for="note in entry.selected.comparison_unknown || []" :key="note" class="pending-note">{{ note }}</p>
      <p class="subtle">{{ entry.selected.reference_unit === 'room_night' ? '酒店展示价，按每间每晚作参考估算；各晚实际价格待确认' : bases[entry.selected.price_basis] || '费用口径待确认' }} · 查询于 {{ new Date(entry.selected.queried_at).toLocaleString() }}</p>
      <p>{{ entry.reason }}</p>
      <a v-if="safeLink(entry.selected.booking_url)" :href="safeLink(entry.selected.booking_url)" target="_blank" rel="noopener noreferrer" class="inline-link">查看预订页面</a>
      <p v-if="entry.selected.platform_hint" class="subtle">{{ entry.selected.platform_hint }}</p>
    </template>
    <p v-else class="pending-note">{{ entry.reason || '尚未获得资源候选，请查看查询状态后重试。' }}</p>
    <p v-for="error in entry.errors" :key="error" class="pending-note">{{ error }}</p>
    <button class="text-button" :disabled="disabled || locked" @click="$emit('refresh',entry.id)">重新查询本项报价</button>
    <details v-if="entry.options.length"><summary>比较与切换 {{ entry.options.length }} 个查询结果</summary><ul class="alternatives"><li v-for="option in entry.options" :key="option.id"><div><strong>{{ option.name }}</strong><small>{{ bases[option.price_basis] }}<span v-if="option.seat_class"> · {{ option.seat_class }}</span></small><small v-if="option.departure || option.arrival">{{ option.departure }} → {{ option.arrival }}</small><small v-if="option.door_minutes != null">含接驳缓冲 {{ option.door_minutes }} 分钟</small><small v-if="option.commute_minutes != null">住宿接驳 {{ option.commute_minutes }} 分钟</small><small v-for="issue in option.constraint_issues || []" :key="issue" class="pending-note">{{ issue }}</small></div><span>{{ price(option) }}</span><a v-if="safeLink(option.booking_url)" :href="safeLink(option.booking_url)" target="_blank" rel="noopener noreferrer">详情</a><button class="text-button" :disabled="disabled || locked || (option.id === entry.selected?.id && (option.kind !== 'hotel' || entry.selected_by_user)) || !!option.constraint_issues?.length" @click="$emit('select',entry.id,option.id)">{{ option.id === entry.selected?.id && (option.kind !== 'hotel' || entry.selected_by_user) ? '已采用' : '采用并重算' }}</button></li></ul></details>
  </section>
</template>

<style scoped>
.resource-image {width:120px; height:82px; object-fit:cover; border-radius:8px; float:right; margin:0 0 12px 16px}
.alternatives li {flex-wrap:wrap; gap:12px}
.alternatives small {display:block}
</style>
