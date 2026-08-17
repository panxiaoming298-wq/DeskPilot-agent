<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { callMcpTool, getMcpAudit, listMcpServers, setMcpServerEnabled } from '../api'
import type { McpAuditEvent, McpServer, McpToolCallResult } from '../types'

const servers = ref<McpServer[]>([])
const audit = ref<McpAuditEvent[]>([])
const text = ref('DeskPilot MCP 只读文本')
const result = ref<McpToolCallResult | null>(null)
const busy = ref(false)
const message = ref<string | null>(null)
const server = computed(() => servers.value[0] ?? null)

onMounted(() => void refresh())

async function refresh(): Promise<void> {
  try {
    const [serverList, auditPage] = await Promise.all([listMcpServers(), getMcpAudit()])
    servers.value = serverList
    audit.value = auditPage.events
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取 MCP 控制面失败。'
  }
}

async function toggle(): Promise<void> {
  if (!server.value || busy.value) return
  busy.value = true
  message.value = null
  try {
    const mutation = await setMcpServerEnabled(server.value.server_id, !server.value.enabled)
    servers.value = [mutation.server]
    await refresh()
    message.value = mutation.server.enabled ? 'Server 已显式启用。' : 'Server 已禁用。'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '状态变更失败。'
  } finally {
    busy.value = false
  }
}

async function invoke(): Promise<void> {
  if (!server.value?.enabled || !text.value || busy.value) return
  busy.value = true
  message.value = null
  try {
    result.value = await callMcpTool(
      server.value.server_id,
      'deskpilot.text.metrics',
      { text: text.value },
    )
    await refresh()
  } catch (error) {
    message.value = error instanceof Error ? error.message : 'MCP 调用失败。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="mcp-page" aria-labelledby="mcp-heading">
    <article v-if="server" class="panel">
      <span class="eyebrow">CONTROLLED STDIO CONNECTION</span>
      <div class="title-row">
        <div>
          <h2 id="mcp-heading">{{ server.title }}</h2>
          <p>{{ server.server_id }} · MCP {{ server.protocol_version }}</p>
        </div>
        <span class="status" :class="{ enabled: server.enabled }">
          {{ server.enabled ? '已启用' : '默认禁用' }}
        </span>
      </div>
      <dl>
        <div><dt>固定命令</dt><dd><code>{{ server.command_preview.join(' ') }}</code></dd></div>
        <div><dt>网络</dt><dd>{{ server.network_access ? '允许' : 'manifest 禁止（未强沙箱）' }}</dd></div>
        <div><dt>文件根</dt><dd>{{ server.filesystem_roots.length ? server.filesystem_roots.join(', ') : '无' }}</dd></div>
        <div><dt>客户端能力</dt><dd>{{ server.client_capabilities.length ? server.client_capabilities.join(', ') : '不提供 roots / sampling / elicitation' }}</dd></div>
        <div><dt>内置包摘要</dt><dd><code>{{ server.bundle_digest.slice(0, 16) }}</code></dd></div>
      </dl>
      <div class="tool" v-for="tool in server.tools" :key="tool.name">
        <strong>{{ tool.title }}</strong><span>{{ tool.risk_floor }} 本地下限</span>
        <p>{{ tool.description }}</p>
        <small>{{ tool.name }} · schema {{ tool.schema_digest.slice(0, 12) }}</small>
      </div>
      <button type="button" :disabled="busy" @click="toggle">
        {{ server.enabled ? '禁用 Server' : '审阅后启用 Server' }}
      </button>
      <p v-if="message" role="status" class="message">{{ message }}</p>
    </article>

    <article v-if="server" class="panel">
      <h3>显式调用只读 Tool</h3>
      <p class="hint">点击调用即同意把下方文本发送给这个本地短生命周期进程；内容不会写入审计。</p>
      <textarea v-model="text" maxlength="4096" aria-label="MCP 文本输入" />
      <button type="button" :disabled="busy || !server.enabled || !text" @click="invoke">计算本地指标</button>
      <pre v-if="result">{{ JSON.stringify(result.structured_content, null, 2) }}</pre>
    </article>

    <article class="panel">
      <h3>脱敏审计</h3>
      <ol v-if="audit.length" class="audit">
        <li v-for="event in audit" :key="event.event_id">
          <strong>#{{ event.sequence }} {{ event.action }}</strong>
          <code>{{ event.event_digest.slice(0, 16) }}</code>
        </li>
      </ol>
      <p v-else class="hint">尚无 MCP 控制面事件。</p>
    </article>
  </section>
</template>

<style scoped>
.mcp-page { display: grid; gap: 18px; padding: 26px; }
.panel { padding: 22px; border: 1px solid rgba(151, 166, 199, .16); border-radius: 18px; background: rgba(13, 19, 35, .78); }
.title-row { display: flex; justify-content: space-between; gap: 16px; }
h2 { margin: 6px 0; } p { color: #8b96ad; }
.status { height: fit-content; padding: 6px 10px; border-radius: 999px; background: #2b3348; }
.status.enabled { background: rgba(19, 206, 159, .18); color: #58e0b8; }
dl { display: grid; gap: 8px; } dl div { display: grid; grid-template-columns: 120px 1fr; } dt { color: #77839e; } dd { margin: 0; overflow-wrap: anywhere; }
.tool { display: grid; grid-template-columns: 1fr auto; gap: 5px 12px; margin: 18px 0; padding: 14px; border: 1px solid rgba(151, 166, 199, .14); border-radius: 12px; }
.tool p, .tool small { grid-column: 1 / -1; margin: 0; color: #8b96ad; }
button { margin-top: 12px; padding: 10px 15px; border: 0; border-radius: 10px; background: #527cff; color: #fff; font-weight: 700; }
button:disabled { opacity: .45; } textarea { width: 100%; min-height: 110px; padding: 12px; border: 1px solid rgba(151, 166, 199, .2); border-radius: 10px; background: #0b1120; color: inherit; resize: vertical; }
pre { overflow: auto; padding: 14px; border-radius: 10px; background: #070b14; } .message { color: #f6c85f; }
.audit { display: grid; gap: 8px; padding: 0; list-style: none; } .audit li { display: flex; justify-content: space-between; padding: 10px; border-bottom: 1px solid rgba(151, 166, 199, .12); }
@media (max-width: 760px) { .title-row { flex-direction: column; } dl div { grid-template-columns: 1fr; } }
</style>
