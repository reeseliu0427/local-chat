<script setup>
const model = defineModel({ required: true });

defineProps({
  availableModels: {
    type: Array,
    required: true,
  },
  connectionState: {
    type: String,
    required: true,
  },
});

const systemPrompt = defineModel("systemPrompt", { required: true });
const temperature = defineModel("temperature", { required: true });
const maxTokens = defineModel("maxTokens", { required: true });
</script>

<template>
  <aside class="control-panel">
    <div class="panel-block">
      <p class="eyebrow">Connection</p>
      <div class="status-row">
        <span class="status-pill" :data-state="connectionState">{{ connectionState }}</span>
      </div>
    </div>

    <div class="panel-block">
      <label class="field-label" for="model">Model</label>
      <select id="model" v-model="model" class="field-input">
        <option v-for="item in availableModels" :key="item" :value="item">{{ item }}</option>
      </select>
    </div>

    <div class="panel-grid">
      <div class="panel-block">
        <label class="field-label" for="temperature">Temperature</label>
        <input
          id="temperature"
          v-model.number="temperature"
          class="field-input"
          type="number"
          min="0"
          max="2"
          step="0.1"
        />
      </div>
      <div class="panel-block">
        <label class="field-label" for="maxTokens">Max Tokens</label>
        <input
          id="maxTokens"
          v-model.number="maxTokens"
          class="field-input"
          type="number"
          min="32"
          max="4096"
          step="32"
        />
      </div>
    </div>

    <div class="panel-block">
      <label class="field-label" for="systemPrompt">System Prompt</label>
      <textarea
        id="systemPrompt"
        v-model="systemPrompt"
        class="field-input prompt-box"
        placeholder="Optional system instruction for every request."
      />
    </div>
  </aside>
</template>

