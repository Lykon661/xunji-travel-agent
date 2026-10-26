<script setup lang="ts">
import { computed, nextTick, reactive, ref, watch } from 'vue'
import type { DecisionOption, DecisionQuestion, InterviewResponse, Pending } from './types'

interface NormalizedOption {
  id: string
  label: string
  description?: string
  recommended: boolean
  impact?: DecisionOption['impact']
  isLegacy: boolean
}

const props = defineProps<{
  pending: Pending
  disabled: boolean
  stopNames?: Record<string, string>
}>()

const emit = defineEmits<{
  answer: [value: InterviewResponse]
}>()

const selectedOptions = reactive<Record<string, string>>({})
const customAnswers = reactive<Record<string, string>>({})
const customOpen = reactive<Record<string, boolean>>({})
const note = ref('')
const reediting = ref(false)
const cachedQuestions = ref<DecisionQuestion[]>([])
const cachedDraftRevision = ref<number | undefined>(undefined)
const lastInitializedRound = ref('')
const confirmHeadingRef = ref<HTMLElement | null>(null)
const noteInputRef = ref<HTMLTextAreaElement | null>(null)
const customInputRefs = new Map<string, HTMLTextAreaElement>()

function setCustomInputRef(id: string, el: HTMLTextAreaElement | null) {
  if (el) customInputRefs.set(id, el)
  else customInputRefs.delete(id)
}

const effectiveRoundId = computed(() => props.pending.batch_id || props.pending.round_id || '')
const effectiveDraftRevision = computed(() => props.pending.draft_revision ?? cachedDraftRevision.value)
const isConfirming = computed(() => (props.pending.kind === 'decision_confirmation' || !!props.pending.interpretation) && !reediting.value)

const effectiveQuestions = computed<DecisionQuestion[]>(() => {
  if (props.pending.questions?.length) return props.pending.questions
  if (cachedQuestions.value.length) return cachedQuestions.value
  const origAnswers = props.pending.interpretation?.original_answers
  if (origAnswers && Object.keys(origAnswers).length) {
    return Object.keys(origAnswers).map((id, idx) => ({
      id,
      title: `取舍项 ${idx + 1}`,
      question: '请修改或补充你对这一项的说明：',
      options: [],
    }))
  }
  return []
})

function isLegacyQuestion(question: DecisionQuestion): boolean {
  return (question.options || []).every(opt => typeof opt === 'string')
}

function normalizeOptions(question: DecisionQuestion): NormalizedOption[] {
  const rawList = question.options || []
  const recText = (question.recommendation || '').trim()
  const recPrefix = recText.split(/[：:，,。]/)[0]?.trim() || ''
  let matchedAnyRec = false

  const items: NormalizedOption[] = rawList.map(raw => {
    if (typeof raw === 'string') {
      const clean = raw.trim()
      const isRec = Boolean(
        (question.recommended_option_id && question.recommended_option_id === raw) ||
        (clean && recText && (recText.startsWith(clean) || (recPrefix && clean.startsWith(recPrefix))))
      )
      if (isRec) matchedAnyRec = true
      return {
        id: raw,
        label: raw,
        recommended: isRec,
        isLegacy: true,
      }
    }
    const isRec = Boolean(raw.recommended || (question.recommended_option_id && question.recommended_option_id === raw.id))
    if (isRec) matchedAnyRec = true
    return {
      id: raw.id,
      label: raw.label,
      description: raw.description,
      recommended: isRec,
      impact: raw.impact,
      isLegacy: false,
    }
  })

  if (!matchedAnyRec && items.length > 0 && !isLegacyQuestion(question)) {
    items[0].recommended = true
  }
  return items
}

function getRecommendedOptionId(question: DecisionQuestion): string {
  const opts = normalizeOptions(question)
  const rec = opts.find(o => o.recommended)
  if (rec) return rec.id
  if (question.recommended_option_id && opts.some(o => o.id === question.recommended_option_id)) {
    return question.recommended_option_id
  }
  return opts[0]?.id || ''
}

function formatImpact(impact?: DecisionOption['impact']): string[] {
  if (!impact) return []
  const tags: string[] = []
  if (impact.minutes != null) {
    const sign = impact.minutes > 0 ? '+' : ''
    tags.push(`${sign}${impact.minutes} 分钟`)
  }
  if (impact.amount != null) {
    const sign = impact.amount > 0 ? '+¥' : impact.amount < 0 ? '-¥' : '¥'
    tags.push(`${sign}${Math.abs(impact.amount)}`)
  }
  if (impact.meters != null) {
    const sign = impact.meters > 0 ? '+' : ''
    tags.push(`${sign}${impact.meters} 米`)
  }
  return tags
}

