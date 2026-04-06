<script setup>
import { computed, onMounted, ref, watch } from "vue";
import ChatMessage from "./components/ChatMessage.vue";
import ControlPanel from "./components/ControlPanel.vue";

const STORAGE_KEY = "local-chat-state-v1";
const IMAGE_SIZE_LIMIT = 8 * 1024 * 1024;
const IMAGE_RELOAD_PLACEHOLDER = "[Image omitted after reload]";

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
const isAuthenticated = ref(false);
const isAuthenticating = ref(false);
const authUser = ref("");
const loginUsername = ref("");
const loginPassword = ref("");
const loginError = ref("");
const selectedImageDataUrl = ref("");
const selectedImageName = ref("");
const composerFileInput = ref(null);

const hasMessages = computed(() => messages.value.length > 0);
const hasPendingInput = computed(() => Boolean(draft.value.trim() || selectedImageDataUrl.value));

function sanitizeTemperature(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 0.7;
  return Math.min(2, Math.max(0, numeric));
}

function sanitizeMaxTokens(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 512;
  return Math.min(4096, Math.max(1, Math.round(numeric)));
}

function normalizeContentPart(part) {
  if (!part || typeof part !== "object") return null;

  if (part.type === "text" && typeof part.text === "string" && part.text.trim()) {
    return { type: "text", text: part.text.trim() };
  }

  if (
    part.type === "image_url" &&
    part.image_url &&
    typeof part.image_url.url === "string" &&
    part.image_url.url
  ) {
    return {
      type: "image_url",
      image_url: { url: part.image_url.url },
    };
  }

  return null;
}

function normalizeStoredMessage(message) {
  if (!message || typeof message !== "object") return null;
  if (!["system", "user", "assistant"].includes(message.role)) return null;

  if (typeof message.content === "string" && message.content.trim()) {
    return { role: message.role, content: message.content };
  }

  if (Array.isArray(message.content)) {
    const parts = message.content.map(normalizeContentPart).filter(Boolean);
    if (parts.length) {
      return { role: message.role, content: parts };
    }
  }

  return null;
}

function serializeContentForStorage(content) {
  if (typeof content === "string") {
    return content;
  }

  if (!Array.isArray(content)) {
    return "";
  }

  return content
    .map((part) => {
      if (part?.type === "text" && typeof part.text === "string") {
        return part.text;
      }
      if (part?.type === "image_url") {
        return IMAGE_RELOAD_PLACEHOLDER;
      }
      return "";
    })
    .filter(Boolean)
    .join("\n\n");
}

function persistState() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      messages: messages.value.map((message) => ({
        role: message.role,
        content: serializeContentForStorage(message.content),
      })),
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
    messages.value = Array.isArray(parsed.messages)
      ? parsed.messages.map(normalizeStoredMessage).filter(Boolean)
      : [];
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

    if (configResponse.status === 401 || healthResponse.status === 401) {
      handleAuthExpired();
      return;
    }

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

async function fetchSession() {
  const response = await fetch("/api/auth/session");
  if (!response.ok) {
    throw new Error("Failed to check login status.");
  }
  const session = await response.json();
  isAuthenticated.value = !!session.authenticated;
  authUser.value = session.username || "";
  return session;
}

function clearPendingImage() {
  selectedImageDataUrl.value = "";
  selectedImageName.value = "";
  if (composerFileInput.value) {
    composerFileInput.value.value = "";
  }
}

function handleAuthExpired(message = "Session expired. Sign in again.") {
  isAuthenticated.value = false;
  authUser.value = "";
  loginPassword.value = "";
  loginError.value = "";
  connectionState.value = "offline";
  statusMessage.value = message;
  abortController.value?.abort();
}

async function submitLogin() {
  if (isAuthenticating.value) return;
  loginError.value = "";
  isAuthenticating.value = true;

  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: loginUsername.value.trim(),
        password: loginPassword.value,
      }),
    });

    if (!response.ok) {
      throw new Error(await parseErrorResponse(response));
    }

    const session = await response.json();
    isAuthenticated.value = !!session.authenticated;
    authUser.value = session.username || "";
    loginPassword.value = "";
    statusMessage.value = "Signed in.";
    await fetchConfig();
  } catch (error) {
    loginError.value = error instanceof Error ? error.message : "Login failed.";
  } finally {
    isAuthenticating.value = false;
  }
}

async function logout() {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
    });
  } finally {
    handleAuthExpired("Signed out.");
  }
}

function buildRequestMessages() {
  const outgoing = [];
  if (systemPrompt.value.trim()) {
    outgoing.push({ role: "system", content: systemPrompt.value.trim() });
  }

  return outgoing.concat(
    messages.value
      .map((message) => {
        if (typeof message?.content === "string" && message.content.trim()) {
          return { role: message.role, content: message.content.trim() };
        }

        if (Array.isArray(message?.content)) {
          const content = message.content.map(normalizeContentPart).filter(Boolean);
          if (content.length) {
            return { role: message.role, content };
          }
        }

        return null;
      })
      .filter(Boolean),
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

  if (response.status === 401) {
    handleAuthExpired();
    throw new Error("Authentication required.");
  }

  if (!response.ok) {
    throw new Error(await parseErrorResponse(response));
  }

  const parsed = await response.json();
  upsertAssistantMessage(parsed.content || "No content returned.");
}

function buildUserMessageContent(text, imageUrl) {
  if (!imageUrl) {
    return text;
  }

  const content = [];
  if (text) {
    content.push({ type: "text", text });
  }
  content.push({
    type: "image_url",
    image_url: { url: imageUrl },
  });
  return content;
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("Failed to read image."));
    reader.readAsDataURL(file);
  });
}

