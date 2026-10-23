<script setup lang="ts">
import { computed } from 'vue'
import { safeLink, safeMapLink } from './api'
import type { Itinerary, Movement } from './types'

const props = withDefaults(defineProps<{itinerary: Itinerary; interactive?: boolean; disabled?: boolean; editableDates?: boolean;
  locks?: string[]; requiredIds?: string[]; selectable?: boolean; selectedIds?: string[]; allowTaxi?: boolean}>(),
  {interactive:false,disabled:false,editableDates:false,locks:() => [],requiredIds:() => [],selectable:false,selectedIds:() => [],allowTaxi:false})
const emit = defineEmits<{lock:[id:string]; source:[id:string]; ticket:[id:string]; editDate:[date:string]; select:[id:string]; taxi:[date:string,id:string]}>()
const hotelAreaCities = computed(() => [...new Set((props.itinerary.hotel_area_segments || []).map(segment => segment.city))])
function money(value: number | null | undefined) {return value == null ? '未提供价格' : new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format(value)}
function previousVisit(day: Itinerary['days'][number], id: string) {
  const visits = day.activities.filter(activity => activity.kind === 'visit')
  return visits[visits.findIndex(activity => activity.id === id) - 1]
}
function interStopTravel(day: Itinerary['days'][number]) {
  const moves = day.activities.filter(activity => activity.kind === 'visit').slice(1).map(activity => activity.move_before)
  return {minutes:moves.reduce((total,move) => total + (move?.minutes ?? move?.estimate_minutes ?? 0),0),
    estimated:moves.some(move => move?.minutes == null)}
}
function movement(move: Movement | undefined) {
  if (!move) return '交通方式与耗时未取得'
  const modes: Record<string,string> = {walking:'步行',transit:'公交/地铁',driving:'驾车',bicycling:'骑行'}
  const label = move.transport_label || (move.mode === 'transit' && move.summary ? (/(地铁|轨道)/.test(move.summary) ? '地铁' : '公交') : modes[move.mode || ''] || '交通方式未取得')
  return `${label} ${move.minutes != null ? `约 ${move.minutes} 分钟` : '耗时未取得'}${move.summary ? ' · '+move.summary : ''}${move.note ? ' · '+move.note : ''}`
}
function evidenceKind(activity: Itinerary['days'][number]['activities'][number]) {
  if (activity.source_kind === 'flyai') return '你确认采用的飞猪商品'
  return activity.evidence?.kind === 'image' ? '攻略图片 ' + activity.evidence.image_index : '攻略正文'
}
</script>

