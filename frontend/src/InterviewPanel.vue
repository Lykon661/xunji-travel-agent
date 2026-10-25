<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import CoordinatorDecisionPanel from './CoordinatorDecisionPanel.vue'
import type { DecisionOption, InterviewResponse, Pending } from './types'

const props = defineProps<{pending: Pending; disabled: boolean; stopNames?: Record<string, string>}>()
const emit = defineEmits<{answer: [value: InterviewResponse]}>()
const answers = reactive<Record<string,string>>({})
const note = ref('')
const isDecision = computed(() => ['decision', 'decision_confirmation'].includes(props.pending.kind))
const question = computed(() => props.pending.questions?.[0] || null)
const optionStrings = computed(() => (question.value?.options || []).map((opt: string | DecisionOption) => typeof opt === 'string' ? opt : opt.label))
const complete = computed(() => !!question.value && !!answers[question.value.id]?.trim())
watch(() => props.pending.round_id, () => {
  Object.keys(answers).forEach(key => delete answers[key])
  note.value = ''
}, {immediate:true})
function submit(confirm = false) {
  if (!props.pending.round_id || props.disabled) return
  if (question.value && (!complete.value || confirm)) return
  emit('answer', {round_id:props.pending.round_id, answers:question.value ? {[question.value.id]:(answers[question.value.id] || '').trim()} : {}, note:note.value.trim(), confirm, draft_revision:props.pending.draft_revision})
}
</script>

<template>
  <CoordinatorDecisionPanel
    v-if="isDecision"
    :pending="pending"
    :disabled="disabled"
    :stop-names="stopNames"
    @answer="emit('answer', $event)"
  />
  <div v-else class="interview-panel">
    <div class="interview-round-heading"><strong>{{ question ? '回答当前问题' : '确认旅行需求' }}</strong><span v-if="question" role="status">第 {{ pending.round || 1 }} 次追问 · 一次一题</span></div>
    <details v-if="question" class="interview-summary"><summary>查看目前已明确的需求</summary><p>{{ pending.summary }}</p></details>
    <div v-else class="interview-summary"><h3>你的旅行需求</h3><p>{{ pending.summary }}</p></div>
    <form @submit.prevent="submit(false)">
      <fieldset :disabled="disabled">
        <article v-if="question" :key="pending.round_id" class="interview-question">
          <div class="question-heading"><h3>{{ question.title }}</h3><span class="question-answer-status">{{ complete ? '已填写' : '待回答' }}</span></div>
          <p class="question-prompt">{{ question.question }}</p>
          <div v-if="optionStrings.length" class="interview-options">
            <button v-for="option in optionStrings" :key="option" type="button" class="secondary" :class="{chosen:answers[question.id] === option}" :aria-pressed="answers[question.id] === option" @click="answers[question.id] = option">{{ option }}</button>
          </div>
          <label>你的回答<textarea v-model="answers[question.id]" rows="2" maxlength="2000" placeholder="选择上面的选项，或写下自己的想法；也可以明确让我决定" required></textarea></label>
          <details v-if="question.recommendation" class="interview-recommendation"><summary>查看推荐与理由</summary><p>{{ question.recommendation }}</p></details>
        </article>
        <label class="wide-label">{{ question ? '其他补充（可选）' : '需要补充或修正的地方' }}<textarea v-model="note" rows="3" maxlength="4000" placeholder="例如：西湖一定要去，但不希望每天都排满"></textarea></label>
        <p v-if="question" class="subtle">回答并提交这一题后，再根据你的选择确定下一问。</p>
        <button v-if="question" class="primary" :disabled="!complete">{{ disabled ? '正在整理这条回答…' : '提交这一题，继续' }}</button>
        <div v-else class="interview-actions">
          <button class="primary" type="button" :disabled="!!note.trim()" @click="submit(true)">确认需求，生成候选路线</button>
          <button class="secondary" :disabled="!note.trim()">提交补充，更新需求总结</button>
        </div>
      </fieldset>
    </form>
  </div>
</template>

<style scoped>
.interview-round-heading,.question-heading {display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.interview-round-heading {justify-content:space-between;margin-bottom:18px;color:var(--sea)}
.interview-question {padding:22px 0;border-bottom:1px solid var(--line);margin-bottom:18px}
.question-heading h3 {margin:0;flex:1}
.question-answer-status {font-size:12px;color:var(--ink-soft)}
.question-prompt {font-size:16px;line-height:1.8;white-space:pre-wrap}
.interview-summary p,.interview-recommendation p {white-space:pre-wrap;line-height:1.8}
.interview-recommendation {margin-top:14px;font-size:13px}
.interview-options {display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
</style>
