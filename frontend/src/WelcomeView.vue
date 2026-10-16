<script setup lang="ts">
defineProps<{busy: boolean}>()
const emit = defineEmits<{search: []; create: []}>()
const steps = [
  {title: '参考攻略与来源', text: '搜索或导入真实攻略，每个地点都保留出处。'},
  {title: '喜欢的路线，你来选', text: '先追问你的偏好，再给出经过独立审查的候选路线。'},
  {title: '交通、住宿与每日安排', text: '核对班次、酒店与门票，生成可调整的每日行程。'},
]
</script>

<template>
  <main class="welcome">
    <div class="welcome-copy">
      <p class="welcome-kicker">从别人的旅行经验，找到你的路线</p>
      <h1>先看攻略，<br>再决定怎么出发。</h1>
      <p class="welcome-lead">找到适合你的参考路线，核对交通与住宿，把喜欢的攻略变成一份可调整的行程。</p>
      <div class="welcome-actions">
        <button class="primary large" :disabled="busy" @click="emit('search')">搜索旅行攻略</button>
        <button class="secondary large" :disabled="busy" @click="emit('create')">开始规划旅行</button>
      </div>
      <p class="welcome-footnote">采集不到攻略时，可以导入文字或图片继续规划。</p>
    </div>
    <ol class="welcome-steps" aria-label="规划流程">
      <li v-for="(step, index) in steps" :key="step.title">
        <span class="welcome-step-index" aria-hidden="true">{{ index + 1 }}</span>
        <div><strong>{{ step.title }}</strong><p>{{ step.text }}</p></div>
      </li>
    </ol>
  </main>
</template>