<template>
  <p v-if="itinerary.recovery_note" class="pending-note">{{ itinerary.recovery_note }}</p>
  <p v-for="note in itinerary.constraint_notes || []" :key="note" class="subtle">{{ note }}</p>
  <section v-if="itinerary.hotel_area_segments?.length" class="resource-block">
    <h3>住宿地段推荐</h3>
    <div v-for="city in hotelAreaCities" :key="city">
      <h4>{{ city }}</h4>
      <div v-for="segment in itinerary.hotel_area_segments.filter(item => item.city === city)" :key="segment.check_in">
        <p>第 {{ segment.day_range.start }}–{{ segment.day_range.end }} 天 · {{ segment.nights }} 晚 · 推荐住在：{{ segment.areas.map(area => area.name + (area.source === 'poi_fallback' ? '附近' : '')).join(' / ') || '地段待确认' }}</p>
        <p class="subtle">{{ segment.reason }}<span v-for="area in segment.areas" :key="area.name"> · {{ area.name }}{{ area.source === 'poi_fallback' ? '附近' : '' }}：{{ area.reason }}</span></p>
      </div>
    </div>
  </section>
  <section v-for="(day,index) in itinerary.days" :key="day.date" class="day-section">
    <header class="day-heading">
      <div class="day-number"><small>DAY</small>{{ index + 1 }}</div>
      <div class="day-title"><h3>{{ day.city }}</h3><span>{{ day.date }}</span></div>
      <button v-if="editableDates" class="secondary small" :disabled="disabled" @click="emit('editDate',day.date)">调整这一天</button>
    </header>
    <p v-if="day.weather" class="weather-note">
      <template v-if="day.weather.available">
        天气预报：白天 {{ day.weather.dayweather }}<span v-if="day.weather.daytemp != null"> {{ day.weather.daytemp }}℃</span>，夜间 {{ day.weather.nightweather }}。<span v-if="day.weather.reporttime">发布于 {{ day.weather.reporttime }}。</span>
      </template>
      <template v-else>天气待复核：</template>{{ day.weather.note }}
    </p>
    <div class="day-facts">
      <p v-if="day.route_basis" class="subtle">{{ day.route_basis }}</p>
      <p v-if="day.activities.filter(activity => activity.kind === 'visit').length > 1" class="movement-note">
        活动点之间移动{{ interStopTravel(day).estimated ? '暂计' : '约' }} {{ interStopTravel(day).minutes }} 分钟<span v-if="interStopTravel(day).estimated">，部分路段耗时未取得</span>。
      </p>
      <p v-if="day.walking_meters != null" class="subtle">交通接驳步行约 {{ (day.walking_meters / 1000).toFixed(1) }} 公里，不含景点内部游览。</p>
      <p v-for="(transfer,transferIndex) in day.transport_transfers || []" :key="transferIndex" class="movement-note">
        {{ transfer.label }}：{{ transfer.move.origin }} → {{ transfer.move.destination }}，{{ movement(transfer.move) }}
      </p>
    </div>
    <div class="timeline">
      <article
        v-for="activity in day.activities"
        :key="activity.id"
        class="activity"
        :class="{rest:activity.kind === 'rest',selected:selectedIds.includes(activity.id),locked:locks.includes(activity.id)}"
      >
        <div class="activity-time">{{ activity.start }}<small>{{ activity.end }}</small></div>
        <div class="activity-content">
          <p v-if="previousVisit(day,activity.id)" class="movement-note movement-leg">
            {{ previousVisit(day,activity.id)?.name }} → {{ activity.name }}：{{ movement(activity.move_before) }}
          </p>
          <div class="activity-heading">
            <h4>
              {{ activity.name }}<span
                v-if="requiredIds.includes(activity.id) || activity.source_references?.some(reference => requiredIds.includes(reference.stop_id))"
                class="badge required"
              >必去</span>
            </h4>
            <button
              v-if="interactive && (activity.kind === 'visit' || locks.includes(activity.id))"
              class="text-button"
              :disabled="disabled"
              @click="emit('lock',activity.id)"
            >{{ locks.includes(activity.id) ? '已锁定 · 解锁' : '锁定' }}</button>
            <label v-if="selectable && activity.kind === 'visit'" class="activity-selection">
              <input
                type="checkbox"
                :checked="selectedIds.includes(activity.id)"
                :disabled="disabled"
                :aria-label="'选择调整：' + activity.name"
                @change="emit('select',activity.id)"
              >选择
            </label>
          </div>
          <p v-if="previousVisit(day,activity.id) && activity.move_before?.refresh_note" class="pending-note">{{ activity.move_before.refresh_note }}</p>
          <p v-if="previousVisit(day,activity.id) && activity.move_before?.walking_alternative" class="subtle">
            仅作步行备选参考：{{ movement(activity.move_before.walking_alternative) }}；长路段仍待补齐公交或地铁方案。
          </p>
          <p v-if="previousVisit(day,activity.id) && activity.move_before?.queried_at" class="subtle">
            高德查询于 {{ new Date(activity.move_before.queried_at).toLocaleString() }}；耗时为查询时参考。<span v-if="activity.move_before.evidence_id">依据 {{ activity.move_before.evidence_id }}</span>
          </p>
          <p v-if="activity.category !== 'dining' && activity.ticket_claim" class="ticket-price">攻略门票信息：{{ activity.ticket_claim }}</p>
          <p v-if="activity.ticket_query_required && !activity.ticket_quote" class="ticket-price">门票：默认免费（0元）。未返回价格，按你的默认规则处理。</p>
          <p v-if="activity.category !== 'dining' && activity.ticket_quote && !activity.ticket_quote.options?.length" class="ticket-price">
            <template v-if="activity.ticket_quote.assumed_free || activity.ticket_quote.price == null">门票：<strong>默认免费（0元）</strong></template>
            <template v-else-if="activity.ticket_quote.reported_free">基础参观：<strong>免费（0元）</strong></template>
            <template v-else>飞猪门票参考价：<strong>{{ money(activity.ticket_quote.price) }}</strong></template>
            <span v-if="activity.ticket_quote?.ticket_name"> · {{ activity.ticket_quote.ticket_name }}</span>
            <span v-if="activity.ticket_quote?.price_date"> · 适用日期 {{ activity.ticket_quote.price_date }}</span>
            <br>{{ activity.ticket_quote.note }}
            <a v-if="safeLink(activity.ticket_quote?.booking_url)" :href="safeLink(activity.ticket_quote?.booking_url)" target="_blank" rel="noopener noreferrer">查看票种与预订页</a>
          </p>
          <div v-if="activity.category !== 'dining' && activity.ticket_quote?.options?.length" class="ticket-candidates">
            <p class="subtle">可选票种（按游玩项目选择，分别计价）：</p>
            <p v-for="option in activity.ticket_quote.options" :key="option.id">
              {{ option.ticket_name || option.name }} · {{ option.assumed_free || option.price == null ? '默认免费（0元）' : money(option.price) }}<span v-if="option.price_date"> · {{ option.price_date }}</span><span v-if="option.assumed_free"> · 未返回价格</span>
              <a v-if="safeLink(option.booking_url)" :href="safeLink(option.booking_url)" target="_blank" rel="noopener noreferrer">查看票种</a>
            </p>
          </div>
          <div v-if="activity.category !== 'dining' && activity.ticket_quote?.candidates?.length" class="ticket-candidates">
            <p class="subtle">地点匹配候选：</p>
            <p v-for="option in activity.ticket_quote.candidates" :key="option.id">
              {{ option.name }} · {{ option.ticket_name }} · {{ option.assumed_free || option.price == null ? '默认免费（0元）' : money(option.price) }}（参考）
              <a v-if="safeLink(option.booking_url)" :href="safeLink(option.booking_url)" target="_blank" rel="noopener noreferrer">查看详情</a>
            </p>
          </div>
          <p v-if="activity.duration_basis" class="subtle">{{ activity.duration_basis }}</p>
          <p v-if="activity.meal_break" class="pending-note">
            午餐预留 {{ activity.meal_break.start }}–{{ activity.meal_break.end }}（{{ activity.meal_break.duration_minutes }} 分钟）。{{ activity.meal_break.adjustment }}；本段总时长包含午餐，实际游玩约 {{ activity.visit_minutes }} 分钟。
          </p>
          <p v-if="activity.category === 'dining'" class="subtle">攻略明确推荐的餐饮点，可选安排。</p>
          <p v-if="activity.opening_claim" class="subtle">攻略开放信息：{{ activity.opening_claim }}</p>
          <p v-if="activity.adjustment" class="subtle">安排说明：{{ activity.adjustment }}</p>
          <details v-if="activity.evidence" class="evidence">
            <summary>参考：{{ activity.source_title }}</summary>
            <blockquote>{{ activity.evidence.quote }}</blockquote>
            <p class="subtle">{{ evidenceKind(activity) }}</p>
            <button v-if="activity.source_kind !== 'flyai'" class="text-button" @click="emit('source',activity.source_id || '')">查看攻略依据</button>
            <a v-if="safeLink(activity.source_url)" :href="safeLink(activity.source_url)" target="_blank" rel="noopener noreferrer">打开来源</a>
          </details>
          <details v-if="(activity.source_references?.length || 0) > 1" class="evidence">
            <summary>查看合并的攻略来源</summary>
            <div v-for="reference in activity.source_references" :key="reference.stop_id">
              <strong>{{ reference.source_title }}</strong>
              <blockquote v-if="reference.evidence">{{ reference.evidence.quote }}</blockquote>
              <button v-if="reference.source_id" class="text-button" @click="emit('source',reference.source_id)">查看这篇攻略</button>
            </div>
          </details>
          <div class="activity-links">
            <a v-if="safeMapLink(activity.map_url)" :href="safeMapLink(activity.map_url)" target="_blank" rel="noopener noreferrer" class="inline-link">高德地点</a>
            <a
              v-if="previousVisit(day,activity.id) && safeMapLink(activity.move_before?.navigation_url)"
              :href="safeMapLink(activity.move_before?.navigation_url)"
              target="_blank"
              rel="noopener noreferrer"
              class="inline-link"
            >导航路线</a>
            <button
              v-if="interactive && activity.kind === 'visit' && activity.category !== 'dining'"
              class="text-button"
              :disabled="disabled || locks.includes(activity.id)"
              @click="emit('ticket',activity.id)"
            >查询飞猪票种参考</button>
            <button
              v-if="interactive && allowTaxi && previousVisit(day,activity.id) && activity.move_before?.origin_place"
              class="text-button"
              :disabled="disabled"
              @click="emit('taxi',day.date,activity.id)"
            >生成打车入口</button>
          </div>
        </div>
      </article>
    </div>
    <p v-if="!day.activities.length" class="pending-note">这一天缺少有来源的活动。</p>
    <section v-if="day.meal_recommendations?.length" class="meal-recommendations">
      <h4>当天区域的餐饮推荐</h4>
      <p class="subtle">按午晚餐附近活动筛选；全程同一门店默认只推荐一次，不同分店分别显示。</p>
      <div v-for="meal in (['lunch','dinner'] as const)" :key="meal" class="meal-options">
        <template v-if="day.meal_recommendations.some(item => item.meal === meal)">
          <h5>{{ meal === 'lunch' ? '午餐附近' : '晚餐附近' }}</h5>
          <article v-for="item in day.meal_recommendations.filter(item => item.meal === meal)" :key="item.id">
            <strong>{{ item.name }}</strong>
            <p>靠近 {{ item.nearby_activity }}，直线约 {{ item.distance_meters < 1000 ? item.distance_meters+' 米' : (item.distance_meters/1000).toFixed(1)+' 公里' }}。</p>
            <p v-if="item.map_place.address" class="subtle">{{ item.map_place.address }}</p>
            <p v-if="item.opening_claim" class="subtle">攻略营业信息：{{ item.opening_claim }}</p>
            <details v-if="item.evidence" class="evidence">
              <summary>参考：{{ item.source_title }}</summary>
              <blockquote>{{ item.evidence.quote }}</blockquote>
              <button class="text-button" @click="emit('source',item.source_id || '')">查看攻略依据</button>
            </details>
            <a v-if="safeMapLink(item.map_url)" :href="safeMapLink(item.map_url)" target="_blank" rel="noopener noreferrer">查看餐饮地点</a>
          </article>
        </template>
      </div>
      <p class="subtle">营业时间、道路通行和实际消费需核对；餐饮预算仍采用每日估算。</p>
    </section>
    <div v-if="day.gaps.length" class="day-gaps"><p v-for="gap in day.gaps" :key="gap">{{ gap }}</p></div>
  </section>
</template>
