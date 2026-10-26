<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { api, ApiError, safeLink } from './api'
import LoadingState from './LoadingState.vue'
import type { Product, TripDocument } from './types'
const props = defineProps<{tripId: string; baseVersion: number; updatedAt: string; document: TripDocument; disabled: boolean; planDisabled?: boolean}>()
const emit = defineEmits<{changed: []; run: [id: string, tripId: string]; busy: [active: boolean]}>()
const form = reactive({query:'',mode:'hotel',city:'',check_in:'',check_out:'',visit_date:'',bed_type:'',hotel_stars:'',
  min_price:(props.document.product_search?.min_price ?? '') as number | '',max_price:(props.document.product_search?.max_price ?? '') as number | '',
  hotel_location:'route' as 'route' | 'custom' | 'city',hotel_area:''})
const busy = ref(false), error = ref(''), feedback = ref('')
const errorCode = ref(''), showPrevious = ref(false)
const queryTimestamp = ref('')
const latest = ref<{products: Product[]; status?: string; message: string; queried_at?: string; updated_at?: string} | null>(null)
watch(() => props.updatedAt,value => {queryTimestamp.value = value},{immediate:true})
const isHotel = computed(() => form.mode === 'hotel')
const resolvedCity = computed(() => form.city.trim() || props.document.requirements.cities[0] || '')
const hotelAreaSegments = computed(() => !props.document.itinerary_outdated ?
  props.document.itinerary?.hotel_area_segments?.filter(segment => segment.city === resolvedCity.value) || [] : [])
const recommendedAreas = computed(() => {
  const planned = !props.document.itinerary_outdated ? props.document.itinerary?.hotels.find(entry => entry.city === resolvedCity.value)?.search_areas : undefined
  const previous = planned?.length ? planned : props.document.candidates[0]?.feasibility?.hotels.find(entry => entry.city === resolvedCity.value)?.area || []
  return [...new Set([...previous,...hotelAreaSegments.value.flatMap(segment => segment.areas.map(area => area.name))])]
})
const routeAreaChoice = computed({
  get() {
    const segments = hotelAreaSegments.value
    let index = segments.findIndex(segment => segment.check_in === form.check_in && segment.check_out === form.check_out && segment.areas.some(area => area.name === form.hotel_area))
    if (index < 0) index = segments.findIndex(segment => segment.areas.some(area => area.name === form.hotel_area))
    return index < 0 ? '' : `${index}:${form.hotel_area}`
  },
  set(value: string) {
    if (!value) {
      form.hotel_area = ''
      const segments = hotelAreaSegments.value
      if (segments.length) {form.check_in = segments[0].check_in;form.check_out = segments[segments.length-1].check_out}
      return
    }
    const separator = value.indexOf(':'), segment = hotelAreaSegments.value[Number(value.slice(0,separator))]
    if (segment) {form.hotel_area = value.slice(separator+1);form.check_in = segment.check_in;form.check_out = segment.check_out}
  },
})
watch(resolvedCity,city => {
  const requirements = props.document.requirements
  const planned = !props.document.itinerary_outdated ? props.document.itinerary?.hotels.find(entry => entry.city === city) : undefined
  const route = props.document.selected_route?.days?.length ? props.document.selected_route : props.document.candidates[0]
  const dates = route?.days?.filter(day => day.city === city).map(day => day.date) || []
  form.check_in = planned?.context?.check_in || dates[0] || requirements.start_date || ''
  const nextDay = dates.length ? new Date(Date.parse(dates[dates.length-1] + 'T00:00:00Z') + 86400000).toISOString().slice(0,10) : ''
  form.check_out = planned?.context?.check_out || (nextDay && requirements.end_date ? (nextDay < requirements.end_date ? nextDay : requirements.end_date) : requirements.end_date || '')
  const previous = props.document.product_search
  const specified = props.document.requirements.hotel_areas?.[city] || ''
  if (previous?.city === city && ['hotel','marriott_hotel'].includes(previous.mode)) {
    form.hotel_location = previous.hotel_location || 'route';form.hotel_area = previous.hotel_area || ''
  } else {form.hotel_location = specified ? 'custom' : 'route';form.hotel_area = specified}
},{immediate:true,flush:'sync'})
const supportedProducts = computed(() => (props.document.products || []).filter(item => ['hotel','poi','marriott_hotel'].includes(item.search_mode)))
const results = computed(() => showPrevious.value ? supportedProducts.value : latest.value?.products ||
  supportedProducts.value.filter(item => !props.document.product_search?.result_ids || props.document.product_search.result_ids.includes(item.id)))
