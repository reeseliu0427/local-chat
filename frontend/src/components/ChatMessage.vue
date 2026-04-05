<script setup>
const props = defineProps({
  message: {
    type: Object,
    required: true,
  },
});
</script>

<template>
  <article :class="['message-card', `role-${props.message.role}`]">
    <header class="message-header">
      <span class="message-role">{{ props.message.role }}</span>
    </header>
    <div class="message-content">{{ props.message.content }}</div>
  </article>
</template>

