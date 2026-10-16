<script setup lang="ts">
import { computed } from 'vue'
import type { ReviewState } from './types'

const props = defineProps<{review?: ReviewState | null; historical?: boolean}>()
const statusNames: Record<string,string> = {pass:'独立检验通过',pass_with_warnings:'独立检验通过，仍有待核对信息',
  revise:'发现问题，草稿待修改',needs_evidence:'草稿待补充依据',needs_decision:'草稿需要你作出取舍',review_unavailable:'独立检验未完成'}
const owners: Record<string,string> = {guide:'攻略',resource:'资源',route:'行程',reviewer:'检验'}
const dimensions: Record<string,string> = {places:'地点重复',dining:'餐饮重复',sources:'来源',constraints:'用户约束',timing:'时间衔接',
  lodging:'住宿',budget:'费用口径',facts:'事实依据',feasibility:'整体安排',scope:'修改范围'}
const results: Record<string,string> = {ok:'完成',warning:'有提示',blocker:'有冲突',unknown:'待核对',not_applicable:'不涉及'}
const outdated = computed(() => !props.historical && !!props.review && ['pass','pass_with_warnings'].includes(props.review.status) && (props.review.current === false ||
  !props.review.expires_at || new Date(props.review.expires_at).getTime() <= Date.now()))
</script>

<template>
  <section class="review-panel" aria-label="独立行程检验">
    <h4>{{ historical ? '历史审查记录' : outdated ? '审查已失效，需要重新审查' : statusNames[review?.status || ''] || '尚未经过独立检验' }}</h4>
    <template v-if="review">
      <p>{{ review.summary }}</p>
      <small v-if="review.reviewed_at" class="subtle">审查于 {{ new Date(review.reviewed_at).toLocaleString() }}；查询参考不保证最终价格或库存。</small>
      <details v-if="review.checks?.length"><summary>查看检查项与发现</summary>
        <p v-for="check in review.checks" :key="check.dimension">{{ dimensions[check.dimension] || check.dimension }}：{{ results[check.result] || check.result }}<span v-if="check.note"> · {{ check.note }}</span></p>
        <article v-for="(finding,index) in review.findings || []" :key="index" class="review-finding">
          <strong>{{ finding.severity === 'blocker' ? '待解决' : '提示' }} · {{ finding.dates.join('、') }}</strong>
          <p>{{ finding.message }}</p><p v-if="finding.suggestion" class="subtle">{{ finding.suggestion }}</p>
          <small class="subtle">交给{{ owners[finding.owner] || finding.owner }}处理 · 依据 {{ finding.evidence_ids.join('、') }}</small>
        </article>
      </details>
    </template>
    <p v-else class="subtle">旧候选需要重新审查后才能确认；已保存历史仍可查看。</p>
  </section>
</template>

<style scoped>
.review-panel {border:1px solid var(--line);border-radius:8px;padding:16px;margin:16px 0}
.review-panel h4 {margin:0 0 8px}
.review-finding {border-top:1px solid var(--line);padding:10px 0}
</style>
