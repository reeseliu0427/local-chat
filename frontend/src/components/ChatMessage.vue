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

const contentParts = computed(() =>
  Array.isArray(props.message.content)
    ? props.message.content.filter((part) => part && typeof part === "object")
    : [],
);

const textContent = computed(() => {
  if (typeof props.message.content === "string") {
    return props.message.content;
  }

  return contentParts.value
    .filter((part) => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n\n");
});

const imageParts = computed(() =>
  contentParts.value.filter(
    (part) =>
      part.type === "image_url" &&
      part.image_url &&
      typeof part.image_url.url === "string" &&
      part.image_url.url,
  ),
);

const renderedContent = computed(() => {
  if (!isMarkdownMessage.value) {
    return "";
  }

  const html = marked.parse(textContent.value || "");
  return DOMPurify.sanitize(html);
});
</script>

<template>
  <article :class="['message-card', `role-${props.message.role}`]">
    <header class="message-header">
      <span class="message-role">{{ props.message.role }}</span>
    </header>
    <div v-if="isMarkdownMessage" class="message-content markdown-content" v-html="renderedContent"></div>
    <div v-else class="message-content">
      <div v-if="textContent" class="message-text">{{ textContent }}</div>
      <div v-if="imageParts.length" class="message-images">
        <img
          v-for="(part, index) in imageParts"
          :key="`${props.message.role}-image-${index}`"
          :src="part.image_url.url"
          alt="Uploaded prompt image"
          class="message-image"
        />
      </div>
    </div>
  </article>
</template>
