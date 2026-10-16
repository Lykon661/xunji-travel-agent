<script setup lang="ts">
import { safeLink } from './api'
import { imageUrl, published } from './format'
import type { Source } from './types'

defineProps<{source: Source; selected: boolean; disabled: boolean}>()
const emit = defineEmits<{toggle: []; show: []}>()
</script>

<template>
  <article class="guide-card" :class="{selected}">
    <div class="guide-image">
      <img
        v-if="source.images[0] && imageUrl(source.images[0])"
        :src="imageUrl(source.images[0])"
        alt="攻略首图"
        referrerpolicy="no-referrer"
        loading="lazy"
      >
      <span v-else>旅行笔记</span>
    </div>
    <div class="guide-content">
      <div class="guide-top">
        <label class="guide-choice">
          <input type="checkbox" :checked="selected" :disabled="disabled" @change="emit('toggle')">
          用作参考攻略
        </label>
        <span class="badge">{{ source.imported ? '你导入的攻略' : '小红书检索' }}</span>
      </div>
      <h3>{{ source.title }}</h3>
      <p class="guide-excerpt">{{ source.text || '路线可能位于攻略图片中' }}</p>
      <p class="guide-meta">
        点赞 {{ source.likes.toLocaleString() }} · 收藏 {{ source.favorites.toLocaleString() }} · {{ published(source.published_at) }}
      </p>
      <p class="guide-meta">采集于 {{ new Date(source.collected_at).toLocaleString() }}</p>
      <p v-if="source.error" class="pending-note">{{ source.error }}</p>
      <p v-if="source.extraction" class="extraction-label">
        已提取 {{ source.extraction.stops.length }} 项活动<span v-if="source.extraction.gaps.length">，{{ source.extraction.gaps.length }} 项待确认</span>
      </p>
      <div class="guide-actions">
        <button class="text-button" @click="emit('show')">查看内容与依据</button>
        <a v-if="safeLink(source.url)" :href="safeLink(source.url)" target="_blank" rel="noopener noreferrer">打开原帖</a>
      </div>
    </div>
  </article>
</template>
