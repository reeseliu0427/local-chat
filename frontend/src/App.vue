<script setup>
import { computed, onMounted, ref, watch } from "vue";
import ChatMessage from "./components/ChatMessage.vue";
import ControlPanel from "./components/ControlPanel.vue";

const STORAGE_KEY = "local-chat-state-v1";

const messages = ref([]);
const draft = ref("");
const availableModels = ref([]);
const selectedModel = ref("");
const systemPrompt = ref("");
const temperature = ref(0.7);
const maxTokens = ref(512);
const connectionState = ref("checking");
const isSending = ref(false);
const statusMessage = ref("Connecting to backend...");
const abortController = ref(null);

const hasMessages = computed(() => messages.value.length > 0);

function persistState() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      messages: messages.value,
      selectedModel: selectedModel.value,
      systemPrompt: systemPrompt.value,
      temperature: temperature.value,
      maxTokens: maxTokens.value,
    }),
  );
}

function loadState() {
  const raw = localStorage.getItem(STORAGE_KEY);
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    messages.value = Array.isArray(parsed.messages) ? parsed.messages : [];
    selectedModel.value = parsed.selectedModel || "";
    systemPrompt.value = parsed.systemPrompt || "";
    temperature.value = parsed.temperature ?? 0.7;
    maxTokens.value = parsed.maxTokens ?? 512;
  } catch {
    localStorage.removeItem(STORAGE_KEY);
  }
}

watch([messages, selectedModel, systemPrompt, temperature, maxTokens], persistState, { deep: true });

async function fetchConfig() {
  connectionState.value = "checking";
  try {
    const [configResponse, healthResponse] = await Promise.all([
      fetch("/api/config"),
      fetch("/api/health"),
    ]);

    if (!configResponse.ok || !healthResponse.ok) {
      throw new Error("Backend returned a non-200 response.");
    }

    const config = await configResponse.json();
    const health = await healthResponse.json();

    availableModels.value = config.available_models || [];
    if (!selectedModel.value) {
      selectedModel.value = config.default_model || config.available_models?.[0] || "";
    }

    connectionState.value = health.vllm_available ? "ready" : "degraded";
    statusMessage.value = health.vllm_available
      ? `Connected to ${config.vllm_base_url}`
      : "Backend is running, but vLLM is not ready.";
  } catch (error) {
    connectionState.value = "offline";
    statusMessage.value = error instanceof Error ? error.message : "Failed to reach backend.";
  }
}

function buildRequestMessages() {
  const outgoing = [];
  if (systemPrompt.value.trim()) {
    outgoing.push({ role: "system", content: systemPrompt.value.trim() });
  }
  return outgoing.concat(
    messages.value
      .filter(
        (message) =>
          typeof message?.content === "string" &&
          message.content.trim() &&
          ["system", "user", "assistant"].includes(message.role),
      )
      .map(({ role, content }) => ({ role, content: content.trim() })),
  );
}

function upsertAssistantMessage(content) {
  const last = messages.value[messages.value.length - 1];
  if (last?.role === "assistant") {
    last.content = content;
  } else {
    messages.value.push({ role: "assistant", content });
  }
}

function parseDelta(delta) {
  if (typeof delta?.content === "string") {
    return delta.content;
  }
  if (Array.isArray(delta?.content)) {
    return delta.content
      .map((item) => item?.text ?? "")
      .join("");
  }
  return "";
}

async function parseErrorResponse(response) {
  const raw = await response.text();
  try {
    const parsed = JSON.parse(raw);
    return parsed.detail || raw || `Request failed with status ${response.status}.`;
  } catch {
    return raw || `Request failed with status ${response.status}.`;
  }
}

async function sendNonStreamingFallback(payload) {
  const response = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(await parseErrorResponse(response));
  }

  const parsed = await response.json();
  upsertAssistantMessage(parsed.content || "No content returned.");
}