async function handleImageSelection(event) {
  const file = event.target.files?.[0];
  if (!file) return;

  try {
    if (!file.type.startsWith("image/")) {
      throw new Error("Please upload an image file.");
    }
    if (file.size > IMAGE_SIZE_LIMIT) {
      throw new Error("Image must be 8 MB or smaller.");
    }

    selectedImageDataUrl.value = await readFileAsDataUrl(file);
    selectedImageName.value = file.name;
    statusMessage.value = `Attached image: ${file.name}`;
  } catch (error) {
    clearPendingImage();
    statusMessage.value = error instanceof Error ? error.message : "Failed to load image.";
  }
}

async function sendMessage() {
  if (isSending.value || !hasPendingInput.value || !isAuthenticated.value) return;
  if (!selectedModel.value) {
    statusMessage.value = "No model is available from the backend.";
    return;
  }

  const userText = draft.value.trim();
  const imageUrl = selectedImageDataUrl.value;
  const userMessageContent = buildUserMessageContent(userText, imageUrl);

  draft.value = "";
  clearPendingImage();
  messages.value.push({ role: "user", content: userMessageContent });
  isSending.value = true;
  statusMessage.value = imageUrl ? "Analyzing image..." : "Generating...";

  const controller = new AbortController();
  abortController.value = controller;
  let assistantContent = "";
  temperature.value = sanitizeTemperature(temperature.value);
  maxTokens.value = sanitizeMaxTokens(maxTokens.value);
  const payload = {
    model: selectedModel.value,
    messages: buildRequestMessages(),
    temperature: sanitizeTemperature(temperature.value),
    max_tokens: sanitizeMaxTokens(maxTokens.value),
  };

  try {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });

    if (response.status === 401) {
      handleAuthExpired();
      return;
    }

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

        const eventPayload = line.slice(6);
        if (eventPayload === "[DONE]") {
          statusMessage.value = "Ready.";
          continue;
        }

        const parsed = JSON.parse(eventPayload);
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
  clearPendingImage();
  statusMessage.value = "Conversation cleared.";
}

onMounted(async () => {
  loadState();
  try {
    const session = await fetchSession();
    if (session.authenticated) {
      await fetchConfig();
    } else {
      statusMessage.value = "Sign in to access the local model.";
    }
  } catch (error) {
    statusMessage.value = error instanceof Error ? error.message : "Failed to reach backend.";
  }
});
</script>

<template>
  <div class="shell" :class="{ 'shell-login': !isAuthenticated }">
    <div class="shell-background"></div>
    <template v-if="!isAuthenticated">
      <main class="login-shell">
        <section class="login-card">
          <p class="eyebrow">Private Access</p>
          <h1>Sign in to Local Chat</h1>
          <p class="login-copy">
            This workspace is protected before it can reach your FastAPI and vLLM backend.
          </p>

          <form class="login-form" @submit.prevent="submitLogin">
            <label class="field-label" for="loginUsername">Username</label>
            <input
              id="loginUsername"
              v-model="loginUsername"
              class="field-input"
              type="text"
              autocomplete="username"
              placeholder="Enter username"
            />

            <label class="field-label" for="loginPassword">Password</label>
            <input
              id="loginPassword"
              v-model="loginPassword"
              class="field-input"
              type="password"
              autocomplete="current-password"
              placeholder="Enter password"
            />

            <p class="login-status">{{ loginError || statusMessage }}</p>

            <button
              class="primary-button login-button"
              type="submit"
              :disabled="isAuthenticating || !loginUsername.trim() || !loginPassword"
            >
              {{ isAuthenticating ? "Signing in..." : "Sign in" }}
            </button>
          </form>
        </section>
      </main>
    </template>

    <template v-else>
      <header class="topbar">
        <div>
          <p class="eyebrow">Local LLM Workbench</p>
          <h1>DHU SIIS leisgroup local LLM</h1>
        </div>
        <div class="topbar-actions">
          <span class="session-badge">{{ authUser }}</span>
          <button class="ghost-button" type="button" @click="fetchConfig">Refresh backend</button>
          <button class="ghost-button" type="button" @click="clearConversation" :disabled="!hasMessages">
            Clear chat
          </button>
          <button class="ghost-button" type="button" @click="logout">Sign out</button>
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

            <div class="composer-toolbar">
              <label class="ghost-button upload-button" for="composerImage">Upload image</label>
              <input
                id="composerImage"
                ref="composerFileInput"
                class="hidden-file-input"
                type="file"
                accept="image/*"
                @change="handleImageSelection"
              />
              <span class="upload-hint">
                {{ selectedImageName || "Single image, up to 8 MB, sent inline to vLLM." }}
              </span>
            </div>

            <div v-if="selectedImageDataUrl" class="image-attachment">
              <img :src="selectedImageDataUrl" :alt="selectedImageName || 'Selected image'" class="attachment-preview" />
              <div class="attachment-meta">
                <strong>{{ selectedImageName || "Attached image" }}</strong>
                <span>Image will be sent with your next prompt.</span>
              </div>
              <button class="ghost-button" type="button" @click="clearPendingImage">Remove</button>
            </div>

            <div class="composer-actions">
              <button class="ghost-button" type="button" @click="stopGeneration" :disabled="!isSending">
                Stop
              </button>
              <button class="primary-button" type="submit" :disabled="isSending || !hasPendingInput">
                {{ isSending ? "Generating..." : "Send" }}
              </button>
            </div>
          </form>
        </section>
      </main>
    </template>
  </div>
</template>
