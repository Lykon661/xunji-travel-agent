<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, safeMapLink } from './api'
import ResourceBlock from './ResourceBlock.vue'
import ModelSettings from './ModelSettings.vue'
import CrawlerSettings from './CrawlerSettings.vue'
import AmapSettings from './AmapSettings.vue'
import FlyaiSettings from './FlyaiSettings.vue'
import InterviewPanel from './InterviewPanel.vue'
import CoordinatorMessages from './CoordinatorMessages.vue'
import ProductPanel from './ProductPanel.vue'
import LoadingState from './LoadingState.vue'
import MapPanel from './MapPanel.vue'
import PlanView from './PlanView.vue'
import ReviewPanel from './ReviewPanel.vue'
import RouteChat from './RouteChat.vue'
import AppSidebar from './AppSidebar.vue'
import SettingsMenu, { type SettingsKind } from './SettingsMenu.vue'
import WorkflowSteps from './WorkflowSteps.vue'
import WelcomeView from './WelcomeView.vue'
import GuideCard from './GuideCard.vue'
import SourceModal from './SourceModal.vue'
import BudgetPanel from './BudgetPanel.vue'
import { money } from './format'
import { resolveWorkflowTab, workflowStamp, type WorkflowTab } from './workflow'
import type { Capabilities, ModelConfiguration, Candidate, InterviewHistoryEntry, InterviewQuestion, InterviewResponse, ProgressEvent, PlanningConstraint, Requirements, Source, Trip, TripSummary, Version, VersionSummary, TripDocument, MapResult } from './types'

const trips = ref<TripSummary[]>([]), current = ref<Trip | null>(null), versions = ref<VersionSummary[]>([])
const versionDocuments = ref<Record<number,TripDocument>>({})
const capabilities = ref<Partial<Capabilities>>({})
const tab = ref<WorkflowTab>('requirements')
const error = ref(''), busy = ref(false), dialog = ref<'create' | 'source' | null>(null)
const actionLabel = ref(''), connectionState = ref('')
const amapConfigRevision = ref(0)
const tripTitle = ref(''), message = ref(''), selectedVersion = ref(0), progress = ref<ProgressEvent[]>([])
const sourceTitle = ref(''), sourceUrl = ref(''), sourceText = ref(''), sourceCity = ref(''), sourceFiles = ref<File[]>([])
const displayedSource = ref<Source | null>(null)
const modelSettingsOpen = ref(false), crawlerSettingsOpen = ref(false), amapSettingsOpen = ref(false), flyaiSettingsOpen = ref(false)
const runningStages = ['queued','coordinating','discovering','parsing','interviewing','decision','candidates','assessing','revising_candidate','transport','hotels','activities','tickets','checking','reviewing','repairing','saving']
function tripRunning(trip: TripSummary) {
  return current.value?.id === trip.id ? current.value.run?.status === 'running' : !!trip.active_run && runningStages.includes(trip.stage)
}
const modelTasksActive = computed(() => busy.value || trips.value.some(tripRunning))
const guideCity = ref(''), guideKeywords = ref(''), guideFeedback = ref('')
const selectedGuideIds = ref<string[]>([])
const routeChat = ref<InstanceType<typeof RouteChat> | null>(null)
const selectedActivityIds = ref<string[]>([])
const guideSearchInput = ref<HTMLInputElement | null>(null)
let events: EventSource | null = null
let selectionEpoch = 0
let refreshEpoch = 0
let capabilitiesEpoch = 0
let navigationStamp = ''
let runPoll: number | undefined
let reconciliation: Promise<void> | null = null
let inputBaseline = ''
const automaticRepairs = new Set<string>()
const extraRequirements = {origin_address:'',children:0,children_ages:[] as number[],earliest_departure:null,latest_arrival:null,latest_return:null,
  max_transport_hours:null,direct_only:true,flight_cabin:'',train_seat:'',hotel_type:'',hotel_stars:'',hotel_areas:{} as Record<string,string>,max_walk_per_day:8000,walking_limit_exempt_dates:[] as string[],allow_taxi:false,wish_places:[] as NonNullable<Requirements['wish_places']>,required_stop_ids:[] as string[],excluded_stop_ids:[] as string[]}
const requirements = reactive<Requirements>({origin:null,cities:[],start_date:null,end_date:null,people:null,rooms:null,budget:null,preferences:'',transport:'any',local_transport:'public',bed_type:'双床房',return_to_origin:true,self_arranged_round_trip:false,...extraRequirements})
const childAgeText = ref('')
const hotelAreas = reactive<Record<string,string>>({})
const cityText = ref('')

const destinationCities = computed(() => cityText.value.split(/[、,，\n]/).map(value => value.trim()).filter(Boolean))
const wishCity = ref(''), wishName = ref(''), wishDate = ref(''), wishDuration = ref<number | null>(null), wishError = ref('')
const requiredGuideStopIds = computed(() => (requirements.required_stop_ids || []).filter(id => !id.startsWith('wish:')))
watch(destinationCities, cities => {
  if (!cities.includes(wishCity.value)) wishCity.value = cities[0] || ''
})
function isWishDateValid(date: string | null) {
  return !!date && (!requirements.start_date || date >= requirements.start_date) && (!requirements.end_date || date <= requirements.end_date)
}
watch(() => [requirements.start_date, requirements.end_date], () => {
  if (wishDate.value && !isWishDateValid(wishDate.value)) wishDate.value = ''
})
function addWishPlace() {
  wishError.value = ''
  const city = wishCity.value.trim(), name = wishName.value.trim(), cleanName = name.replace(/\s+/g, '')
  if (!destinationCities.value.includes(city)) { wishError.value = '请先填写并选择当前目的地城市'; return }
  if (!cleanName) { wishError.value = '请输入想去的地点名称'; return }
  const list = requirements.wish_places || []
  if (list.length >= 20) { wishError.value = '最多添加 20 个想去的地点'; return }
  if (list.some(item => item.city === city && item.name.replace(/\s+/g, '') === cleanName)) { wishError.value = '同一城市不能重复添加相同名称的地点'; return }
  const date = wishDate.value || null
  if (date && !isWishDateValid(date)) {
    wishError.value = '指定日期必须在出发与结束日期之间'; return
  }
  const rawDuration = wishDuration.value
  const duration = rawDuration === null || rawDuration === undefined || String(rawDuration) === '' || Number.isNaN(Number(rawDuration)) ? null : Math.round(Number(rawDuration))
  if (duration !== null && (duration < 10 || duration > 720)) { wishError.value = '游玩时长需在 10 到 720 分钟之间'; return }
  requirements.wish_places = [...list, {city, name, date, duration_minutes: duration}]
  wishName.value = ''; wishDate.value = ''; wishDuration.value = null
}
function removeWishPlace(index: number) {
  wishError.value = ''
  requirements.wish_places = (requirements.wish_places || []).filter((_, i) => i !== index)
}
const taxiResult = ref<MapResult | null>(null)

const doc = computed(() => selectedVersion.value ? versionDocuments.value[selectedVersion.value] : current.value?.document)
const isRunning = computed(() => current.value?.run?.status === 'running' || busy.value)
const isHistory = computed(() => selectedVersion.value > 0)
const canEdit = computed(() => !!current.value && !isRunning.value && !isHistory.value)
const itinerary = computed(() => doc.value?.itinerary)
const visibleCandidates = computed(() => {
  if (!isHistory.value && (isRunning.value || ['interview','interview_confirmation','decision','decision_confirmation'].includes(doc.value?.pending?.kind || ''))) return []
  const candidates = (doc.value?.candidates || []).filter(candidate => isHistory.value ||
    !!candidate.draft?.days?.length && reviewReady(candidate) && !candidate.confirmation_blockers?.length)
  const selected = candidates.find(candidate => candidate.id === doc.value?.selected_route?.id)
  return selected ? [selected] : candidates.slice(0,1)
})
const blockedCandidate = computed(() => {
  if (isHistory.value || isRunning.value || interviewPending.value) return null
  return (doc.value?.candidates || []).find(candidate => candidate.draft?.days?.length &&
    (!!candidate.confirmation_blockers?.length || !reviewReady(candidate))) || null
})
const planningDecision = computed(() => ['decision','decision_confirmation'].includes(doc.value?.pending?.kind || '') ? doc.value?.pending || null : null)
const selectedActivities = computed(() => (doc.value?.candidates[0]?.draft?.days || []).flatMap(day => day.activities
  .filter(activity => activity.kind === 'visit' && selectedActivityIds.value.includes(activity.id))
  .map(activity => ({id:activity.id,name:activity.name,date:day.date}))))
const showRouteChat = computed(() => tab.value === 'routes' && !isHistory.value && !!doc.value?.candidates[0]?.draft?.days?.length && doc.value.pending?.kind === 'selection')
watch(() => [current.value?.id,selectedVersion.value,doc.value?.candidates[0]?.id], (value,previous) => {
  if (value[0] !== previous?.[0] || value[1] !== previous?.[1]) selectedActivityIds.value = []
  else {
    const ids = new Set((doc.value?.candidates[0]?.draft?.days || []).flatMap(day => day.activities.map(activity => activity.id)))
    selectedActivityIds.value = selectedActivityIds.value.filter(id => ids.has(id))
  }
})
const savedItineraryVersion = computed(() => current.value?.document.itinerary_saved_version ?? null)
const canSaveItinerary = computed(() => canEdit.value && !!itinerary.value && savedItineraryVersion.value === null &&
  !doc.value?.itinerary_outdated && !doc.value?.pending && ['ready','completed','incomplete','conflicts'].includes(doc.value?.stage || ''))
