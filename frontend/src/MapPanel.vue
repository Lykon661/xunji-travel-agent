<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, safeMapLink } from './api'
import { loadAmap, type AmapMap } from './amap-sdk'
import LoadingState from './LoadingState.vue'
import type { Itinerary, MapPlace } from './types'

const props = defineProps<{tripId: string; itinerary: Itinerary; version: number; configRevision: number; disabled: boolean; exporting: boolean}>()
defineEmits<{configure: []; export: []}>()
const container = ref<HTMLElement | null>(null), dayIndex = ref(0)
const configured = ref(false), loading = ref(false), error = ref(''), feedback = ref(''), qrError = ref(false)
let map: AmapMap | null = null
let renderEpoch = 0
let disposed = false
let cancelMapWait: (() => void) | null = null
function cancelPendingMap() {
  const cancel = cancelMapWait
  cancelMapWait = null
  cancel?.()
}
const day = computed(() => props.itinerary.days[dayIndex.value] || props.itinerary.days[0])
function location(place?: MapPlace | null): [number,number] | null {
  if (!place?.location) return null
  const pair = place.location.split(',').map(Number)
  return place.location.split(',').every(value => value.trim()) && pair.length === 2 && pair.every(Number.isFinite) && Math.abs(pair[0]) <= 180 && Math.abs(pair[1]) <= 90 ? [pair[0],pair[1]] : null
}
const points = computed(() => {
  if (!day.value) return []
  const visits = day.value.activities.filter(item => item.kind === 'visit')
  const items = [visits[0]?.move_before?.origin_place,...visits.map(item => item.map_place),day.value.return_move?.destination_place]
  return items.filter((place,index): place is MapPlace => !!location(place) && (index === 0 || place?.location !== items[index-1]?.location))
})
const missing = computed(() => day.value?.activities.filter(item => item.kind === 'visit' && !location(item.map_place)).map(item => item.name) || [])
const links = computed(() => (props.itinerary.personal_map?.links || []).map((link,index) => ({link:safeMapLink(link),index})).filter((item): item is {link:string;index:number} => !!item.link))
const preferred = computed(() => links.value.find(item => item.link.startsWith('https://')) || links.value[0])
const qrUrl = computed(() => preferred.value ? `/api/trips/${props.tripId}/maps/qrcode?index=${preferred.value.index}${props.version ? '&version='+props.version : ''}&at=${encodeURIComponent(props.itinerary.personal_map?.queried_at || '')}` : '')

async function render() {
  const epoch = ++renderEpoch
  cancelPendingMap()
  loading.value = true;error.value = ''
  map?.destroy();map = null
  try {
    const config = await api<{configured: boolean; js_key: string}>('/maps/web-config',{cache:'no-store'})
    if (disposed || epoch !== renderEpoch) return
    configured.value = config.configured
    if (!config.configured || !points.value.length) return
    const sdk = await loadAmap(config.js_key)
    await nextTick()
    if (disposed || epoch !== renderEpoch || !container.value) return
    map = new sdk.Map(container.value,{zoom:12,center:location(points.value[0]),viewMode:'2D'})
    const instance = map
    const ready = new Promise<void>((resolve,reject) => {
      const clean = () => {clearTimeout(timer);instance.off('complete',complete);if (epoch === renderEpoch) cancelMapWait = null}
      const complete = () => {clean();resolve()}
      const timer = window.setTimeout(() => {clean();reject(new Error('地图底图加载超时，请检查网络、JSAPI Key、安全密钥和域名白名单'))},20000)
      cancelMapWait = () => {clean();resolve()}
      instance.on('complete',complete)
    })
    await ready
    if (disposed || epoch !== renderEpoch) return
    const markers = points.value.map((place,index) => {
      const label = document.createElement('span');label.className = 'map-point-label';label.textContent = `${index+1} ${place.name}`
      return new sdk.Marker({position:location(place),title:place.name,label:{content:label,direction:'top'}})
    })
    if (points.value.length > 1) markers.push(new sdk.Polyline({path:points.value.map(place => location(place)),strokeColor:'#205b61',strokeStyle:'dashed',strokeWeight:3}))
    instance.add(markers);instance.setFitView()
  } catch (cause) {
    if (!disposed && epoch === renderEpoch) {cancelPendingMap();map?.destroy();map = null;error.value = cause instanceof Error ? cause.message : '无法加载网页地图'}
  } finally {if (!disposed && epoch === renderEpoch) loading.value = false}
}
async function copy(link: string) {
  feedback.value = ''
  try {await navigator.clipboard.writeText(link);feedback.value = '地图地址已复制，请在安装高德的手机上打开'}
  catch {feedback.value = '浏览器未允许复制，请选中下方地址手动复制'}
}
watch(() => [props.itinerary,props.configRevision,dayIndex.value],() => {void render()})
watch(qrUrl,() => {qrError.value = false})
onMounted(() => {void render()})
onUnmounted(() => {disposed = true;renderEpoch++;cancelPendingMap();map?.destroy()})
</script>

