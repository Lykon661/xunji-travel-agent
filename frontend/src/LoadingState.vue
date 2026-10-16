<script setup lang="ts">
defineProps<{title: string; detail?: string; compact?: boolean}>()
</script>

<template>
  <div class="loading-state" :class="{'loading-compact':compact}" role="status" aria-live="polite" aria-busy="true">
    <span class="loading-spinner" aria-hidden="true"></span>
    <div><strong>{{ title }}</strong><p v-if="detail">{{ detail }}</p></div>
  </div>
</template>