const sortOrder = ref('default')
const sortedResults = computed(() => {
  if (sortOrder.value === 'default') return results.value
  const field = sortOrder.value === 'rating_desc' ? 'rating' : 'price', direction = sortOrder.value === 'price_asc' ? 1 : -1
  return results.value.map((product,index) => ({product,index,value:field === 'rating' ? ratingValue(product.rating) : numericValue(product.price)})).sort((a,b) => {
    if (a.value == null) return b.value == null ? a.index - b.index : 1
    if (b.value == null) return -1
    return direction * (a.value - b.value) || a.index - b.index
  }).map(item => item.product)
})
function numericValue(value: string | number | null | undefined) {
  if (value == null || typeof value === 'string' && !value.trim()) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}
function ratingValue(value: Product['rating']) {
  const rating = numericValue(value)
  return rating != null && rating > 0 ? rating : null
}
function hotelInfo(product: Product) {
  const rating = ratingValue(product.rating)
  return [rating == null ? '暂无评分' : `评分 ${rating.toFixed(1)}`,product.star,
    product.decoration_year ? `${product.decoration_year}年装修` : '',product.nearby].filter(Boolean).join(' · ')
}
const searchRecord = computed(() => ['hotel','poi','marriott_hotel'].includes(props.document.product_search?.mode || '') ? props.document.product_search : undefined)
const statusNames: Record<string,string> = {ok:'查询完成',empty:'没有返回候选',not_configured:'尚未配置',unrecognized:'返回字段待适配',error:'查询失败'}
watch(() => props.document.requirements,req => {
  if (!form.check_in) form.check_in = req.start_date || ''
  if (!form.check_out) form.check_out = req.end_date || ''
  if (!form.visit_date) form.visit_date = req.start_date || ''
},{immediate:true})
async function search() {
  if (busy.value || props.disabled) return
  if (isHotel.value) {
    if (form.hotel_location === 'custom' && !form.hotel_area.trim()) {
      error.value = '请填写指定的住宿区域、路段或附近地标';errorCode.value = '';return
    }
    if ([form.min_price,form.max_price].some(value => value !== '' && (!Number.isInteger(value) || value < 0 || value > 10000000))) {
      error.value = '酒店价格请填写 0–10000000 之间的整数，留空表示不限';errorCode.value = '';return
    }
    if (form.min_price !== '' && form.max_price !== '' && form.min_price > form.max_price) {
      error.value = '酒店最低价不能高于最高价';errorCode.value = '';return
    }
  }
  busy.value = true; emit('busy',true); error.value = '';errorCode.value = '';feedback.value = '';showPrevious.value = false
  try {
    const result = await api<NonNullable<typeof latest.value>>(`/trips/${props.tripId}/products/search`,{method:'POST',body:JSON.stringify({...form,
      check_in:isHotel.value ? form.check_in || null : null,check_out:isHotel.value ? form.check_out || null : null,
      min_price:isHotel.value && form.min_price !== '' ? form.min_price : null,max_price:isHotel.value && form.max_price !== '' ? form.max_price : null,
      hotel_location:isHotel.value ? form.hotel_location : 'city',hotel_area:isHotel.value ? form.hotel_area : '',
      visit_date:form.mode === 'poi' ? form.visit_date || null : null,base_version:props.baseVersion})})
    latest.value = result
    if (result.updated_at) queryTimestamp.value = result.updated_at
    feedback.value = `${statusNames[result.status || 'ok'] || '查询完成'}：本次 ${result.products.length} 项候选。${result.message || ''}`
    emit('changed')
  } catch (e) {error.value = e instanceof Error ? e.message : String(e);errorCode.value = e instanceof ApiError ? e.code || '' : '';latest.value = {products:[],status:'error',message:error.value};emit('changed')}
  finally {busy.value = false; emit('busy',false)}
}
async function applyHotelArea() {
  if (busy.value || props.disabled || props.planDisabled || !resolvedCity.value || (form.hotel_location === 'custom' && !form.hotel_area.trim())) return
  busy.value = true;emit('busy',true);error.value = '';errorCode.value = ''
  try {
    const result = await api<{run_id:string}>(`/trips/${props.tripId}/hotels/area`,{method:'POST',body:JSON.stringify({
      city:resolvedCity.value,area:form.hotel_area.trim(),base_version:props.baseVersion,updated_at:queryTimestamp.value || props.updatedAt})})
    emit('run',result.run_id,props.tripId)
  } catch (cause) {error.value = cause instanceof Error ? cause.message : String(cause)}
  finally {busy.value = false;emit('busy',false)}
}
async function favorite(product: Product) {
  if (busy.value || props.disabled) return
  busy.value = true; emit('busy',true); error.value = ''
  try {
    const result = await api<{updated_at?: string}>(`/trips/${props.tripId}/products/select`,{method:'POST',body:JSON.stringify({
      product_id:product.id,base_version:props.baseVersion,updated_at:queryTimestamp.value || props.updatedAt,
    })})
    if (result.updated_at) queryTimestamp.value = result.updated_at
    emit('changed')
  } catch (e) {error.value = e instanceof Error ? e.message : String(e)}
  finally {busy.value = false; emit('busy',false)}
}
function requery(product: Product) {
  form.mode = product.search_mode === 'poi' ? 'poi' : 'hotel';form.query = product.name;form.city = product.city
  if (isHotel.value) {
    form.hotel_location = product.context?.hotel_location === 'custom' ? 'custom' : product.context?.hotel_location === 'city' ? 'city' : 'route'
    form.hotel_area = product.context?.hotel_area || ''
    form.min_price = product.context?.min_price != null ? Number(product.context.min_price) : ''
    form.max_price = product.context?.max_price != null ? Number(product.context.max_price) : ''
  }
  if (product.context?.check_in) form.check_in = product.context.check_in
  if (product.context?.check_out) form.check_out = product.context.check_out
  if (product.context?.date) form.visit_date = product.context.date
  void search()
}
</script>