function resolveStopLabels(question: DecisionQuestion): string[] {
  if (!question.affected_stop_ids?.length) return []
  return question.affected_stop_ids.map(id => {
    if (props.stopNames?.[id]) return props.stopNames[id]
    if (id.startsWith('wish:')) return '你填写的地点'
    return id
  })
}

function initRoundState(questions: DecisionQuestion[]) {
  Object.keys(selectedOptions).forEach(k => delete selectedOptions[k])
  Object.keys(customAnswers).forEach(k => delete customAnswers[k])
  Object.keys(customOpen).forEach(k => delete customOpen[k])
  for (const q of questions) {
    const opts = normalizeOptions(q)
    const recId = getRecommendedOptionId(q)
    if (recId) selectedOptions[q.id] = recId
    customAnswers[q.id] = ''
    customOpen[q.id] = opts.length === 0
  }
  note.value = ''
}

watch(
  () => [props.pending.kind, effectiveRoundId.value, props.pending.interpretation?.interpretation_id] as const,
  async ([kind, roundId]) => {
    if (props.pending.questions?.length) {
      cachedQuestions.value = props.pending.questions
    }
    if (props.pending.draft_revision !== undefined) {
      cachedDraftRevision.value = props.pending.draft_revision
    }
    if (kind === 'decision_confirmation' || props.pending.interpretation) {
      reediting.value = false
      const interp = props.pending.interpretation
      if (interp?.original_answers) {
        for (const [qid, text] of Object.entries(interp.original_answers)) {
          if (typeof text === 'string' && text.trim()) {
            customAnswers[qid] = text
            customOpen[qid] = true
          }
        }
      }
      if (interp?.original_note !== undefined && interp?.original_note !== null) {
        note.value = interp.original_note
      }
      lastInitializedRound.value = roundId
      await nextTick()
      confirmHeadingRef.value?.focus()
      return
    }

    reediting.value = false
    if (roundId !== lastInitializedRound.value || !Object.keys(selectedOptions).length) {
      initRoundState(effectiveQuestions.value)
      lastInitializedRound.value = roundId
    }
  },
  { immediate: true }
)

function hasActiveCustomText(qid: string): boolean {
  return Boolean(customOpen[qid] && customAnswers[qid]?.trim())
}

function isOptionSelected(qid: string, optId: string): boolean {
  if (hasActiveCustomText(qid)) return false
  return selectedOptions[qid] === optId
}

function selectOption(qid: string, optId: string) {
  if (props.disabled) return
  selectedOptions[qid] = optId
  customAnswers[qid] = ''
  customOpen[qid] = false
}

async function toggleCustomText(qid: string) {
  if (props.disabled) return
  const nextState = !customOpen[qid]
  customOpen[qid] = nextState
  if (!nextState) {
    customAnswers[qid] = ''
  } else {
    await nextTick()
    customInputRefs.get(qid)?.focus()
  }
}

const canSubmitRecommended = computed(() => {
  return (
    !props.disabled &&
    !!effectiveRoundId.value &&
    effectiveQuestions.value.length > 0 &&
    effectiveQuestions.value.every(q => !!getRecommendedOptionId(q))
  )
})

const isFormComplete = computed(() => {
  if (!effectiveRoundId.value) return false
  if (!effectiveQuestions.value.length) return !!note.value.trim()
  return effectiveQuestions.value.every(q => {
    const text = customOpen[q.id] ? (customAnswers[q.id] || '').trim() : ''
    return Boolean(text || selectedOptions[q.id])
  })
})

function submitRecommended() {
  if (!canSubmitRecommended.value) return
  const selected_options: Record<string, string> = {}
  const answers: Record<string, string> = {}
  for (const q of effectiveQuestions.value) {
    const recId = getRecommendedOptionId(q)
    if (recId) {
      selectedOptions[q.id] = recId
      if (isLegacyQuestion(q)) {
        answers[q.id] = recId
      } else {
        selected_options[q.id] = recId
      }
    }
    customAnswers[q.id] = ''
    customOpen[q.id] = false
  }
  emit('answer', {
    round_id: effectiveRoundId.value,
    ...(effectiveDraftRevision.value !== undefined ? { draft_revision: effectiveDraftRevision.value } : {}),
    selected_options,
    answers,
    note: note.value.trim(),
  })
}

