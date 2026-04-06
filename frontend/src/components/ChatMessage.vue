<script setup>
import { computed } from "vue";
import DOMPurify from "dompurify";
import { marked } from "marked";

const props = defineProps({
  message: {
    type: Object,
    required: true,
  },
});

marked.setOptions({
  gfm: true,
  breaks: true,
});

const isMarkdownMessage = computed(() => props.message.role === "assistant");

const renderedContent = computed(() => {
  if (!isMarkdownMessage.value) {
    return "";
  }

  const raw = typeof props.message.content === "string" ? props.message.content : "";
  const html = marked.parse(raw);
  return DOMPurify.sanitize(html);
});
</script>

<template>
  <article :class="['message-card', `role-${props.message.role}`]">
    <header class="message-header">
      <span class="message-role">{{ props.message.role }}</span>
    </header>
    <div v-if="isMarkdownMessage" class="message-content markdown-content" v-html="renderedContent"></div>
    <div v-else class="message-content">{{ props.message.content }}</div>
  </article>
</template>