async function sendMessage() {
  if (isSending.value || !draft.value.trim()) return;
  if (!selectedModel.value) {
    statusMessage.value = "No model is available from the backend.";
    return;
  }

  const userContent = draft.value.trim();
  draft.value = "";
  messages.value.push({ role: "user", content: userContent });
  isSending.value = true;
  statusMessage.value = "Generating...";

  const controller = new AbortController();
  abortController.value = controller;
  let assistantContent = "";
  const payload = {
    model: selectedModel.value,
    messages: buildRequestMessages(),
    temperature: temperature.value,
    max_tokens: maxTokens.value,
  };

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (!response.ok || !response.body) {
      throw new Error(await parseErrorResponse(response));
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";

      for (const event of events) {
        const line = event
          .split("\n")
          .find((item) => item.startsWith("data: "));
        if (!line) continue;

        const payload = line.slice(6);
        if (payload === "[DONE]") {
          statusMessage.value = "Ready.";
          continue;
        }

        const parsed = JSON.parse(payload);
        const delta = parsed.choices?.[0]?.delta;
        const contentDelta = parseDelta(delta);
        if (!contentDelta) continue;
        assistantContent += contentDelta;
        upsertAssistantMessage(assistantContent);
      }
    }

    statusMessage.value = "Ready.";
  } catch (error) {
    if (controller.signal.aborted) {
      statusMessage.value = "Generation stopped.";
    } else {
      try {
        await sendNonStreamingFallback(payload);
        statusMessage.value = "Ready. Stream failed, fallback response used.";
      } catch (fallbackError) {
        statusMessage.value =
          fallbackError instanceof Error ? fallbackError.message : "Request failed.";
        upsertAssistantMessage(
          assistantContent || "The request failed before a response was completed.",
        );
      }
    }
  } finally {
    isSending.value = false;
    abortController.value = null;
  }
}

function stopGeneration() {
  abortController.value?.abort();
}

function clearConversation() {
  messages.value = [];
  statusMessage.value = "Conversation cleared.";
}

onMounted(async () => {
  loadState();
  await fetchConfig();
});
</script>

<template>
  <div class="shell">
    <div class="shell-background"></div>
    <header class="topbar">
      <div>
        <p class="eyebrow">Local LLM Workbench</p>
        <h1>Chat against your local vLLM backend.</h1>
      </div>
      <div class="topbar-actions">
        <button class="ghost-button" type="button" @click="fetchConfig">Refresh backend</button>
        <button class="ghost-button" type="button" @click="clearConversation" :disabled="!hasMessages">
          Clear chat
        </button>
      </div>
    </header>

    <main class="workspace">
      <ControlPanel
        v-model="selectedModel"
        v-model:system-prompt="systemPrompt"
        v-model:temperature="temperature"
        v-model:max-tokens="maxTokens"
        :available-models="availableModels"
        :connection-state="connectionState"
      />

      <section class="chat-stage">
        <div class="status-bar">
          <span>{{ statusMessage }}</span>
          <span>{{ selectedModel || "No model selected" }}</span>
        </div>

        <div class="message-list">
          <div v-if="!hasMessages" class="empty-state">
            <p class="eyebrow">Ready</p>
            <h2>Ask the local model anything.</h2>
            <p>
              The UI talks to FastAPI, and FastAPI proxies requests to the vLLM service running on
              this machine.
            </p>
          </div>

          <ChatMessage
            v-for="(message, index) in messages"
            :key="`${message.role}-${index}`"
            :message="message"
          />
        </div>

        <form class="composer" @submit.prevent="sendMessage">
          <textarea
            v-model="draft"
            class="composer-input"
            placeholder="Send a message to your local model..."
            rows="4"
          />
          <div class="composer-actions">
            <button class="ghost-button" type="button" @click="stopGeneration" :disabled="!isSending">
              Stop
            </button>
            <button class="primary-button" type="submit" :disabled="isSending || !draft.trim()">
              {{ isSending ? "Generating..." : "Send" }}
            </button>
          </div>
        </form>
      </section>
    </main>
  </div>
</template>