function submitSelected() {
  if (!effectiveRoundId.value || props.disabled || !isFormComplete.value) return
  const selected_options: Record<string, string> = {}
  const answers: Record<string, string> = {}

  for (const q of effectiveQuestions.value) {
    const text = customOpen[q.id] ? (customAnswers[q.id] || '').trim() : ''
    if (text) {
      answers[q.id] = text
    } else if (selectedOptions[q.id]) {
      if (isLegacyQuestion(q)) {
        answers[q.id] = selectedOptions[q.id]
      } else {
        selected_options[q.id] = selectedOptions[q.id]
      }
    }
  }

  emit('answer', {
    round_id: effectiveRoundId.value,
    ...(effectiveDraftRevision.value !== undefined ? { draft_revision: effectiveDraftRevision.value } : {}),
    selected_options,
    answers,
    note: note.value.trim(),
  })
}

function confirmInterpretation() {
  const interpId = props.pending.interpretation?.interpretation_id
  if (!effectiveRoundId.value || !interpId || props.disabled) return
  emit('answer', {
    round_id: effectiveRoundId.value,
    interpretation_id: interpId,
    confirm: true,
  })
}

async function startReedit() {
  if (props.disabled) return
  const interp = props.pending.interpretation
  if (interp?.original_answers) {
    for (const [qid, text] of Object.entries(interp.original_answers)) {
      if (typeof text === 'string' && text.trim()) {
        customAnswers[qid] = text
        customOpen[qid] = true
      }
    }
  }
  if (interp?.original_note !== undefined && interp?.original_note !== null) {
    note.value = interp.original_note
  }
  reediting.value = true
  await nextTick()
  const firstAnswerId =
    Object.keys(customAnswers).find(id => customOpen[id] && customAnswers[id]?.trim()) ||
    Object.keys(customOpen).find(id => customOpen[id])
  if (firstAnswerId && customInputRefs.get(firstAnswerId)) {
    customInputRefs.get(firstAnswerId)?.focus()
  } else {
    noteInputRef.value?.focus()
  }
}
</script>

