<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  importKnowledgeSource,
  listKnowledgeSources,
  searchKnowledge,
} from '../api'
import type { KnowledgeSearchResult, KnowledgeSource } from '../types'

const sourcePath = ref('')
const query = ref('')
const sources = ref<KnowledgeSource[]>([])
const result = ref<KnowledgeSearchResult | null>(null)
const busy = ref(false)
const message = ref<string | null>(null)

onMounted(() => void refreshSources())

async function refreshSources(): Promise<void> {
  try {
    sources.value = await listKnowledgeSources()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取知识源失败。'
  }
}

async function importSource(): Promise<void> {
  const path = sourcePath.value.trim()
  if (!path || busy.value) return
  busy.value = true
  message.value = null
  try {
    const imported = await importKnowledgeSource(path)
    await refreshSources()
    message.value = `已导入 ${imported.chunk_count} 个可验证分块。`
  } catch (error) {
    message.value = error instanceof Error ? error.message : '导入失败。'
  } finally {
    busy.value = false
  }
}

async function runSearch(): Promise<void> {
  const text = query.value.trim()
  if (!text || busy.value) return
  busy.value = true
  message.value = null
  try {
    result.value = await searchKnowledge(text)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '检索失败。'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="knowledge" aria-labelledby="knowledge-heading">
    <article class="panel intro">
      <span class="eyebrow">CONTENT-ADDRESSED LOCAL MEMORY</span>
      <h2 id="knowledge-heading">本地知识库</h2>
      <p>显式导入单个 UTF-8 Markdown 或文本文件。源文件保持只读，命中结果携带行号与完整性证明。</p>
      <form class="row" @submit.prevent="importSource">
        <label>
          文件绝对路径
          <input v-model="sourcePath" name="source-path" placeholder="D:\docs\runbook.md" />
        </label>
        <button type="submit" :disabled="busy || !sourcePath.trim()">导入知识源</button>
      </form>
      <p v-if="message" class="message" role="status">{{ message }}</p>
    </article>

    <article class="panel">
      <h3>已登记来源</h3>
      <ul v-if="sources.length" class="source-list">
        <li v-for="source in sources" :key="source.source_id">
          <strong>{{ source.canonical_path }}</strong>
          <small>{{ source.chunk_count }} 块 · {{ source.content_digest.slice(0, 12) }}</small>
        </li>
      </ul>
      <p v-else class="empty">尚未导入知识源。</p>
    </article>

    <article class="panel search-panel">
      <form class="row" @submit.prevent="runSearch">
        <label>
          本地关键词检索
          <input v-model="query" name="knowledge-query" placeholder="输入要核对的事实或步骤" />
        </label>
        <button type="submit" :disabled="busy || !query.trim()">检索</button>
      </form>
      <p v-if="result?.stale_source_ids.length" class="warning" role="alert">
        {{ result.stale_source_ids.length }} 个来源已变更，旧分块已停止返回，请重新导入。
      </p>
      <ol v-if="result?.citations.length" class="results">
        <li v-for="citation in result.citations" :key="citation.chunk_id">
          <div><strong>{{ citation.locator }}</strong><small>{{ citation.canonical_path }}</small></div>
          <p>{{ citation.snippet }}</p>
          <code>proof {{ citation.retrieval_proof_digest.slice(0, 16) }}</code>
        </li>
      </ol>
      <p v-else-if="result" class="empty">没有通过来源版本和证据校验的命中。</p>
    </article>
  </section>
</template>

<style scoped>
.knowledge { display: grid; gap: 18px; padding: 26px; }
.panel { padding: 22px; border: 1px solid rgba(151, 166, 199, .16); border-radius: 18px; background: rgba(13, 19, 35, .78); }
.intro h2 { margin: 6px 0; }
.intro p { color: #8b96ad; }
.row { display: flex; align-items: end; gap: 12px; margin-top: 18px; }
label { display: grid; flex: 1; gap: 7px; font-size: 13px; color: #8b96ad; }
input { min-height: 42px; padding: 0 12px; border: 1px solid rgba(151, 166, 199, .2); border-radius: 10px; background: #0b1120; color: inherit; }
button { min-height: 42px; padding: 0 16px; border: 0; border-radius: 10px; background: #527cff; color: #fff; font-weight: 700; }
button:disabled { opacity: .45; }
.source-list, .results { display: grid; gap: 10px; padding: 0; list-style: none; }
.source-list li, .results li { display: grid; gap: 6px; padding: 14px; border: 1px solid rgba(151, 166, 199, .16); border-radius: 12px; }
.source-list small, .results small { display: block; color: #8b96ad; overflow-wrap: anywhere; }
.results p { margin: 0; white-space: pre-wrap; }
code { color: #8b96ad; }
.message, .warning { color: #f6c85f; }
.empty { color: #8b96ad; }
@media (max-width: 760px) { .row { align-items: stretch; flex-direction: column; } }
</style>