const itineraryStatus = computed(() => isHistory.value ? `已保存 · v${selectedVersion.value}` : savedItineraryVersion.value !== null ? `已保存 · v${savedItineraryVersion.value}` : '未保存的草稿')
const visibleSources = computed(() => isHistory.value ? doc.value?.sources_snapshot || current.value?.sources || [] : current.value?.sources || [])
const sourceStops = computed(() => new Map([...visibleSources.value.flatMap(s => s.extraction?.stops || []),...(doc.value?.supplements || [])].map(s => [s.id,s])))
const stopNameMap = computed(() => ({
  ...Object.fromEntries(
    Object.entries(doc.value?.stops || {})
      .filter(([, stop]) => stop?.name)
      .map(([id, stop]) => [id, stop.name])
  ),
  ...Object.fromEntries([...sourceStops.value.entries()].map(([id, stop]) => [id, stop.name])),
}))
function roundAnswer(round: InterviewHistoryEntry, question: InterviewQuestion): string {
  const direct = round.answers?.[question.id]
  if (direct) return direct
  const optId = round.selected_options?.[question.id]
  if (optId) {
    for (const opt of question.options || []) {
      if (typeof opt === 'object' && opt !== null && opt.id === optId) return opt.label || opt.id
      if (typeof opt === 'string' && opt === optId) return opt
    }
    if (round.summary) return round.summary
    return optId
  }
  return round.summary || ''
}
function roundFallbackText(round: InterviewHistoryEntry): string {
  if (round.summary?.trim()) return round.summary.trim()
  if (round.place_selection) {
    const name = round.place_selection.name || round.place_selection.label || round.place_selection.title || round.place_selection.place_id || round.place_selection.stop_id
    return name ? `选择了地点：${name}` : '已完成地点选择'
  }
  if (round.hub_selection) {
    const name = round.hub_selection.name || round.hub_selection.area || round.hub_selection.label || round.hub_selection.title || round.hub_selection.city
    return name ? `选择了住宿区域：${name}` : '已完成住宿区域选择'
  }
  return ''
}
function hasRoundContent(round: InterviewHistoryEntry): boolean {
  return Boolean(round.questions?.length || roundFallbackText(round) || round.note?.trim())
}
const activeConstraints = computed(() => (requirements.constraints || []).filter(item => !item.superseded_by && !item.revoke))
const preferencesEdited = computed(() => requirements.preferences !== (doc.value?.requirements.preferences ?? ''))
const constraintsEdited = computed(() => JSON.stringify(requirements.constraints || []) !== JSON.stringify(doc.value?.requirements.constraints || []))
// Deleting keeps whatever this condition replaced inactive: the user asked to drop the condition, not to restore an older one.
function removeConstraint(id: string) { requirements.constraints = (requirements.constraints || []).filter(item => item.id !== id) }
function toggleConstraintStrength(id: string) {
  requirements.constraints = (requirements.constraints || []).map(item => item.id === id ? {...item,strength:item.strength === 'hard' ? 'soft' : 'hard'} : item)
}
const anchorLabels = {sunrise:'看日出',sunset:'看日落',appointment:'按预约时刻','':''}
const modeLabels = {train:'火车',flight:'航班',ferry:'船班','':'班次'}
function stopName(id: string) { return stopNameMap.value[id] || '攻略依据已变化的地点' }
function constraintText(item: PlanningConstraint) {
  const stop = item.scope.stop_ids[0] ? stopName(item.scope.stop_ids[0]) : ''
  const body = {
    fewer_walking:item.meters ? `每天步行不超过${item.meters}米` : '少走路',
    fixed_order:`先去${stopName(item.before)}，再去${stopName(item.after)}`,
    time_anchor:`${stop}${anchorLabels[item.anchor]}${item.at ? ' '+item.at : ''}`,
    visit_duration:`${stop}游玩${item.minutes}分钟`,
    transfer_buffer:`${modeLabels[item.mode]}出发前预留${item.minutes}分钟`,
    booking_prerequisite:`${stop}须先完成预约再安排`,
    transit_rule:item.rule === 'public_only' ? '市内只用公共交通' : '按真实公交查询结果自动取舍',
    daily_min_stops:`每天至少安排${item.count}个景点`,
  }[item.kind]
  // Without a distance, "less walking" is only ever a target.
  const soft = item.strength === 'soft' || (item.kind === 'fewer_walking' && !item.meters)
  return (item.scope.dates.length ? item.scope.dates.join('、')+'：' : '')+body+(soft ? '（尽量）' : '')
}
const sourceMap = computed(() => new Map(visibleSources.value.map(s => [s.id,s])))
const interviewPending = computed(() => ['interview','interview_confirmation','decision','decision_confirmation'].includes(doc.value?.pending?.kind || '') ? doc.value?.pending : null)
const tripDayCount = computed(() => {
  if (!requirements.start_date || !requirements.end_date) return 0
  return Math.max(0, Math.round((Date.parse(requirements.end_date) - Date.parse(requirements.start_date)) / 86400000) + 1)
})
const candidateInputsChanged = computed(() => {
  if (!current.value) return false
  const saved = current.value.document
  const form = formData()
  const normalized = {...extraRequirements,...saved.requirements,local_transport:saved.requirements.local_transport || 'public',
    hotel_areas:Object.fromEntries(Object.entries(saved.requirements.hotel_areas || {}).sort(([left],[right]) => left.localeCompare(right))),
    wish_places:(saved.requirements.wish_places || []).filter(item => (saved.requirements.cities || []).includes(item.city) && item.name?.trim()).map(item => ({
      city:item.city,name:item.name.trim(),
      date:item.date && (!saved.requirements.start_date || item.date >= saved.requirements.start_date) && (!saved.requirements.end_date || item.date <= saved.requirements.end_date) ? item.date : null,
      duration_minutes:item.duration_minutes ?? null,
    }))}
  return JSON.stringify([...selectedGuideIds.value].sort()) !== JSON.stringify([...(saved.selected_source_ids || [])].sort()) ||
    (Object.keys(form) as (keyof Requirements)[]).some(key => JSON.stringify(form[key]) !== JSON.stringify(normalized[key]))
})
const labels: Record<string,string> = {draft:'填写旅行需求',queued:'等待处理',coordinating:'理解需求',discovering:'读取所选攻略',parsing:'解析攻略',interviewing:'梳理旅行偏好',decision:'回答当前路线问题',candidates:'生成路线',assessing:'查证候选资源',revising_candidate:'局部修改候选',waiting:'等待你的回答或选择',transport:'查询交通',hotels:'匹配住宿',activities:'细化每日行程',tickets:'查询景点门票',checking:'检查安排',repairing:'调整冲突',saving:'整理草稿',ready:'每日行程待保存',completed:'行程已保存',incomplete:'每日行程待补全',conflicts:'每日行程需调整',failed:'运行中断',recoverable:'可以继续'}
const tabs = [{key:'requirements',label:'旅行需求'},{key:'guides',label:'参考攻略'},{key:'interview',label:'需求追问'},{key:'routes',label:'候选路线'},{key:'itinerary',label:'每日行程'},{key:'resources',label:'酒店与门票'}] as const
const planningStep = computed(() => {
  const document = current.value?.document
  if (!document) return '填写旅行需求'
  if (busy.value && actionLabel.value) return actionLabel.value
  if (current.value?.run?.status === 'running') return document.progress_message || labels[document.progress_stage || document.stage] || '正在规划旅行'
  if (['failed','recoverable'].includes(document.stage)) return `${labels[document.progress_stage || ''] || '规划旅行'}（已中断）`
  const pending: Record<string,string> = {requirements:'补充旅行需求',guides:'选择参考攻略',sources:'补充参考攻略',interview:'回答旅行偏好',interview_confirmation:'确认旅行需求',selection:'确认完整路线',decision:'回答当前路线问题',decision_confirmation:'确认主理人理解'}
  return pending[document.pending?.kind || ''] || labels[document.stage] || '查看当前行程'
})
const workflowStep = computed(() => current.value ? resolveWorkflowTab(current.value) : 'requirements')
const backSteps = computed(() => {
  const steps = tabs.filter(item => item.key !== 'resources')
  return workflowStep.value === 'resources' ? steps : steps.slice(0,steps.findIndex(item => item.key === workflowStep.value))
})
const previousStep = computed(() => tabs[tabs.findIndex(item => item.key === tab.value)-1])

async function action(fn: () => Promise<void>, label = '正在处理当前操作') {
  error.value = ''; busy.value = true;actionLabel.value = label
  try { await fn() } catch (e) { error.value = e instanceof Error ? e.message : '操作失败' }
  finally { busy.value = false;actionLabel.value = '' }
}
function moveToStep(target: WorkflowTab) {
  tab.value = target
  void nextTick(() => {
    const panel = document.querySelector<HTMLElement>('.main-panel')
    panel?.scrollIntoView({block:'start',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'})
  })
}
async function backToStep(target: WorkflowTab) {
  if (isRunning.value) return
  if (isHistory.value) {moveToStep(target);return}
  if (!current.value || target === 'resources') return
  const targetLabel = tabs.find(item => item.key === target)?.label
  const clearedRecords = {
    requirements:'参考攻略、需求追问、候选路线、每日行程及历史版本',
    guides:'需求追问、候选路线、每日行程及历史版本',
    interview:'候选路线、每日行程及历史版本',
    routes:'路线确认结果、每日行程及历史版本',
    itinerary:'后续资源查询与运行记录',
  }[target]
  if (!window.confirm(`确定将进度回退到“${targetLabel}”吗？\n将清除${clearedRecords}等记录，此操作无法撤销。\n如果只想查看之前的内容，请取消后点击顶部步骤。`)) return
  const trip = current.value,epoch = selectionEpoch
  await action(async () => {
    const result = await api<{run_id:string | null}>(`/trips/${trip.id}/workflow/back`,{method:'POST',body:JSON.stringify({step:target,base_version:trip.version,updated_at:trip.updated_at})})
    if (epoch !== selectionEpoch || current.value?.id !== trip.id) return
    events?.close();events = null;connectionState.value = '';progress.value = [];message.value = '';selectedActivityIds.value = []
    guideFeedback.value = '';displayedSource.value = null;taxiResult.value = null;selectedVersion.value = 0;navigationStamp = ''
    if (target === 'requirements') guideKeywords.value = ''
    const data = await refreshCurrent(true,result.run_id || undefined)
    if (data) {
      navigationStamp = workflowStamp(data);moveToStep(target)
      if (result.run_id) listen(result.run_id,trip.id,epoch)
    }
  },'正在返回并清除后续步骤记录')
}
function navigateToStep(target: WorkflowTab) {
  if (isRunning.value) return
  moveToStep(target)
}
async function reopenRoute() {
  if (!current.value || !canEdit.value || candidateInputsChanged.value) return
  await backToStep('routes')
}
function toggleRequired(id: string) {
  if (id.startsWith('wish:')) return
  const ids = requirements.required_stop_ids || []
  requirements.required_stop_ids = ids.includes(id) ? ids.filter(value => value !== id) : [...ids,id]
  requirements.excluded_stop_ids = (requirements.excluded_stop_ids || []).filter(value => value !== id)
  error.value = '景点约束已修改，请重新生成路线后确认。'
}
async function reopenDecision(index: number) {
  if (!current.value || !canEdit.value || candidateInputsChanged.value) return
  const trip = current.value
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${trip.id}/decisions/reopen`,{method:'POST',body:JSON.stringify({history_index:index,base_version:trip.version,updated_at:trip.updated_at})})
    progress.value=[];await refreshCurrent();listen(result.run_id,trip.id,selectionEpoch)
  },'正在重新打开这项选择')
}
async function recheckCandidate(candidate: Candidate) {
  if (!current.value || !canEdit.value) return
  const trip = current.value
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${trip.id}/candidates/revise`,{method:'POST',body:JSON.stringify({candidate_id:candidate.id,dates:candidate.days.map(day => day.date),content:'保留景点和日期，补齐地图依据',recheck:true,base_version:trip.version,updated_at:trip.updated_at})})
    progress.value=[];await refreshCurrent();listen(result.run_id,trip.id,selectionEpoch)
  },'正在补齐地图路线依据')
}
async function repairCandidate(candidate: Candidate) {
  const affected = new Set((candidate.review?.findings || []).filter(finding => finding.severity === 'blocker').flatMap(finding => finding.dates))
  const dates = candidate.days.filter(day => !affected.size || affected.has(day.date)).map(day => day.date)
  await reviseFromChat({dates:dates.length ? dates : candidate.days.map(day => day.date),selected_stop_ids:[],
    content:'按当前审查发现修复时间冲突：预留60分钟午餐，满足开放窗口、指定开始时间和当天结束及返程期限。保留用户已明确保留的日期安排、必去和锁定项；无法同时满足的真实取舍应先提问，不要虚构耗时或擅自放宽约束。'})
}
watch(blockedCandidate,candidate => {
  if (!candidate || !current.value || !canEdit.value || candidateInputsChanged.value ||
      doc.value?.stage !== 'waiting' || doc.value.pending?.kind !== 'selection') return
  // Resume drafts left waiting by older versions once; new failures stay in the failed workflow.
  const key = `${current.value.id}:${candidate.id}:${candidate.draft_revision}:${candidate.plan_hash}`
  if (automaticRepairs.has(key)) return
  automaticRepairs.add(key)
  if (['pass','pass_with_warnings'].includes(candidate.review?.status || '') &&
      !candidate.review?.findings?.some(finding => finding.severity === 'blocker')) void recheckCandidate(candidate)
  else void repairCandidate(candidate)
},{flush:'post'})