<template>
  <div class="coordinator-decision-panel">
    <!-- 二阶段确认态：decision_confirmation -->
    <section
      v-if="isConfirming && pending.interpretation"
      class="interpretation-confirm-card"
      role="region"
      aria-live="polite"
      aria-label="主理人理解确认"
    >
      <div class="decision-top-bar">
        <span class="badge">请确认理解</span>
        <span role="status" class="subtle">确认无误后将按以下调整执行</span>
      </div>
      <h3 ref="confirmHeadingRef" tabindex="-1">我理解为……</h3>
      <p class="interpretation-summary">{{ pending.interpretation.summary }}</p>

      <div v-if="pending.interpretation.changes?.length" class="interpretation-changes">
        <h4>将执行的调整</h4>
        <ul>
          <li v-for="(change, idx) in pending.interpretation.changes" :key="idx">{{ change }}</li>
        </ul>
      </div>

      <div v-if="pending.interpretation.relaxed?.length" class="interpretation-relaxed" role="alert">
        <h4>需要放宽的原要求</h4>
        <ul>
          <li v-for="(item, idx) in pending.interpretation.relaxed" :key="idx">
            <strong>{{ item }}</strong>
          </li>
        </ul>
      </div>

      <details
        v-if="
          (pending.interpretation.original_answers && Object.keys(pending.interpretation.original_answers).length) ||
          pending.interpretation.original_note
        "
        class="interpretation-original"
      >
        <summary>查看你的原话</summary>
        <p v-for="(text, qid) in pending.interpretation.original_answers" :key="qid">{{ text }}</p>
        <p v-if="pending.interpretation.original_note">补充：{{ pending.interpretation.original_note }}</p>
      </details>

      <div class="decision-actions">
        <button type="button" class="primary" :disabled="disabled" @click="confirmInterpretation">
          {{ disabled ? '正在执行…' : '确认，按此执行' }}
        </button>
        <button type="button" class="secondary" :disabled="disabled" @click="startReedit">重新说</button>
      </div>
    </section>

    <!-- 集中取舍编辑态：decision（或从确认态点“重新说”返回） -->
    <template v-else>
      <div class="decision-top-bar">
        <div class="decision-top-info">
          <strong>{{ pending.stage_scope === 'early_identity' ? '确认地点身份' : '需要你拍板的行程取舍' }}</strong>
          <span v-if="effectiveQuestions.length" role="status" class="subtle">
            共 {{ effectiveQuestions.length }} 项 · 已默认预选推荐方案
          </span>
        </div>
        <button
          v-if="effectiveQuestions.length"
          type="button"
          class="primary small"
          :disabled="!canSubmitRecommended"
          @click="submitRecommended"
        >
          按推荐继续
        </button>
      </div>

      <p v-if="pending.summary" class="info-banner">{{ pending.summary }}</p>

      <form @submit.prevent="submitSelected">
        <fieldset :disabled="disabled">
          <article
            v-for="(question, qIndex) in effectiveQuestions"
            :key="question.id"
            class="decision-card"
          >
            <header class="decision-card-head">
              <div class="decision-title-row">
                <span v-if="effectiveQuestions.length > 1" class="badge">取舍 {{ qIndex + 1 }} / {{ effectiveQuestions.length }}</span>
                <h3 :id="`decision-q-title-${question.id}`">{{ question.title }}</h3>
              </div>
              <div v-if="question.affected_dates?.length || resolveStopLabels(question).length" class="decision-scopes">
                <span v-for="d in question.affected_dates || []" :key="`date-${d}`" class="scope-tag">{{ d }}</span>
                <span v-for="stop in resolveStopLabels(question)" :key="`stop-${stop}`" class="scope-tag stop">{{ stop }}</span>
              </div>
            </header>

            <p class="decision-prompt">{{ question.question }}</p>
            <p v-if="question.why_needed" class="decision-why subtle">{{ question.why_needed }}</p>

            <!-- 方案按钮组（无障碍 radiogroup，兼容旧格式 string[] 与新格式 DecisionOption[]） -->
            <div
              v-if="normalizeOptions(question).length"
              class="decision-options"
              :class="{ 'legacy-options': isLegacyQuestion(question) }"
              role="radiogroup"
              :aria-labelledby="`decision-q-title-${question.id}`"
            >
              <button
                v-for="opt in normalizeOptions(question)"
                :key="opt.id"
                type="button"
                role="radio"
                class="secondary decision-option-btn"
                :class="{
                  chosen: isOptionSelected(question.id, opt.id),
                  recommended: opt.recommended,
                }"
                :aria-checked="isOptionSelected(question.id, opt.id)"
                @click="selectOption(question.id, opt.id)"
              >
                <span class="decision-option-main">
                  <span class="decision-option-label">{{ opt.label }}</span>
                  <span v-if="opt.recommended" class="badge">推荐</span>
                </span>
                <small v-if="opt.description" class="decision-option-desc">{{ opt.description }}</small>
                <span v-if="formatImpact(opt.impact).length" class="decision-option-impacts">
                  <span v-for="tag in formatImpact(opt.impact)" :key="tag" class="impact-tag">{{ tag }}</span>
                </span>
              </button>
            </div>

            <details v-if="isLegacyQuestion(question) && question.recommendation" class="interview-recommendation">
              <summary>查看推荐与理由</summary>
              <p>{{ question.recommendation }}</p>
            </details>

            <!-- 单题自由文字展开与输入 -->
            <div class="decision-custom-area">
              <button
                v-if="normalizeOptions(question).length"
                type="button"
                class="text-button"
                :aria-expanded="!!customOpen[question.id]"
                @click="toggleCustomText(question.id)"
              >
                {{ customOpen[question.id] ? '收起自由文字（改用上方方案）' : '其他想法？展开自由文字输入' }}
              </button>
              <label v-if="customOpen[question.id]" class="decision-custom-label">
                <span>本题自由文字说明（填写后优先按文字理解）</span>
                <textarea
                  :ref="el => setCustomInputRef(question.id, el as HTMLTextAreaElement | null)"
                  v-model="customAnswers[question.id]"
                  rows="2"
                  maxlength="2000"
                  placeholder="直接写下你对这一题的想法；留空则按上方所选方案执行"
                ></textarea>
              </label>
            </div>
          </article>

          <label class="wide-label">
            其他补充（可选）
            <textarea
              ref="noteInputRef"
              v-model="note"
              rows="3"
              maxlength="4000"
              placeholder="如果有跨题目的整体调整想法，可以在这里补充说明"
            ></textarea>
          </label>

          <div class="decision-bottom-bar action-bar">
            <span class="action-bar-note">点选方案将直接执行；填写自由文字将由主理人理解并执行。</span>
            <button
              v-if="effectiveQuestions.length"
              type="button"
              class="secondary"
              :disabled="!canSubmitRecommended"
              @click="submitRecommended"
            >
              按推荐继续
            </button>
            <button type="submit" class="primary" :disabled="!isFormComplete">
              {{ disabled ? '正在提交…' : '提交当前选择' }}
            </button>
          </div>
        </fieldset>
      </form>
    </template>
  </div>
</template>
