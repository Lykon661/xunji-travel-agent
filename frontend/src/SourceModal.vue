<script setup lang="ts">
import { safeLink } from './api'
import { imageUrl } from './format'
import type { Source } from './types'

defineProps<{source: Source}>()
const emit = defineEmits<{close: []}>()
</script>

<template>
  <div class="modal-backdrop" @click.self="emit('close')">
    <section class="modal source-modal" role="dialog" aria-modal="true" aria-label="攻略内容与依据">
      <header>
        <h2>{{ source.title }}</h2>
        <button class="icon-close" aria-label="关闭" @click="emit('close')">×</button>
      </header>
      <a v-if="safeLink(source.url)" :href="safeLink(source.url)" target="_blank" rel="noopener noreferrer" class="inline-link">打开小红书原帖</a>
      <p class="source-text">{{ source.text || '没有可读取的正文' }}</p>
      <div class="source-images">
        <figure v-for="(image, index) in source.images" :key="image">
          <img v-if="imageUrl(image)" :src="imageUrl(image)" :alt="`攻略图片 ${index + 1}`" loading="lazy" referrerpolicy="no-referrer">
          <figcaption>图片 {{ index + 1 }}{{ index >= 9 ? '，本轮未解析' : '' }}</figcaption>
        </figure>
      </div>
      <h3>提取出的路线</h3>
      <ol class="extracted-stops">
        <li v-for="stop in source.extraction?.stops" :key="stop.id">
          <strong>第 {{ stop.day }} 天 · {{ stop.city }} · {{ stop.name }}</strong>
          <blockquote>{{ stop.evidence.quote }}</blockquote>
          <p v-if="stop.ticket_claim" class="subtle">攻略门票信息：{{ stop.ticket_claim }}</p>
          <small>{{ stop.evidence.kind === 'image' ? `图片 ${stop.evidence.image_index}，需核对原图` : '正文证据' }}</small>
        </li>
      </ol>
      <p v-for="gap in source.extraction?.gaps" :key="gap" class="pending-note">{{ gap }}</p>
      <p v-for="caution in source.extraction?.cautions" :key="caution" class="subtle">攻略提醒：{{ caution }}</p>
      <p v-if="source.error" class="pending-note">{{ source.error }}</p>
    </section>
  </div>
</template>