function backSelection(event: Event) {
  const select = event.target as HTMLSelectElement
  if (select.value) void backToStep(select.value as WorkflowTab)
  select.value = ''
}
function syncWorkflow(data: Trip, force = false) {
  if (isHistory.value || current.value?.id !== data.id) return
  const stamp = workflowStamp(data)
  const target = resolveWorkflowTab(data)
  if (stamp === navigationStamp && (!force || tab.value === target)) return
  if (data.run?.status === 'failed') error.value = data.run.document.message || '任务中断，请查看错误或继续本次任务'
  moveToStep(target);navigationStamp = stamp
}
function productBusy(active: boolean, label = '正在查询或保存飞猪资源') {busy.value = active;actionLabel.value = active ? label : ''}
function productChanged() {void refreshCurrent().catch(cause => {error.value = String(cause)})}
function amapConfigSaved() {amapConfigRevision.value++;void refreshCapabilities()}
async function loadList() {trips.value = await api<TripSummary[]>('/trips')}
function loadForm(value: Requirements) {
  Object.assign(requirements,{...extraRequirements,...value,local_transport:value.local_transport ?? 'public',
    wish_places:(value.wish_places || []).map(item => ({city:item.city,name:item.name,date:item.date || null,duration_minutes:item.duration_minutes ?? null}))})
  childAgeText.value = (value.children_ages || []).join('、')
  cityText.value = value.cities.join('、')
  wishCity.value = value.cities[0] || ''
  wishName.value = ''; wishDate.value = ''; wishDuration.value = null; wishError.value = ''
  Object.keys(hotelAreas).forEach(city => delete hotelAreas[city])
  Object.assign(hotelAreas,value.hotel_areas || {})
}
function formData(): Requirements {
  return {...requirements,cities:cityText.value.split(/[、,，\n]/).map(s => s.trim()).filter(Boolean),
    origin:requirements.origin?.trim() || null,start_date:requirements.start_date || null,end_date:requirements.end_date || null,
    people:requirements.people || null,rooms:requirements.rooms || null,budget:requirements.budget || null,
    children:Number(requirements.children || 0),children_ages:requirements.children ? childAgeText.value.split(/[、,，\s]/).filter(Boolean).map(Number) : [],
    earliest_departure:requirements.earliest_departure || null,latest_arrival:requirements.latest_arrival || null,latest_return:requirements.latest_return || null,
    max_transport_hours:requirements.max_transport_hours || null,max_walk_per_day:requirements.max_walk_per_day ?? 8000,
    hotel_areas:Object.fromEntries(destinationCities.value.filter(city => hotelAreas[city]?.trim()).sort((left,right) => left.localeCompare(right)).map(city => [city,hotelAreas[city].trim()])),
    wish_places:(requirements.wish_places || []).filter(item => destinationCities.value.includes(item.city) && item.name.trim()).slice(0,20).map(item => ({
      city:item.city,name:item.name.trim(),
      date:item.date && (!requirements.start_date || item.date >= requirements.start_date) && (!requirements.end_date || item.date <= requirements.end_date) ? item.date : null,
      duration_minutes:item.duration_minutes ?? null,
    }))}
}
function inputSignature() {
  return JSON.stringify([formData(),[...selectedGuideIds.value].sort()])
}
function syncInputs(trip: Trip) {
  loadForm(trip.document.requirements)
  selectedGuideIds.value = (trip.document.selected_source_ids || []).filter(id => trip.sources.some(source => source.id === id))
  inputBaseline = inputSignature()
}
async function openTrip(id: string) {
  const epoch = ++selectionEpoch
  events?.close(); events = null;connectionState.value = '';navigationStamp = ''
  const data = await api<Trip>('/trips/' + id)
  if (epoch !== selectionEpoch) return
  current.value = data; selectedVersion.value = 0; versionDocuments.value = {}; progress.value = []; displayedSource.value = null; taxiResult.value = null
  guideCity.value = data.document.requirements.cities[0] || ''; guideKeywords.value = ''; guideFeedback.value = ''
  syncInputs(data)
  localStorage.setItem('travel.current',id)
  const history = await api<VersionSummary[]>(`/trips/${id}/versions?include_documents=false`)
  if (epoch !== selectionEpoch) return
  versions.value = history
  syncWorkflow(data,true)
  if (data.active_run && data.run?.status === 'running') listen(data.active_run,id,epoch)
}
async function refreshCurrent(loadInputs = false, expectedRunId?: string, refreshHistory = false): Promise<Trip | null> {
  if (!current.value) return null
  const id = current.value.id, epoch = selectionEpoch, refresh = ++refreshEpoch
  const previousVersion = current.value.version
  const data = await api<Trip>('/trips/' + id)
  const history = loadInputs || refreshHistory || data.version !== previousVersion || (expectedRunId && data.run?.status !== 'running')
    ? await api<VersionSummary[]>(`/trips/${id}/versions?include_documents=false`) : null
  if (epoch !== selectionEpoch || refresh !== refreshEpoch || (expectedRunId && data.active_run !== expectedRunId)) return null
  const inputsUnchanged = inputSignature() === inputBaseline
  current.value = data
  if (loadInputs || (data.active_run && data.run?.status !== 'running' && inputsUnchanged)) syncInputs(data)
  if (history) {
    versions.value = history
    const retained = new Set(history.map(item => item.version))
    Object.keys(versionDocuments.value).forEach(key => {if (!retained.has(Number(key))) delete versionDocuments.value[Number(key)]})
    if (selectedVersion.value && !retained.has(selectedVersion.value)) selectedVersion.value = 0
  }
  trips.value = trips.value.map(trip => trip.id === data.id ? {...trip,version:data.version,updated_at:data.updated_at,stage:data.document.stage,active_run:data.active_run} : trip)
    .sort((left,right) => right.updated_at.localeCompare(left.updated_at))
  if (epoch !== selectionEpoch || refresh !== refreshEpoch) return null
  return data
}
async function selectVersion(event: Event) {
  const select = event.target as HTMLSelectElement, version = Number(select.value)
  select.value = String(selectedVersion.value)
  if (!version) {selectedVersion.value = 0;return}
  if (!current.value || busy.value) return
  const tripId = current.value.id,epoch = selectionEpoch
  await action(async () => {
    if (!versionDocuments.value[version]) {
      const saved = await api<Version>(`/trips/${tripId}/versions/${version}`)
      if (epoch !== selectionEpoch || current.value?.id !== tripId) return
      versionDocuments.value[version] = saved.document
    }
    selectedVersion.value = version
  },'正在读取已保存行程')
}
function listen(runId: string, tripId: string, epoch: number, navigate = true) {
  if (!current.value || current.value.id !== tripId || epoch !== selectionEpoch || current.value.active_run !== runId) return
  syncWorkflow(current.value,navigate)
  if (current.value.run?.status !== 'running') {
    if (current.value.run?.status === 'failed') error.value = current.value.run.document.message || '任务中断，请查看错误或继续本次任务'
    return
  }
  events?.close()
  const channel = new EventSource(`/api/runs/${runId}/events`)
  events = channel
  let terminalRefresh: Promise<Trip | null> | null = null
  const valid = () => epoch === selectionEpoch && current.value?.id === tripId && events === channel
  const finish = () => {
    if (!valid()) return
    if (!terminalRefresh) terminalRefresh = refreshCurrent(false,runId).then(data => {
      if (!data || !valid()) return null
      syncWorkflow(data)
      if (data.run?.status !== 'running') {channel.close();events = null;connectionState.value = ''}
      return data
    }).catch(cause => {if (valid()) error.value = String(cause);return null})
    return terminalRefresh
  }
  channel.onmessage = event => {
    const trip = current.value
    if (!valid() || !trip) return
    const item = JSON.parse(event.data) as ProgressEvent
    if (!progress.value.some(p => p.id === item.id)) progress.value.push(item)
    if (item.kind === 'progress' && item.stage) {
      trip.document.stage = item.stage
      if (item.stage !== 'queued') trip.document.progress_stage = item.stage
    }
    if (item.kind === 'error' && item.stage) trip.document.progress_stage = item.stage
    if (item.kind === 'error') error.value = item.message
    if (['waiting','completed','error'].includes(item.kind)) void finish()
  }
  channel.addEventListener('end',() => {
    channel.close()
    void finish()
  })
  channel.onopen = () => {if (valid()) {const reconnecting = !!connectionState.value;connectionState.value = '';if (reconnecting) void reconcileRun()}}
  channel.onerror = () => {if (valid()) {connectionState.value = '正在恢复进度连接';void reconcileRun()}}
}
function reconcileRun(): Promise<void> {
  if (reconciliation) return reconciliation
  if (!current.value?.active_run || busy.value) return Promise.resolve()
  const runId = current.value.active_run,epoch = selectionEpoch
  reconciliation = refreshCurrent(false,runId).then(data => {
    if (!data || epoch !== selectionEpoch) return
    syncWorkflow(data)
    if (data.run?.status !== 'running') {events?.close();events = null;connectionState.value = ''}
    else if (!events || events.readyState === EventSource.CLOSED) listen(runId,data.id,epoch,false)
  }).catch(() => {if (epoch === selectionEpoch) connectionState.value = '连接暂时中断，正在恢复状态'}).finally(() => {reconciliation = null})
  return reconciliation
}
watch(selectedVersion,value => {if (!value && current.value) syncWorkflow(current.value)})