<template>
  <section class="trip-map-panel">
    <div class="section-intro with-action"><div><h3>行程地图</h3><p>在电脑上按日查看地点，在手机高德中保存专属地图。</p></div><button class="text-button" @click="$emit('configure')">网页地图配置</button></div>
    <label>查看哪一天<select v-model.number="dayIndex"><option v-for="(item,index) in itinerary.days" :key="item.date" :value="index">{{ item.date }} · {{ item.city }}</option></select></label>
    <LoadingState v-if="loading" title="正在加载行程地图" compact />
    <div ref="container" v-show="configured && points.length && !error" class="amap-preview" aria-label="当天行程地点地图"></div>
    <p v-if="!loading && !configured" class="pending-note">网页地图需要单独的 Web端(JSAPI) Key 和安全密钥。<button class="text-button" @click="$emit('configure')">配置网页地图</button></p>
    <p v-if="error" class="pending-note" role="alert">{{ error }} <button class="text-button" @click="render">重新加载</button></p>
    <p v-if="!points.length" class="pending-note">当天还没有确认坐标的地点，补全定位后可以显示地图。</p>
    <p v-else class="subtle">虚线表示地点顺序，实际道路与公交路线请查看各段导航。</p>
    <ol class="map-point-list"><li v-for="(place,index) in points" :key="index+'-'+place.location"><span>{{ place.name }}</span><a v-if="safeMapLink(place.map_url)" :href="safeMapLink(place.map_url)" target="_blank" rel="noopener noreferrer">查看地点</a></li></ol>
    <p v-if="missing.length" class="pending-note">未定位：{{ missing.join('、') }}</p>
    <div class="map-export-actions"><button v-if="!version" class="secondary" :disabled="disabled || exporting" @click="$emit('export')"><span v-if="exporting" class="loading-spinner" aria-hidden="true"></span>{{ exporting ? '正在生成导出入口…' : '导出到手机高德' }}</button><span v-if="itinerary.personal_map?.queried_at" class="subtle">导出于 {{ new Date(itinerary.personal_map.queried_at).toLocaleString() }}</span></div>
    <div v-if="preferred" class="map-mobile-export">
      <div><img v-if="!qrError" :src="qrUrl" alt="高德专属地图手机导出二维码" width="180" height="180" @error="qrError = true"><p v-else class="pending-note">二维码加载失败，可复制地址；更新依赖后重试。</p></div>
      <div><p>请用安装高德地图的手机打开。扫码唤起取决于手机扫描器，App 地址无法作为普通电脑网页查看。</p><div class="map-export-actions"><a :href="preferred.link" :target="preferred.link.startsWith('http') ? '_blank' : undefined" rel="noopener noreferrer">{{ preferred.link.startsWith('http') ? '打开高德导出入口' : '在手机高德中打开' }}</a><button class="text-button" @click="copy(preferred.link)">复制地址</button></div><input class="map-copy-address" :value="preferred.link" readonly aria-label="手机高德地图地址" @focus="($event.target as HTMLInputElement).select()"><p v-if="feedback" role="status">{{ feedback }}</p></div>
    </div>
    <p v-if="itinerary.personal_map?.skipped?.length" class="pending-note">未导入点位：{{ itinerary.personal_map.skipped.join('、') }}</p>
  </section>
</template>