<template>
  <section class="product-panel">
    <h3>查询酒店与景点门票</h3>
    <p class="subtle">可以先查资源，再规划路线。候选价格、适用日期与预订链接会保留；收藏不会改变行程。</p>
    <form @submit.prevent="search"><fieldset :disabled="disabled || busy"><div class="product-fields">
      <label>查询类型<select v-model="form.mode"><option value="hotel">酒店</option><option value="poi">景点门票参考</option></select></label>
      <label>城市<input v-model="form.city" maxlength="80" :placeholder="document.requirements.cities[0] || '例如：杭州'" :required="!document.requirements.cities.length && (isHotel || form.mode === 'poi')"></label>
      <label v-if="isHotel">入住日期<input v-model="form.check_in" type="date" required></label><label v-if="isHotel">退房日期<input v-model="form.check_out" type="date" :min="form.check_in || undefined" required></label>
      <label v-if="form.mode === 'poi'">计划游览日期<input v-model="form.visit_date" type="date"></label>
      <label v-if="isHotel">床型<select v-model="form.bed_type"><option value="">不限</option><option>大床房</option><option>双床房</option><option>多床房</option></select></label>
      <label v-if="form.mode === 'hotel'">酒店星级<input v-model="form.hotel_stars" placeholder="不限，或填写 4,5" pattern="[1-5](,[1-5])*"></label>
      <label v-if="isHotel">住宿位置<select v-model="form.hotel_location" @change="form.hotel_area = form.hotel_location === 'custom' ? document.requirements.hotel_areas?.[resolvedCity] || '' : ''"><option value="route">根据每日行程推荐区域</option><option value="custom">我指定区域或路段</option><option value="city">全市查询</option></select></label>
      <label v-if="isHotel && form.hotel_location === 'route' && hotelAreaSegments.length">推荐住宿区域<select v-model="routeAreaChoice"><option value="">查询全部推荐区域</option><optgroup v-for="(segment,index) in hotelAreaSegments" :key="segment.check_in" :label="`第${segment.day_range.start}–${segment.day_range.end}天（${segment.check_in.slice(5).replace('-','/')}–${segment.check_out.slice(5).replace('-','/')}）`"><option v-for="area in segment.areas" :key="area.name" :value="`${index}:${area.name}`">{{ area.name }}{{ area.source === 'poi_fallback' ? '附近' : '' }}</option></optgroup></select></label>
      <label v-else-if="isHotel && form.hotel_location === 'route' && recommendedAreas.length">推荐住宿区域<select v-model="form.hotel_area"><option value="">查询全部推荐区域</option><option v-for="area in recommendedAreas" :key="area" :value="area">{{ area }}附近</option></select></label>
      <label v-if="isHotel && form.hotel_location === 'custom'">区域、路段或地标<input v-model="form.hotel_area" maxlength="150" placeholder="例如：湖滨路、武林广场附近" required></label>
      <label v-if="isHotel">最低价（元/间/晚）<input v-model.number="form.min_price" type="number" min="0" max="10000000" step="1" placeholder="不限"></label>
      <label v-if="isHotel">最高价（元/间/晚）<input v-model.number="form.max_price" type="number" min="0" max="10000000" step="1" placeholder="不限"></label>
      <label class="product-query">{{ isHotel ? '酒店名称或关键词（可选）' : '景点名称（可选）' }}<input v-model="form.query" maxlength="1000" :placeholder="isHotel ? '可留空查看城市酒店' : '例如：西湖、故宫博物院'"></label>
      <button class="secondary"><span v-if="busy" class="loading-spinner" aria-hidden="true"></span>{{ busy ? '正在查询…' : '查询飞猪' }}</button>
    </div></fieldset></form>
    <p v-if="isHotel && recommendedAreas.length" class="subtle">根据行程建议住宿在：{{ recommendedAreas.map(area => area + '附近').join('、') }}。也可切换为自己指定区域。</p>
    <p v-else-if="isHotel && form.hotel_location === 'route'" class="subtle">确认路线后会显示沿线住宿建议。当前可按城市查询，或自己指定区域。</p>
    <button v-if="isHotel && form.hotel_location !== 'city' && document.itinerary && !document.itinerary_outdated && !document.pending" class="text-button" :disabled="disabled || busy || planDisabled || !resolvedCity || (form.hotel_location === 'custom' && !form.hotel_area.trim())" @click="applyHotelArea">{{ form.hotel_location === 'custom' ? '按这个区域重查行程住宿与接驳' : '按每日行程推荐区域重查住宿与接驳' }}</button>
    <p v-if="isHotel" class="subtle">价格区间按人民币每间每晚参考价筛选本次返回的候选，留空表示不限。设置区间后，未报价或每晚价格口径不明的候选暂不展示，最终价格以预订页为准。</p>
    <p v-if="form.mode === 'poi'" class="subtle">景点接口不支持指定日期库存查询。游览日期用于核对参考价，具体票种、库存和最终价格请在飞猪详情页确认。</p>
    <LoadingState v-if="busy" :title="isHotel ? '正在查询酒店候选与价格' : '正在查询景点门票参考'" detail="正在等待飞猪返回结果，请勿重复提交。" compact />
    <p v-if="error" class="pending-note" role="alert">{{ error }}<small v-if="errorCode">（{{ errorCode }}）</small></p><p v-if="feedback" class="subtle" role="status">{{ feedback }}</p>
    <p v-if="!error && !feedback && searchRecord" class="pending-note" role="status">{{ statusNames[searchRecord.status || 'ok'] || '查询记录' }}：{{ searchRecord.message || '已保留上次查询结果' }}<span v-if="searchRecord.code">（{{ searchRecord.code }}）</span><span v-if="searchRecord.queried_at"> · {{ new Date(searchRecord.queried_at).toLocaleString() }}</span></p>
    <label v-if="supportedProducts.length" class="checkbox-label"><input v-model="showPrevious" type="checkbox">查看此前酒店、门票查询与收藏</label>
    <p v-if="latest && !results.length && !busy && !error" class="pending-note">本次没有可显示的候选，请调整城市、名称或筛选条件后重新查询。</p>
    <label v-if="results.length > 1" class="subtle">排序<select v-model="sortOrder"><option value="default">默认顺序</option><option value="price_asc">价格从低到高</option><option value="price_desc">价格从高到低</option><option value="rating_desc">评分从高到低</option></select></label>
    <div class="product-list"><article v-for="product in sortedResults" :key="product.id" class="product-card">
      <img v-if="safeLink(product.image_url)" :src="safeLink(product.image_url)" :alt="product.name" loading="lazy" referrerpolicy="no-referrer">
      <div><h4>{{ product.name }}</h4><p>{{ product.city }} · {{ product.price != null ? `${product.currency} ${product.price}${product.price_basis === 'room_night' || product.price_basis === 'unknown' && product.reference_unit === 'room_night' ? ' / 每间每晚（参考）' : '（参考展示价）'}` : '接口未提供价格，查看飞猪实时价' }}</p>
        <p v-if="['hotel','marriott_hotel'].includes(product.search_mode) || product.kind === 'hotel'" class="subtle">{{ hotelInfo(product) }}</p>
        <p v-if="product.context?.check_in" class="subtle">{{ product.context.check_in }} 至 {{ product.context.check_out }}</p>
        <p v-if="product.context?.search_area" class="subtle">住宿搜索区域：{{ product.context.search_area }}附近</p>
        <p v-if="product.ticket_name || product.price_date" class="subtle">{{ product.ticket_name || '票种待确认' }} · 报价日期 {{ product.price_date || '未提供' }}</p>
        <p v-if="product.price_note" class="pending-note">{{ product.price_note }}</p><p class="subtle">查询于 {{ new Date(product.queried_at).toLocaleString() }}</p>
        <p class="subtle">适用日期、人数、票种或房型与最终价格需在预订页核对；查询结果只作为参考。</p>
        <div class="product-actions"><a v-if="safeLink(product.booking_url)" :href="safeLink(product.booking_url)" target="_blank" rel="noopener noreferrer">查看飞猪实时价</a><button class="text-button" :disabled="disabled || busy" @click="requery(product)">重新查询本项</button>
          <button class="text-button" :disabled="disabled || busy || document.reference_product_ids?.includes(product.id)" @click="favorite(product)">{{ document.reference_product_ids?.includes(product.id) ? '已收藏' : '收藏为参考' }}</button>
        </div>
        <p v-if="product.platform_hint" class="subtle">{{ product.platform_hint }}</p>
      </div>
    </article></div>
    <p v-if="searchRecord?.platform_hint" class="subtle">{{ searchRecord.platform_hint }}</p>
  </section>
</template>

<style scoped>
.product-panel {margin-top:24px; padding-top:20px; border-top:1px solid var(--line, #ddd)}
.product-fields {display:flex; flex-wrap:wrap; align-items:end; gap:12px; margin-bottom:12px}
.product-fields label {display:flex; flex-direction:column; gap:6px; flex:1; min-width:150px}
.product-query {flex:2!important}
.product-card {display:flex; gap:16px; padding:16px 0; border-top:1px solid var(--line, #ddd)}
.product-card img {width:110px; height:90px; border-radius:8px; object-fit:cover}
.product-card > div {flex:1; min-width:0}
.product-actions {display:flex; flex-wrap:wrap; gap:14px; align-items:center}
@media(max-width:600px) {.product-card {flex-direction:column}}
</style>