async function saveRequirements() {
  if (!current.value || !canEdit.value) return
  const trip = current.value,epoch = selectionEpoch
  await action(async () => {
    await api<Trip>(`/trips/${trip.id}/requirements`,{method:'PATCH',body:JSON.stringify({requirements:formData(),base_version:trip.version,updated_at:trip.updated_at})})
    if (epoch !== selectionEpoch || current.value?.id !== trip.id) return
    events?.close();events = null;progress.value = []
    const data = await refreshCurrent(true)
    if (data) {guideCity.value = data.document.requirements.cities[0] || '';navigationStamp = workflowStamp(data);moveToStep('guides')}
  },'正在保存旅行需求')
}
async function openResourceSearch() {
  if (busy.value) return
  await action(async () => {
    if (!current.value) {
      const data = await api<Trip>('/trips',{method:'POST',body:JSON.stringify({title:'酒店与门票查询'})})
      await loadList();await openTrip(data.id)
    }
    moveToStep('resources')
  },'正在打开酒店与门票查询')
}
async function createTrip() {
  await action(async () => {
    const data = await api<Trip>('/trips',{method:'POST',body:JSON.stringify({title:tripTitle.value.trim() || '新的旅行'})})
    dialog.value = null; tripTitle.value = ''; await loadList(); await openTrip(data.id)
  })
}
async function deleteTrip(trip: TripSummary) {
  if (busy.value || tripRunning(trip)) return
  if (!window.confirm(`确定删除“${trip.title}”吗？\n这会永久删除该旅行及其攻略、回答记录和历史行程，无法撤销。`)) return
  await action(async () => {
    const base = current.value?.id === trip.id ? current.value.version : trip.version
    await api(`/trips/${trip.id}?base_version=${base}`,{method:'DELETE'})
    trips.value = trips.value.filter(item => item.id !== trip.id)
    if (localStorage.getItem('travel.current') === trip.id) localStorage.removeItem('travel.current')
    if (current.value?.id === trip.id) {
      selectionEpoch++; events?.close(); events = null
      current.value = null; versions.value = []; selectedVersion.value = 0; progress.value = []
      displayedSource.value = null; selectedGuideIds.value = []; message.value = ''; dialog.value = null
      tab.value = 'requirements'
    }
  })
}
async function run(retry = false, fromMessage = false, resumeOnly = false) {
  if (!current.value) return
  if (resumeOnly && candidateInputsChanged.value) {
    error.value = '需求或参考攻略已修改。请恢复原输入再继续，或重新开始规划。'; return
  }
  if (!selectedGuideIds.value.length) {
    tab.value = 'guides'; error.value = '请先勾选一篇或多篇参考攻略，再生成候选路线'; return
  }
  const id = current.value.id
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${id}/messages`,{method:'POST',body:JSON.stringify({
      content:fromMessage ? message.value : '',requirements:fromMessage ? {} : formData(),base_version:current.value!.version,updated_at:current.value!.updated_at,retry,resume:resumeOnly,
      selected_source_ids:selectedGuideIds.value,
    })})
    message.value = ''; progress.value = []; const data = await refreshCurrent(true,result.run_id);if (data) {syncWorkflow(data,true);listen(result.run_id,id,selectionEpoch)}
  })
}
async function refreshQuotes(resourceIds?: string[]) {
  if (!current.value || !canEdit.value || candidateInputsChanged.value || doc.value?.pending) return
  const id = current.value.id
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${id}/messages`,{method:'POST',body:JSON.stringify({
      base_version:current.value!.version,updated_at:current.value!.updated_at,refresh_resources:true,selected_source_ids:selectedGuideIds.value,
      ...(resourceIds ? {resource_ids:resourceIds} : {}),
    })})
    progress.value = []; const data = await refreshCurrent(false,result.run_id);if (data) {syncWorkflow(data,true);listen(result.run_id,id,selectionEpoch)}
  })
}
async function resourceSelect(entryId: string, optionId: string) {
  if (!current.value || !canEdit.value || candidateInputsChanged.value || doc.value?.pending || doc.value?.itinerary_outdated) return
  const trip = current.value
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${trip.id}/resources/select`,{method:'POST',body:JSON.stringify({entry_id:entryId,option_id:optionId,base_version:trip.version,updated_at:trip.updated_at})})
    progress.value = []; taxiResult.value = null; await refreshCurrent(); listen(result.run_id,trip.id,selectionEpoch)
  })
}
async function productRun(runId: string, tripId: string) {
  if (!current.value || current.value.id !== tripId) return
  const id = current.value.id
  progress.value = []; await refreshCurrent(); listen(runId,id,selectionEpoch)
}
async function exportMap() {
  if (!current.value || !canEdit.value) return
  const trip = current.value
  await action(async () => {
    await api<MapResult>(`/trips/${trip.id}/maps`,{method:'POST',body:JSON.stringify({base_version:trip.version,updated_at:trip.updated_at})})
    await refreshCurrent()
  },'正在生成手机高德导出入口')
}
async function openTaxi(date: string, activityId: string) {
  if (!current.value || !canEdit.value) return
  const trip = current.value
  await action(async () => {
    taxiResult.value = await api<MapResult>(`/trips/${trip.id}/taxi`,{method:'POST',body:JSON.stringify({date,activity_id:activityId,base_version:trip.version,updated_at:trip.updated_at})})
  })
}
function guideSelected(id: string) {
  return (isHistory.value ? doc.value?.selected_source_ids || [] : selectedGuideIds.value).includes(id)
}
function toggleGuide(id: string) {
  if (!canEdit.value) return
  requirements.required_stop_ids = (requirements.required_stop_ids || []).filter(value => !value.startsWith(id + ':'))
  requirements.excluded_stop_ids = (requirements.excluded_stop_ids || []).filter(value => !value.startsWith(id + ':'))
  if (selectedGuideIds.value.includes(id)) selectedGuideIds.value = selectedGuideIds.value.filter(value => value !== id)
  else if (selectedGuideIds.value.length < 20) selectedGuideIds.value = [...selectedGuideIds.value,id]
  else error.value = '最多选择 20 篇参考攻略'
}
function reviewReady(candidate: Candidate) {
  const review = candidate.review
  const expiry = new Date(review?.expires_at || '').getTime()
  return !!review && review.current !== false && ['pass','pass_with_warnings'].includes(review.status) &&
    Number.isFinite(expiry) && expiry > Date.now()
}
async function choose(candidate: Candidate) {
  if (!current.value) return
  if (!reviewReady(candidate)) {error.value = '这份路线的独立审查未完成或已失效，请重新审查'; return}
  if (candidateInputsChanged.value || !selectedGuideIds.value.length) {
    tab.value = 'guides'; error.value = '参考攻略或旅行需求已变化，请重新生成候选路线'; return
  }
  const id = current.value.id
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${id}/selection`,{method:'POST',body:JSON.stringify({candidate_id:candidate.id,base_version:current.value!.version,updated_at:current.value!.updated_at,draft_revision:candidate.draft_revision,plan_hash:candidate.plan_hash})})
    progress.value = []; await refreshCurrent(); listen(result.run_id,id,selectionEpoch)
  })
}
async function answerInterview(response: InterviewResponse) {
  if (!current.value || !canEdit.value || candidateInputsChanged.value) return
  const id = current.value.id
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${id}/messages`,{method:'POST',body:JSON.stringify({base_version:current.value!.version,updated_at:current.value!.updated_at,interview_response:response})})
    progress.value = []; await refreshCurrent(); listen(result.run_id,id,selectionEpoch)
  })
}
function editCandidate(candidate: Candidate, date?: string) {
  if (date) selectedActivityIds.value = (candidate.draft?.days.find(day => day.date === date)?.activities || []).filter(activity => activity.kind === 'visit').map(activity => activity.id)
  void routeChat.value?.editDates(date ? [date] : candidate.days.map(day => day.date))
}
function selectActivity(id: string) {
  selectedActivityIds.value = selectedActivityIds.value.includes(id) ? selectedActivityIds.value.filter(value => value !== id) : [...selectedActivityIds.value,id]
}
async function reviseFromChat(request: {dates:string[];content:string;selected_stop_ids:string[]}) {
  const trip = current.value, candidate = visibleCandidates.value[0] || blockedCandidate.value
  if (!trip || !candidate || !canEdit.value || candidateInputsChanged.value || doc.value?.pending?.kind !== 'selection') return
  await action(async () => {
    const result = await api<{run_id:string}>(`/trips/${trip.id}/candidates/revise`,{method:'POST',body:JSON.stringify({candidate_id:candidate.id,...request,base_version:trip.version,updated_at:trip.updated_at})})
    progress.value = [];await refreshCurrent();listen(result.run_id,trip.id,selectionEpoch)
  },'正在根据你的要求调整行程')
}
async function toggleLock(id: string) {
  if (!current.value) return
  await action(async () => {
    await api(`/trips/${current.value!.id}/locks`,{method:'PATCH',body:JSON.stringify({item_id:id,locked:!current.value!.document.locks.includes(id),base_version:current.value!.version})})
    await refreshCurrent()
  })
}
async function saveItinerary() {
  if (!current.value || !canSaveItinerary.value) return
  const trip = current.value
  await action(async () => {
    await api(`/trips/${trip.id}/itinerary/save`,{method:'POST',body:JSON.stringify({base_version:trip.version,updated_at:trip.updated_at})})
    await refreshCurrent()
  })
}
async function deleteItinerary() {
  if (!current.value || !itinerary.value || isRunning.value) return
  const trip = current.value, version = selectedVersion.value || savedItineraryVersion.value
  const target = version ? `已保存的行程 v${version}` : '当前未保存的行程草稿'
  if (!window.confirm(`确定删除${target}吗？\n删除后无法撤销，旅行需求和参考攻略会保留。`)) return
  await action(async () => {
    const params = new URLSearchParams({base_version:String(trip.version),updated_at:trip.updated_at})
    if (version) params.set('version',String(version))
    await api(`/trips/${trip.id}/itinerary?${params}`,{method:'DELETE'})
    selectedVersion.value = 0
    await refreshCurrent(false,undefined,true)
    if (!current.value?.document.itinerary) progress.value = []
  })
}
function newTrip() {
  tripTitle.value = ''; dialog.value = 'create'
}
function goHome() {
  if (busy.value) return
  selectionEpoch++; current.value = null; events?.close(); events = null
}
async function openGuideSearch() {
  if (busy.value) return
  await action(async () => {
    if (!current.value) {
      const data = await api<Trip>('/trips',{method:'POST',body:JSON.stringify({title:'旅行攻略收藏'})})
      await loadList(); await openTrip(data.id)
    }
    selectedVersion.value = 0; tab.value = 'guides'
  })
  await nextTick(); guideSearchInput.value?.focus()
}
async function searchGuides() {
  if (!current.value || !canEdit.value) return
  const id = current.value.id
  guideFeedback.value = ''
  await action(async () => {
    const result = await api<{count:number;message:string}>(`/trips/${id}/sources/search`,{
      method:'POST',body:JSON.stringify({city:guideCity.value,keywords:guideKeywords.value,base_version:current.value!.version}),
    })
    guideFeedback.value = result.message
    await refreshCurrent(); tab.value = 'guides'
  })
}
async function refreshCapabilities(configuration?: ModelConfiguration) {
  const epoch = ++capabilitiesEpoch
  if (configuration) capabilities.value = {...capabilities.value,
    model_configured:configuration.configured,model_active:configuration.configured,
    model_name:[configuration.provider_name,configuration.model_name || configuration.model].filter(Boolean).join(' / ')}
  try {
    const value = await api<Capabilities>('/capabilities',{cache:'no-store'})
    if (epoch === capabilitiesEpoch) capabilities.value = value
  }
  catch (e) { error.value = e instanceof Error ? e.message : '无法刷新接入状态' }
}
function importSource() {
  sourceTitle.value = ''; sourceText.value = ''; sourceUrl.value = ''; sourceCity.value = ''; sourceFiles.value = []
  dialog.value = 'source'
}
function filesChanged(event: Event) {sourceFiles.value = Array.from((event.target as HTMLInputElement).files || [])}
async function saveSource() {
  if (!current.value) return
  await action(async () => {
    const body = new FormData()
    body.set('title',sourceTitle.value || '导入的攻略'); body.set('url',sourceUrl.value); body.set('text',sourceText.value)
    body.set('city_hint',sourceCity.value); body.set('base_version',String(current.value!.version))
    sourceFiles.value.forEach(file => body.append('files',file))
    await api(`/trips/${current.value!.id}/sources`,{method:'POST',body})
    dialog.value = null; await refreshCurrent(); tab.value = 'guides'
  })
}
function openSettings(kind: SettingsKind) {
  if (kind === 'model') modelSettingsOpen.value = true
  else if (kind === 'crawler') crawlerSettingsOpen.value = true
  else if (kind === 'flyai') flyaiSettingsOpen.value = true
  else amapSettingsOpen.value = true
}
function showSource(id?: string) {if (id) displayedSource.value = sourceMap.value.get(id) || null}
function refreshOnFocus() {void refreshCapabilities();void reconcileRun()}
onMounted(() => {window.addEventListener('focus',refreshOnFocus);void action(async () => {
  await loadList(); capabilities.value = await api<Capabilities>('/capabilities',{cache:'no-store'})
  const id = localStorage.getItem('travel.current')
  if (id && trips.value.some(t => t.id === id)) await openTrip(id)
},'正在读取旅行工作台');runPoll = window.setInterval(() => {if (current.value?.run?.status === 'running' || connectionState.value) void reconcileRun()},12000)})
onUnmounted(() => {events?.close();window.clearInterval(runPoll);window.removeEventListener('focus',refreshOnFocus)})
</script>

<template>
  <div class="app-shell">
    <AppSidebar
      :trips="trips"
      :current-id="current?.id"
      :busy="busy"
      :labels="labels"
      :running="tripRunning"
      @home="goHome"
      @create="newTrip"
      @search="openGuideSearch"
      @resources="openResourceSearch"
      @open="id => action(() => openTrip(id))"
      @remove="deleteTrip"
    />

    <div class="workspace">
      <header class="topbar">
        <div class="topbar-title">
          <span>旅行工作台</span>
          <strong v-if="current">{{ current.title }}</strong>
        </div>
        <div class="topbar-actions">
          <span class="local-label">本机个人使用</span>
          <SettingsMenu :capabilities="capabilities" @open="openSettings" />
        </div>
      </header>

      <div v-if="error" class="error-banner" role="alert">
        <span>{{ error }}</span>
        <button class="text-button" @click="error = ''">关闭</button>
      </div>
      <LoadingState
        v-if="isRunning"
        class="workspace-loading"
        :title="current?.run?.status === 'running' ? (labels[current.document.progress_stage || current.document.stage] || '正在规划旅行') : actionLabel || '正在读取数据'"
        :detail="connectionState || (current?.run?.status === 'running' ? progress[progress.length-1]?.message || '完成后会自动显示下一步。' : undefined)"
        compact
      />

      <WelcomeView v-if="!current" :busy="busy" @search="openGuideSearch" @create="newTrip" />

      <template v-else>
        <section class="trip-header">
          <div class="trip-header-main">
            <div class="city-chain">
              <span>{{ current.document.requirements.origin || '出发地待填写' }}</span>
              <template v-for="city in current.document.requirements.cities" :key="city">
                <i aria-hidden="true">→</i><span>{{ city }}</span>
              </template>
            </div>
            <h1>{{ current.title }}</h1>
            <p class="trip-meta">
              {{ current.document.requirements.start_date || '开始日期待填写' }} 至 {{ current.document.requirements.end_date || '结束日期待填写' }}
              <span v-if="current.document.requirements.people"> · {{ current.document.requirements.people }} 人</span>
            </p>
          </div>
          <div class="header-actions">
            <span class="stage-tag">{{ labels[current.document.stage] || current.document.stage }}</span>
            <label v-if="versions.length" class="version-select">
              行程版本
              <select :value="selectedVersion" :disabled="busy" @change="selectVersion">
                <option :value="0">{{ current.document.itinerary ? savedItineraryVersion !== null ? `当前行程 v${savedItineraryVersion}` : '当前草稿（未保存）' : '当前暂无行程' }}</option>
                <option v-for="version in versions" :key="version.version" :value="version.version">已保存 v{{ version.version }}</option>
              </select>
            </label>
          </div>
        </section>

        <div v-if="isHistory" class="info-banner">正在查看已保存版本，可在每日行程中删除。切换到当前行程后可以修改。</div>
        <div v-if="current.document.pending" class="info-banner pending-banner">
          <strong>{{ current.document.pending.message }}</strong>
          <span class="banner-actions">
            <button v-if="current.document.pending.kind === 'requirements'" class="primary small" @click="navigateToStep('requirements')">填写信息</button>
            <button v-if="current.document.pending.kind === 'guides'" class="primary small" @click="navigateToStep('guides')">选择参考攻略</button>
            <button v-if="interviewPending" class="primary small" @click="navigateToStep('interview')">{{ planningDecision ? '处理路线取舍' : interviewPending.kind === 'interview' ? '回答本轮问题' : '确认需求总结' }}</button>
            <button v-if="current.document.pending.kind === 'sources'" class="primary small" :disabled="!canEdit" @click="importSource">导入攻略</button>
          </span>
        </div>

        <WorkflowSteps :steps="tabs" :active="tab" :progress-step="workflowStep" :disabled="isRunning" :guide-count="visibleSources.length" @navigate="navigateToStep" />

        <div class="content-layout">
          <div class="main-panel">
            <div class="step-navigation">
              <button v-if="previousStep" class="text-button" :disabled="isRunning" @click="navigateToStep(previousStep.key)">← 返回{{ previousStep.label }}</button>
              <button v-else class="text-button" :disabled="busy" @click="goHome">← 返回旅行列表</button>
              <label v-if="backSteps.length">
                回到之前的步骤
                <select :disabled="isRunning" value="" @change="backSelection">
                  <option value="">选择要返回的步骤</option>
                  <option v-for="step in backSteps" :key="step.key" :value="step.key">{{ step.label }}</option>
                </select>
              </label>
            </div>

            <!-- 旅行需求 -->
            <section v-if="tab === 'requirements'" class="panel-body">
              <div class="section-intro">
                <h2>这趟旅行，你想怎么过？</h2>
                <p>填写基本需求并选择攻略后，大模型会分轮追问你的偏好，确认需求后再生成候选路线。</p>
              </div>
              <form class="requirements-form" @submit.prevent="saveRequirements">
                <fieldset :disabled="!canEdit">
                  <div class="form-section">
                    <h3>行程与人数</h3>
                    <div class="form-grid">
                      <label>出发城市<input v-model="requirements.origin" placeholder="例如：北京" maxlength="80"></label>
                      <label>目的地城市，按旅行顺序<input v-model="cityText" placeholder="例如：杭州、苏州" maxlength="300"></label>
                      <label>开始日期<input v-model="requirements.start_date" type="date"></label>
                      <label>结束日期<input v-model="requirements.end_date" type="date"></label>
                      <label>总人数，含儿童<input v-model.number="requirements.people" type="number" min="1" max="20"></label>
                      <label>其中儿童人数<input v-model.number="requirements.children" type="number" min="0" max="19"></label>
                      <label v-if="requirements.children">儿童年龄，可选<input v-model="childAgeText" placeholder="例如：6、10"></label>
                      <label>房间数<input v-model.number="requirements.rooms" type="number" min="1" max="10"></label>
                      <label>整趟预算（元）<input v-model.number="requirements.budget" type="number" min="1" placeholder="所有人的总预算"></label>
                    </div>
                  </div>

                  <div class="form-section">
                    <h3>出行方式</h3>
                    <div class="form-grid">
                      <label>跨城市交通
                        <select v-model="requirements.transport">
                          <option value="any">铁路、飞机都可以</option>
                          <option value="train">只查铁路</option>
                          <option value="flight">只查飞机</option>
                        </select>
                      </label>
                      <label>市内出行
                        <select v-model="requirements.local_transport">
                          <option value="public">步行＋公交 / 地铁</option>
                          <option value="walking">只步行</option>
                          <option value="driving">自驾</option>
                          <option value="bicycling">骑行</option>
                        </select>
                      </label>
                      <label>床型筛选
                        <select v-model="requirements.bed_type"><option>双床房</option><option>大床房</option><option>多床房</option></select>
                      </label>
                      <label>每日步行上限（米）
                        <input v-model.number="requirements.max_walk_per_day" type="number" min="0" max="50000" @input="requirements.walking_limit_exempt_dates = []">
                        <small v-if="requirements.walking_limit_exempt_dates?.length">
                          已按你的指令豁免：{{ requirements.walking_limit_exempt_dates.join('、') }}。
                          <button type="button" class="text-button" @click="requirements.walking_limit_exempt_dates = []">恢复全部日期的上限</button>
                        </small>
                      </label>
                    </div>
                    <div class="check-row">
                      <label class="checkbox-label"><input v-model="requirements.allow_taxi" type="checkbox">允许打车与打车入口</label>
                      <label class="checkbox-label"><input v-model="requirements.return_to_origin" type="checkbox" :disabled="requirements.self_arranged_round_trip">结束后回到出发地</label>
                      <label class="checkbox-label"><input v-model="requirements.self_arranged_round_trip" type="checkbox">往返交通自行安排（只规划目的地行程）</label>
                    </div>
                  </div>

                  <details class="travel-limits">
                    <summary>交通时段与住宿筛选<span>可选</span></summary>
                    <div class="form-grid">
                      <label>出发城市的具体起终点<input v-model="requirements.origin_address" placeholder="用于计算车站或机场接驳" maxlength="200"></label>
                      <label>最早出发时间<input v-model="requirements.earliest_departure" type="time"></label>
                      <label>最晚到达时间<input v-model="requirements.latest_arrival" type="time"></label>
                      <label>返程最晚到达时间<input v-model="requirements.latest_return" type="time"></label>
                      <label>城际最长耗时（小时）<input v-model.number="requirements.max_transport_hours" type="number" min="0.5" max="72" step="0.5"></label>
                      <label>飞机舱位<input v-model="requirements.flight_cabin" placeholder="例如：经济舱" maxlength="40"></label>
                      <label>铁路坐席<input v-model="requirements.train_seat" placeholder="例如：二等座" maxlength="40"></label>
                      <label>住宿类型
                        <select v-model="requirements.hotel_type"><option value="">不限</option><option>酒店</option><option>民宿</option><option>客栈</option></select>
                      </label>
                      <label>酒店星级<input v-model="requirements.hotel_stars" placeholder="例如：4,5"></label>
                      <label class="checkbox-label"><input v-model="requirements.direct_only" type="checkbox">只查直达班次</label>
                    </div>
                    <p class="subtle">儿童价、多人多房库存与具体房型需在预订页确认。时间限制适用于班次，提供具体起终点后也检查出发接驳。</p>
                  </details>

                  <details class="travel-limits" open>
                    <summary>住宿区域<span>留空自动推荐</span></summary>
                    <p class="subtle">留空时，根据每日活动位置推荐住宿区域。可以为每座城市指定区域、路段或附近地标。</p>
                    <div class="form-grid">
                      <label v-for="city in destinationCities" :key="city">{{ city }}住宿区域
                        <input v-model="hotelAreas[city]" maxlength="150" placeholder="留空自动推荐，例如：湖滨路附近">
                      </label>
                    </div>
                  </details>

                  <label class="wide-label">兴趣与旅行节奏
                    <textarea v-model="requirements.preferences" placeholder="例如：不打车、少走路、喜欢园林和博物馆、不想太赶" rows="3" maxlength="2000"></textarea>
                  </label>

                  <div v-if="activeConstraints.length" class="wide-label stop-marks">
                    <strong>已识别的条件</strong>
                    <p v-for="item in activeConstraints" :key="item.id">
                      {{ constraintText(item) }} <small class="subtle">「{{ item.quote }}」</small>
                      <button v-if="item.kind !== 'fewer_walking' || item.meters" class="text-button" type="button" @click="toggleConstraintStrength(item.id)">{{ item.strength === 'hard' ? '改为尽量满足' : '改为必须满足' }}</button>
                      <button class="text-button" type="button" @click="removeConstraint(item.id)">删除</button>
                    </p>
                    <small v-if="preferencesEdited" class="subtle">修改上面的文字后，会在下次生成路线时重新识别。</small>
                    <small v-else-if="constraintsEdited" class="subtle">条件的改动在保存需求后生效。</small>
                  </div>

                  <details class="travel-limits" open>
                    <summary>想去的地点<span>可选，最多 20 项</span></summary>
                    <p class="subtle">不在攻略里也可以填写，规划时一定会安排；餐厅、小吃等会安排进合适的一餐；高德匹配到多个同名地点时会请你选择。</p>
                    <div class="form-grid">
                      <label>所属城市
                        <select v-model="wishCity" :disabled="!destinationCities.length">
                          <option v-if="!destinationCities.length" value="">请先填写目的地城市</option>
                          <option v-for="city in destinationCities" :key="city" :value="city">{{ city }}</option>
                        </select>
                      </label>
                      <label>地点名称<input v-model="wishName" maxlength="100" placeholder="例如：灵隐寺" @keydown.enter.prevent="addWishPlace"></label>
                      <label>指定日期（可选）<input v-model="wishDate" type="date" :min="requirements.start_date || undefined" :max="requirements.end_date || undefined"></label>
                      <label>游玩时长（分钟，可选）<input v-model.number="wishDuration" type="number" min="10" max="720" placeholder="10–720" @keydown.enter.prevent="addWishPlace"></label>
                    </div>
                    <p v-if="wishError" class="pending-note" role="alert">{{ wishError }}</p>
                    <div class="check-row">
                      <button class="secondary small" type="button" :disabled="!destinationCities.length || (requirements.wish_places?.length || 0) >= 20" @click="addWishPlace">添加</button>
                    </div>
                    <div v-if="requirements.wish_places?.length" class="wide-label stop-marks">
                      <p v-for="(item, index) in requirements.wish_places" :key="item.city + ':' + item.name">
                        <strong>{{ item.city }}</strong> · {{ item.name }}
                        <small v-if="item.date" class="subtle"> · {{ item.date }}</small>
                        <small v-if="item.duration_minutes" class="subtle"> · {{ item.duration_minutes }} 分钟</small>
                        <small v-if="!destinationCities.includes(item.city)" class="subtle"> · 不在当前目的地，不会保存</small>
                        <small v-else-if="item.date && !isWishDateValid(item.date)" class="subtle"> · 日期超出旅行范围，将不指定日期</small>
                        <button class="text-button" type="button" @click="removeWishPlace(index)">删除</button>
                      </p>
                    </div>
                  </details>

                  <div v-if="requiredGuideStopIds.length" class="wide-label stop-marks">
                    <strong>必去景点标记</strong>
                    <p v-for="id in requiredGuideStopIds" :key="id">
                      {{ sourceStops.get(id)?.name || '攻略依据已变化的景点' }}
                      <button class="text-button" type="button" @click="toggleRequired(id)">移除必去标记</button>
                    </p>
                  </div>
                  <div v-if="requirements.excluded_stop_ids?.length" class="wide-label stop-marks">
                    <strong>排除的景点</strong>
                    <p v-for="id in requirements.excluded_stop_ids" :key="id">
                      {{ sourceStops.get(id)?.name || '攻略依据已变化的景点' }}
                      <button class="text-button" type="button" @click="requirements.excluded_stop_ids = (requirements.excluded_stop_ids || []).filter(value => value !== id)">恢复为可选</button>
                    </p>
                  </div>

                  <div class="action-bar">
                    <span class="action-bar-note">保存后进入下一步：选择参考攻略</span>
                    <button class="primary" type="submit">
                      <span v-if="busy" class="loading-spinner" aria-hidden="true"></span>{{ busy ? '正在保存…' : '保存需求，下一步选攻略' }}
                    </button>
                  </div>
                </fieldset>
              </form>
            </section>

            <!-- 参考攻略 -->
            <section v-if="tab === 'guides'" class="panel-body">
              <div class="section-intro with-action">
                <div>
                  <h2>搜索旅行攻略</h2>
                  <p>勾选想参考的攻略后，先让大模型结合攻略追问你的旅行偏好，再生成候选路线。</p>
                </div>
                <button class="secondary" :disabled="!canEdit" @click="importSource">＋ 导入攻略</button>
              </div>
              <form class="guide-search-form" @submit.prevent="searchGuides">
                <fieldset :disabled="!canEdit">
                  <div class="guide-search-fields">
                    <label>目的地城市<input ref="guideSearchInput" v-model="guideCity" placeholder="例如：杭州" maxlength="80"></label>
                    <label>攻略关键词<input v-model="guideKeywords" type="search" placeholder="例如：三天两晚、园林、少走路" maxlength="200"></label>
                    <button class="primary" type="submit">{{ busy ? '正在处理…' : '搜索攻略' }}</button>
                  </div>
                </fieldset>
                <p class="subtle">只需填写城市或关键词，无需先填写日期、人数和预算。搜索结果会加入当前旅行。</p>
                <p v-if="!capabilities.crawler_enabled" class="pending-note">
                  采集器尚未启动。<button type="button" class="text-button" @click="crawlerSettingsOpen = true">打开小红书采集器</button>，在这里安装、登录或启动；也可以先导入攻略。
                </p>
                <p v-if="guideFeedback" class="guide-search-feedback" role="status">{{ guideFeedback }}</p>
              </form>
              <div v-if="!visibleSources.length" class="empty-panel">
                <span class="empty-symbol" aria-hidden="true">⌁</span>
                <h3>还没有参考攻略</h3>
                <p>在上方输入城市和关键词搜索，或导入喜欢的攻略文字与图片。</p>
                <button class="secondary" :disabled="!canEdit" @click="importSource">导入第一份攻略</button>
              </div>
              <div class="guide-list">
                <GuideCard
                  v-for="source in visibleSources"
                  :key="source.id"
                  :source="source"
                  :selected="guideSelected(source.id)"
                  :disabled="!canEdit"
                  @toggle="toggleGuide(source.id)"
                  @show="displayedSource = source"
                />
              </div>
              <div v-if="visibleSources.length" class="action-bar">
                <div class="action-bar-note">
                  <p class="guide-selection-summary">
                    已选 <strong>{{ isHistory ? (doc?.selected_source_ids?.length || 0) : selectedGuideIds.length }}</strong> 篇参考攻略
                    <span v-if="!isHistory && tripDayCount"> · {{ requirements.start_date }} 至 {{ requirements.end_date }}，共 {{ tripDayCount }} 天（含首尾）</span>
                  </p>
                  <p v-if="!isHistory && !tripDayCount" class="pending-note">基本信息不完整时，会先提示补充旅行需求。</p>
                </div>
                <button class="text-button guide-requirements-link" @click="navigateToStep('requirements')">填写或修改旅行需求</button>
                <button class="primary" :disabled="!canEdit || !selectedGuideIds.length" @click="run(true)">
                  {{ interviewPending && !candidateInputsChanged ? '重新开始需求追问' : '选好了，开始协作规划' }}
                </button>
              </div>
            </section>

            <!-- 需求追问 -->
            <section v-if="tab === 'interview'" class="panel-body">
              <div class="section-intro">
                <h2>{{ planningDecision ? '处理当前路线取舍' : '先把想要的旅行聊清楚' }}</h2>
                <p>{{ planningDecision ? '主理人已为你算好可执行方案并预选推荐项；可直接按推荐继续，也可改选其他方案或填写文字说明。' : '每次只问一个问题。提交这一题后，再根据你的回答决定下一问；推荐选项可以改成自己的答案。' }}</p>
              </div>
              <p v-if="candidateInputsChanged && !isHistory" class="pending-note">攻略或基本需求已变化，请回到参考攻略重新协作规划。</p>
              <InterviewPanel v-if="interviewPending" :key="current.id" :pending="interviewPending" :stop-names="stopNameMap" :disabled="!canEdit || candidateInputsChanged" @answer="answerInterview" />
              <LoadingState v-else-if="isRunning" title="正在整理需求与查证行程" detail="偏好问题和路线冲突都在这里回答，问题解决后会显示完整候选路线。" />
              <div v-else-if="doc?.interview?.summary" class="interview-summary">
                <h3>{{ doc.interview.confirmed ? '已确认的旅行需求' : '目前的需求总结' }}</h3>
                <p>{{ doc.interview.summary }}</p>
                <button v-if="doc.interview.confirmed" class="secondary" @click="navigateToStep('routes')">查看候选路线</button>
              </div>
              <div v-else class="empty-panel">
                <h3>规划中的选择与回答记录</h3>
                <p>先在参考攻略中勾选内容，点击“选好了，开始协作规划”。</p>
                <button class="secondary" @click="navigateToStep('guides')">选择参考攻略</button>
              </div>
              <details v-if="doc?.interview?.history?.length" class="interview-history">
                <summary>查看前面 {{ doc.interview.history.length }} 轮回答</summary>
                <template v-for="(round,index) in doc.interview.history" :key="index">
                  <section v-if="hasRoundContent(round)">
                    <h3>第 {{ index + 1 }} 轮</h3>
                    <template v-if="round.questions?.length">
                      <p v-for="question in round.questions" :key="question.id"><strong>{{ question.title }}</strong><br>{{ roundAnswer(round, question) }}</p>
                    </template>
                    <p v-else-if="roundFallbackText(round)">{{ roundFallbackText(round) }}</p>
                    <p v-if="round.note">补充：{{ round.note }}</p>
                    <button
                      v-if="round.reopenable && !isHistory"
                      class="text-button"
                      :disabled="!canEdit || candidateInputsChanged"
                      @click="reopenDecision(index)"
                    >重新回答这一题</button>
                  </section>
                </template>
              </details>
            </section>

            <!-- 候选路线 -->
            <section v-if="tab === 'routes'" class="panel-body">
              <div class="section-intro">
                <h2>看看这份路线是否合你心意</h2>
                <p>完整路线通过独立审查后才会作为候选显示；未通过时保留草稿供修复。</p>
              </div>
              <LoadingState
                v-if="isRunning"
                :title="doc?.progress_stage === 'reviewing' ? '检验 Agent 正在重审完整行程' : '正在生成、查证或修复候选路线'"
                :detail="doc?.progress_message || '正在核对所选攻略、真实资源和全程地点与餐饮重复，完成后显示审查结果。'"
              />
              <div v-else-if="interviewPending" class="empty-panel">
                <h3>先解决当前规划问题</h3>
                <p>需求和路线取舍确认后，这里会显示完整候选路线。</p>
                <button class="primary" @click="navigateToStep('interview')">前往需求追问</button>
              </div>
              <div v-else-if="!doc?.candidates.length" class="empty-panel">
                <h3>候选路线还在等待攻略</h3>
                <p>资料不足时会保留缺口，补充攻略后可以继续。</p>
                <button class="secondary" @click="navigateToStep('guides')">查看参考攻略</button>
              </div>
              <div v-else-if="blockedCandidate" class="issues-panel">
                <h3>尚未形成可确认的路线</h3>
                <p>{{ doc?.stage === 'failed' ? '自动修复或查证未能完成，已保留最新草稿与问题。这是处理失败，不需要你批准修复。' : '发现的问题会自动反馈责任 Agent，修改后重新审查；只有真实取舍才会转到需求追问。' }}</p>
                <ReviewPanel :review="blockedCandidate.review" />
                <p v-for="reason in blockedCandidate.confirmation_blockers || []" :key="reason">{{ reason }}</p>
                <details>
                  <summary>查看未通过的草稿</summary>
                  <CoordinatorMessages
                    :messages="doc?.coordinator_messages"
                    :disabled="!canEdit || candidateInputsChanged"
                    :historical="isHistory"
                    @reopen="reopenDecision"
                  />
                  <PlanView
                    v-if="blockedCandidate.draft"
                    :itinerary="blockedCandidate.draft"
                    :disabled="!canEdit || candidateInputsChanged"
                    :editable-dates="doc?.pending?.kind === 'selection'"
                    :required-ids="requirements.required_stop_ids || doc?.stop_constraints?.required || []"
                    :selectable="doc?.pending?.kind === 'selection'"
                    :selected-ids="selectedActivityIds"
                    @source="showSource"
                    @edit-date="date => blockedCandidate && editCandidate(blockedCandidate,date)"
                    @select="selectActivity"
                  />
                </details>
              </div>
              <div v-else-if="!visibleCandidates.length" class="empty-panel">
                <h3>还没有完整的候选路线</h3>
                <p>请重新协作规划，完成需求追问与行程查证后再查看路线。</p>
                <button class="secondary" :disabled="isRunning" @click="navigateToStep('guides')">返回参考攻略</button>
              </div>
              <p v-if="candidateInputsChanged && !isHistory && doc?.candidates.length" class="pending-note">参考攻略或旅行需求已变化，请重新生成协作路线。</p>
              <button
                v-if="!isHistory && doc?.candidates.length && !doc.pending && doc.itinerary"
                class="secondary spaced"
                :disabled="!canEdit || candidateInputsChanged"
                @click="reopenRoute"
              >返回候选路线继续调整</button>

              <article v-for="candidate in visibleCandidates" :key="candidate.id" class="candidate-plan">
                <header class="candidate-head">
                  <span class="badge">候选路线</span>
                  <h3>{{ candidate.title }}</h3>
                  <p class="candidate-date-range">{{ candidate.days.length }} 天 · {{ candidate.days[0]?.date }} 至 {{ candidate.days[candidate.days.length-1]?.date }}</p>
                  <p class="candidate-reason">{{ candidate.reason }}</p>
                </header>
                <ReviewPanel :review="candidate.review" :historical="isHistory" />
                <CoordinatorMessages
                  v-if="!showRouteChat"
                  :messages="doc?.coordinator_messages"
                  :disabled="!canEdit || candidateInputsChanged"
                  :historical="isHistory"
                  @reopen="reopenDecision"
                />
                <section v-if="candidate.feasibility?.budget" class="candidate-feasibility">
                  <div class="fact-row">
                    <div><small>已知费用</small><strong>{{ money(candidate.feasibility.budget.known_subtotal) }}</strong></div>
                    <div><small>加各项估算</small><strong>{{ money(candidate.feasibility.budget.known_plus_estimates) }}</strong></div>
                    <div v-if="candidate.feasibility.budget.unknown.length"><small>待核对费用</small><strong>{{ candidate.feasibility.budget.unknown.length }} 项</strong></div>
                  </div>
                  <p v-for="entry in candidate.feasibility.transport" :key="entry.label">
                    {{ entry.label }}：{{ entry.selected.name || '班次待确认' }}<span v-if="entry.selected.departure"> · {{ entry.selected.departure }} → {{ entry.selected.arrival }}</span>
                  </p>
                  <div v-for="hotel in candidate.feasibility.hotels" :key="hotel.city">
                    <p>
                      {{ hotel.city }}{{ hotel.selected_by_user ? '已采用住宿' : '住宿推荐（未参与行程）' }}：{{ hotel.name || '暂无推荐' }}<span v-if="hotel.selected?.price != null"> · {{ money(hotel.selected.price) }}（查询参考价）</span> · 搜索区域 {{ hotel.area.join('、') || '待定位' }}
                    </p>
                    <p v-if="!hotel.name && hotel.reason" class="pending-note">{{ hotel.reason }}</p>
                    <p v-for="failure in hotel.errors || []" :key="failure" class="pending-note">{{ failure }}</p>
                  </div>
                  <p v-for="issue in candidate.feasibility.issues" :key="issue.code + issue.target" class="pending-note">{{ issue.message }}</p>
                  <small class="subtle">查询于 {{ new Date(candidate.feasibility.queried_at).toLocaleString() }}，库存与最终价格以预订页面为准。</small>
                </section>
                <PlanView
                  v-if="candidate.draft"
                  :itinerary="candidate.draft"
                  :disabled="!canEdit || candidateInputsChanged || !!planningDecision"
                  :editable-dates="current.document.pending?.kind === 'selection'"
                  :required-ids="requirements.required_stop_ids || doc?.stop_constraints?.required || []"
                  :selectable="current.document.pending?.kind === 'selection' && !isHistory"
                  :selected-ids="selectedActivityIds"
                  @source="showSource"
                  @edit-date="date => editCandidate(candidate,date)"
                  @select="selectActivity"
                />
                <div v-else class="candidate-days">
                  <div v-for="(day,index) in candidate.days" :key="day.date" class="candidate-day">
                    <span>第 {{ index + 1 }} 天<br>{{ day.date.slice(5) }}<strong>{{ day.city }}</strong></span>
                    <div>
                      <p>{{ day.stop_ids.map(id => sourceStops.get(id)?.name || '来源待查看').join(' → ') || '攻略活动待补充' }}</p>
                      <p v-if="day.adjustment" class="subtle">{{ day.adjustment }}</p>
                      <div class="source-links">
                        <button
                          v-for="id in [...new Set(day.stop_ids.filter(id => !id.startsWith('product:')).map(id => id.split(':')[0]))]"
                          :key="id"
                          class="text-button"
                          @click="showSource(id)"
                        >{{ sourceMap.get(id)?.title || '查看参考攻略' }}</button>
                        <small v-if="day.stop_ids.some(id => id.startsWith('product:'))">包含你确认的飞猪体验</small>
                      </div>
                      <button
                        v-if="!isHistory && current.document.pending?.kind === 'selection'"
                        class="text-button candidate-edit-link"
                        :disabled="!canEdit || candidateInputsChanged"
                        @click="editCandidate(candidate,day.date)"
                      >调整这一天</button>
                    </div>
                  </div>
                </div>
                <div v-if="candidate.confirmation_blockers?.length || !reviewReady(candidate)" class="issues-panel">
                  <h3>处理以下问题后才能确认</h3>
                  <p v-for="reason in candidate.confirmation_blockers || []" :key="reason">{{ reason }}</p>
                  <button
                    class="secondary"
                    :disabled="!canEdit || candidateInputsChanged || current.document.pending?.kind !== 'selection'"
                    @click="recheckCandidate(candidate)"
                  >补齐依据并重新审查</button>
                </div>
                <p v-if="!candidate.draft" class="pending-note">这是旧版候选，请重新规划以生成完整路线草稿。</p>
                <div class="candidate-actions action-bar">
                  <span class="action-bar-note">确认后保存为每日行程，之后仍可提出修改。</span>
                  <button
                    v-if="!isHistory && current.document.pending?.kind === 'selection'"
                    class="secondary"
                    :disabled="!canEdit || candidateInputsChanged"
                    @click="editCandidate(candidate)"
                  >提出局部修改</button>
                  <button
                    class="primary"
                    :disabled="!canEdit || candidateInputsChanged || !selectedGuideIds.length || !candidate.draft || !reviewReady(candidate) || !!candidate.confirmation_blockers?.length || current.document.pending?.kind !== 'selection'"
                    @click="choose(candidate)"
                  >{{ current.document.selected_route?.id === candidate.id && doc?.itinerary ? '已确认并保存' : '确认并保存这份路线' }}</button>
                </div>
              </article>
            </section>

            <!-- 酒店与门票 -->
            <section v-if="tab === 'resources'" class="panel-body">
              <div class="section-intro">
                <h2>住宿安排与预订参考</h2>
                <p>住宿按每日行程推荐区域，也可以自己指定路段。门票按攻略说明展示，涉及购票的景点可查询飞猪参考；多个票种分别列出。</p>
              </div>
              <details v-if="itinerary" class="resource-section" open>
                <summary>行程住宿与备选</summary>
                <div v-for="entry in itinerary.hotels" :key="entry.id">
                  <p v-if="entry.search_areas?.length" class="subtle">{{ entry.city }}住宿搜索区域：{{ entry.search_areas.join('、') }}附近</p>
                  <ResourceBlock
                    :entry="entry"
                    :locked="doc?.locks.includes(entry.id) || false"
                    :disabled="!canEdit || candidateInputsChanged || !!doc?.pending || !!doc?.itinerary_outdated"
                    @lock="toggleLock"
                    @select="resourceSelect"
                    @refresh="id => refreshQuotes([id])"
                  />
                </div>
                <p v-if="!itinerary.hotels.length" class="subtle">没有需要住宿的夜晚。</p>
              </details>
              <ProductPanel
                v-if="doc"
                :key="current.id + '-resources'"
                :trip-id="current.id"
                :base-version="current.version"
                :updated-at="current.updated_at"
                :document="doc"
                :disabled="!canEdit"
                :plan-disabled="candidateInputsChanged"
                @changed="productChanged"
                @run="productRun"
                @busy="productBusy"
              />
            </section>

            <!-- 每日行程 -->
            <section v-if="tab === 'itinerary'" class="panel-body">
              <p v-if="doc?.itinerary_outdated" class="info-banner">下方是上一版行程，新的候选路线尚未采用。若新日期与锁定项目冲突，可在下方先解锁旧安排。</p>
              <div class="section-intro with-action">
                <div>
                  <h2>你的每日行程</h2>
                  <p>这里显示你已确认的完整路线。路线修改先形成候选草稿，确认后再更新此版本。</p>
                </div>
                <div v-if="itinerary" class="itinerary-actions">
                  <button class="secondary danger itinerary-delete" :disabled="isRunning" @click="deleteItinerary">{{ isHistory || savedItineraryVersion !== null ? '删除此版本' : '删除草稿' }}</button>
                  <button v-if="!isHistory" class="primary" :disabled="!canSaveItinerary" @click="saveItinerary">{{ savedItineraryVersion !== null ? '已保存' : '保存行程' }}</button>
                </div>
              </div>
              <LoadingState
                v-if="current.run?.status === 'running' && !isHistory"
                title="正在准备路线修改草稿"
                detail="正在查询交通、住宿、门票参考和地图路线。已确认行程会保留，修改草稿确认后才更新。"
              />
              <div v-else-if="!itinerary" class="empty-panel">
                <h3>确认完整候选后，显示每日行程</h3>
                <p>候选包含游览时段、活动点之间的交通方式与耗时、住宿区域和攻略票种说明，确认后直接保存。</p>
                <button class="secondary" @click="navigateToStep('routes')">查看候选路线</button>
              </div>
              <template v-else>
                <p class="itinerary-status" :class="{draft: !isHistory && savedItineraryVersion === null}" role="status">
                  {{ itineraryStatus }}<span v-if="!isHistory && savedItineraryVersion === null"> · 草稿会保留，尚未计入已保存版本。</span>
                </p>
                <div v-if="itinerary.issues.length" class="issues-panel">
                  <h3>需要关注的安排</h3>
                  <p v-for="issue in itinerary.issues" :key="issue.code + issue.target">{{ issue.message }}</p>
                </div>
                <MapPanel
                  :key="current.id + '-' + selectedVersion"
                  :trip-id="current.id"
                  :itinerary="itinerary"
                  :version="selectedVersion"
                  :config-revision="amapConfigRevision"
                  :disabled="!canEdit || candidateInputsChanged || !!doc?.pending || !!doc?.itinerary_outdated"
                  :exporting="busy && actionLabel === '正在生成手机高德导出入口'"
                  @configure="amapSettingsOpen = true"
                  @export="exportMap"
                />
                <section v-if="taxiResult" class="info-banner">
                  <span>{{ taxiResult.note }}</span>
                  <a v-for="link in taxiResult.links" :key="link" :href="safeMapLink(link)" target="_blank" rel="noopener noreferrer">打开高德打车</a>
                </section>
                <section v-if="itinerary.change_summary?.length" class="day-gaps">
                  <h3>本次变化</h3>
                  <p v-for="change in itinerary.change_summary" :key="change">{{ change }}</p>
                </section>
                <details class="resource-section" open>
                  <summary>跨城市交通</summary>
                  <ResourceBlock
                    v-for="entry in itinerary.transport"
                    :key="entry.id"
                    :entry="entry"
                    :locked="doc?.locks.includes(entry.id) || false"
                    :disabled="!canEdit || candidateInputsChanged || !!doc?.pending || !!doc?.itinerary_outdated"
                    @lock="toggleLock"
                    @select="resourceSelect"
                    @refresh="id => refreshQuotes([id])"
                  />
                </details>
                <p v-if="itinerary.hotels.length" class="subtle">
                  住宿选择与备选位于“酒店与门票”。<button class="text-button" @click="moveToStep('resources')">查看行程住宿</button>
                </p>
                <ReviewPanel :review="doc?.review" :historical="isHistory" />
                <CoordinatorMessages
                  :messages="doc?.coordinator_messages"
                  :disabled="!canEdit || candidateInputsChanged"
                  :historical="isHistory"
                  @reopen="reopenDecision"
                />
                <PlanView
                  :itinerary="itinerary"
                  :interactive="!isHistory"
                  :disabled="!canEdit || candidateInputsChanged || !!doc?.pending || !!doc?.itinerary_outdated"
                  :locks="doc?.locks || []"
                  :required-ids="doc?.stop_constraints?.required || []"
                  :allow-taxi="!!doc?.requirements.allow_taxi"
                  @lock="toggleLock"
                  @source="showSource"
                  @ticket="id => refreshQuotes(['ticket:' + id])"
                  @taxi="openTaxi"
                />
                <section class="modify-panel">
                  <h3>调整这趟旅行</h3>
                  <p>例如：“杭州酒店换便宜一点，第二天少走路，苏州安排保留。”修改先生成候选草稿，确认后才替换已保存版本。</p>
                  <form @submit.prevent="run(false,true)">
                    <textarea v-model="message" :disabled="!canEdit" rows="3" maxlength="8000" placeholder="告诉我想修改的城市、日期或安排"></textarea>
                    <button class="primary" :disabled="!canEdit || !message.trim()">生成修改后的草稿</button>
                  </form>
                  <p v-if="doc?.changes?.explanation" class="subtle">最近修改：{{ doc.changes.explanation }}</p>
                </section>
              </template>
            </section>
          </div>

          <aside class="context-panel" :class="{'chat-sidebar':showRouteChat}">
            <RouteChat
              v-if="showRouteChat && doc?.candidates[0]"
              ref="routeChat"
              :key="current.id"
              :days="doc.candidates[0].days"
              :messages="doc.route_chat || []"
              :coordinator-messages="doc.coordinator_messages"
              @reopen="reopenDecision"
              :selected-activities="selectedActivities"
              :disabled="!canEdit || candidateInputsChanged"
              @revise="reviseFromChat"
              @unselect="selectActivity"
            />
            <section class="progress-panel side-card">
              <h2>当前规划步骤</h2>
              <p class="current-planning-step" role="status" aria-live="polite">
                <span class="pulse" :class="{active: isRunning}" aria-hidden="true"></span>{{ planningStep }}
              </p>
              <button
                v-if="['failed','recoverable'].includes(current.run?.status || '')"
                class="primary full-width"
                :disabled="!canEdit"
                @click="run(true,false,true)"
              >继续本次任务</button>
            </section>
            <details v-if="itinerary?.information_unknown?.length" class="day-gaps side-card">
              <summary>{{ itinerary.information_unknown.length }} 项行程信息待确认（不计入未知费用数量）</summary>
              <ul><li v-for="item in itinerary.information_unknown" :key="item">{{ item }}</li></ul>
            </details>
            <BudgetPanel
              v-if="itinerary"
              :itinerary="itinerary"
              :history="isHistory"
              :disabled="!canEdit || candidateInputsChanged || !!doc?.pending"
              @refresh="refreshQuotes()"
            />
            <section v-if="doc?.rollback?.message" class="warnings-panel side-card">
              <h2>本轮已回退</h2>
              <p>{{ doc.rollback.message }}</p>
              <p>{{ doc.rollback.reason }}</p>
            </section>
            <section v-if="doc?.warnings.length" class="warnings-panel side-card">
              <h2>数据与接入提示</h2>
              <p v-for="warning in doc.warnings" :key="warning">{{ warning }}</p>
            </section>
          </aside>
        </div>
      </template>
    </div>

    <CrawlerSettings v-if="crawlerSettingsOpen" @close="crawlerSettingsOpen = false; refreshCapabilities()" @changed="refreshCapabilities" />
    <AmapSettings v-if="amapSettingsOpen" :active="modelTasksActive" @close="amapSettingsOpen = false; refreshCapabilities()" @saved="amapConfigSaved" />
    <FlyaiSettings v-if="flyaiSettingsOpen" :active="modelTasksActive" @close="flyaiSettingsOpen = false; refreshCapabilities()" @saved="refreshCapabilities()" />
    <ModelSettings v-if="modelSettingsOpen" :active="modelTasksActive" @close="modelSettingsOpen = false; refreshCapabilities()" @saved="refreshCapabilities" />

    <div v-if="dialog" class="modal-backdrop" @click.self="!busy && (dialog = null)">
      <section class="modal" role="dialog" aria-modal="true" :aria-label="dialog === 'create' ? '创建旅行' : '导入攻略'">
        <header>
          <h2>{{ dialog === 'create' ? '给这趟旅行起个名字' : '导入参考攻略' }}</h2>
          <button class="icon-close" aria-label="关闭" :disabled="busy" @click="dialog = null">×</button>
        </header>
        <form v-if="dialog === 'create'" @submit.prevent="createTrip">
          <label>旅行名称<input v-model="tripTitle" autofocus placeholder="例如：江南慢游六天" maxlength="100"></label>
          <p class="subtle">创建后可手动搜索或导入攻略，再填写需求开始规划。</p>
          <div class="modal-actions"><button class="primary" :disabled="busy">创建旅行</button></div>
        </form>
        <form v-else @submit.prevent="saveSource">
          <p v-if="error" class="pending-note" role="alert">{{ error }}</p>
          <label>攻略标题<input v-model="sourceTitle" placeholder="给攻略一个易辨认的名字" maxlength="200"></label>
          <label>主要城市<input v-model="sourceCity" placeholder="例如：杭州"></label>
          <label>小红书链接<input v-model="sourceUrl" placeholder="可选，保留完整分享链接和访问参数"></label>
          <label>攻略正文<textarea v-model="sourceText" rows="6" placeholder="粘贴攻略正文，保留每日路线与地点名称" maxlength="30000"></textarea></label>
          <label>攻略图片<input type="file" accept="image/jpeg,image/png,image/webp" multiple @change="filesChanged"></label>
          <p class="subtle">已选择 {{ sourceFiles.length }} 张。每张不超过 10 MB，最多上传 18 张，单篇首轮解析前 9 张。仅提供链接会保存来源，请同时导入正文或图片。</p>
          <div class="modal-actions"><button class="primary" :disabled="busy">保存攻略</button></div>
        </form>
      </section>
    </div>
    <SourceModal v-if="displayedSource" :source="displayedSource" @close="displayedSource = null" />
  </div>
</template>
