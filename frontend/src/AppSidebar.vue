<script setup lang="ts">
import type { TripSummary } from './types'

defineProps<{
  trips: TripSummary[]
  currentId?: string
  busy: boolean
  labels: Record<string,string>
  running: (trip: TripSummary) => boolean
}>()
const emit = defineEmits<{home: []; create: []; search: []; resources: []; open: [id: string]; remove: [trip: TripSummary]}>()
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-head">
      <a href="#" class="brand" @click.prevent="emit('home')">
        <svg viewBox="0 0 40 40" aria-hidden="true">
          <path d="M6 27C6 16 18 30 18 16S34 8 34 17" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
          <circle cx="6" cy="27" r="4" fill="currentColor"/>
          <circle cx="34" cy="17" r="4" fill="none" stroke="currentColor" stroke-width="2.5"/>
        </svg>
        <span>循迹<small>有依据的旅行规划</small></span>
      </a>
      <button class="primary new-trip" :disabled="busy" @click="emit('create')">
        <span aria-hidden="true">＋</span> 创建旅行
      </button>
    </div>

    <div class="sidebar-tools">
      <button class="sidebar-tool" :disabled="busy" @click="emit('search')">
        <svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="5.5"/><path d="M13 13l4 4"/></svg>
        搜索旅行攻略
      </button>
      <button class="sidebar-tool" :disabled="busy" @click="emit('resources')">
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 15V8l7-4 7 4v7"/><path d="M3 15h14M8 15v-4h4v4"/></svg>
        查询酒店与门票
      </button>
    </div>

    <div class="sidebar-label">我的旅行<span v-if="trips.length">{{ trips.length }}</span></div>
    <nav class="trip-list" aria-label="旅行列表">
      <div v-for="trip in trips" :key="trip.id" class="trip-row" :class="{selected: currentId === trip.id}">
        <button class="trip-button" :disabled="busy" :aria-current="currentId === trip.id ? 'page' : undefined" @click="emit('open', trip.id)">
          <span>{{ trip.title }}</span>
          <small><i class="stage-dot" :class="{running: running(trip)}" aria-hidden="true"></i>{{ labels[trip.stage] || trip.stage }}</small>
        </button>
        <button
          class="trip-delete"
          :disabled="busy || running(trip)"
          :aria-label="`删除旅行：${trip.title}`"
          :title="running(trip) ? '正在处理，结束后可删除' : '删除旅行'"
          @click="emit('remove', trip)"
        >
          <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 6h12M8 6V4h4v2M6 6l1 10h6l1-10"/></svg>
          <span>删除</span>
        </button>
      </div>
    </nav>
    <p v-if="!trips.length" class="sidebar-empty">你的第一趟旅行，<br>从一份真实攻略开始。</p>
    <div class="sidebar-footer"><span class="status-dot"></span>个人旅行工作台<small>攻略提供参考，路线由你选择</small></div>
  </aside>
</template>
