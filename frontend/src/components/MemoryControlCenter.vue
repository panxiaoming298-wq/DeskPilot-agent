<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  confirmMemoryProposal,
  createLongTermMemory,
  deleteLongTermMemory,
  editLongTermMemory,
  exportLongTermMemory,
  getLongTermMemory,
  rejectMemoryProposal,
  resolveMemoryConflict,
} from '../api'
import type {
  LongTermMemoryItem,
  LongTermMemoryKind,
  LongTermMemoryPage,
  LongTermMemoryStatus,
  MemoryProposal,
} from '../types'

type Filter = 'all' | 'active' | 'pending_confirmation' | 'conflict' | 'deleted'

const page = ref<LongTermMemoryPage>({ items: [], proposals: [], conflicts: [], usage: [] })
const loading = ref(true)
const busy = ref(false)
const message = ref<string | null>(null)
const error = ref<string | null>(null)
const query = ref('')
const filter = ref<Filter>('all')
const selectedId = ref<string | null>(null)
const showCreate = ref(false)
const editMode = ref(false)
const deleteArmed = ref(false)
const editValue = ref('')
const editClassification = ref<'public' | 'internal' | 'sensitive'>('internal')
const draft = ref({
  key: '',
  kind: 'preference' as LongTermMemoryKind,
  value: '',
  classification: 'internal' as 'public' | 'internal' | 'sensitive',
  expiresAt: '',
  deliveryId: '',
})

const pendingProposals = computed(() => page.value.proposals.filter(
  (item) => item.status === 'pending_confirmation',
))

const filteredItems = computed(() => {
  const text = query.value.trim().toLowerCase()
  return page.value.items.filter((item) => {
    const matchesFilter = filter.value === 'all' || item.status === filter.value
    const matchesText = !text || `${item.key} ${item.value ?? ''} ${item.source_id}`
      .toLowerCase()
      .includes(text)
    return matchesFilter && matchesText
  })
})

const selectedItem = computed(() => page.value.items.find(
  (item) => item.memory_id === selectedId.value,
) ?? null)

const selectedProposal = computed(() => page.value.proposals.find(
  (item) => item.proposal_id === selectedId.value,
) ?? null)

const selectedConflict = computed(() => {
  if (!selectedItem.value) return null
  return page.value.conflicts.find(
    (item) => item.status === 'open' && item.memory_ids.includes(selectedItem.value!.memory_id),
  ) ?? null
})

const selectedUsage = computed(() => selectedItem.value
  ? page.value.usage.filter((item) => item.memory_id === selectedItem.value!.memory_id)
  : [])

const requiresDelivery = computed(() => ['verified_episode', 'skill_template'].includes(draft.value.kind))

const statusLabels: Record<LongTermMemoryStatus, string> = {
  proposal: '提案',
  pending_confirmation: '待确认',
  confirmed: '已确认',
  active: '生效',
  conflict: '冲突',
  expired: '已过期',
  deleted: '已删除',
  rejected: '已拒绝',
}

const kindLabels: Record<LongTermMemoryKind, string> = {
  preference: '偏好',
  restrictive_permission: '收紧型限制',
  user_confirmed_fact: '用户确认事实',
  verified_episode: '已验证经历',
  skill_template: '技能模板',
}

onMounted(() => void refresh())

function applyPage(next: LongTermMemoryPage, preferredId?: string): void {
  page.value = next
  const candidate = preferredId ?? selectedId.value
  if (candidate && (
    next.items.some((item) => item.memory_id === candidate)
    || next.proposals.some((item) => item.proposal_id === candidate)
  )) {
    selectedId.value = candidate
  } else {
    selectedId.value = next.items[0]?.memory_id ?? next.proposals[0]?.proposal_id ?? null
  }
}

async function refresh(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    applyPage(await getLongTermMemory())
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '读取长期记忆失败。'
  } finally {
    loading.value = false
  }
}

async function run(action: () => Promise<LongTermMemoryPage>, success: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = null
  message.value = null
  try {
    applyPage(await action())
    message.value = success
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '长期记忆操作失败。'
  } finally {
    busy.value = false
  }
}

async function createMemory(): Promise<void> {
  const data = draft.value
  await run(() => createLongTermMemory({
    key: data.key.trim(),
    kind: data.kind,
    value: data.value.trim(),
    classification: data.classification,
    ...(data.expiresAt ? { expires_at: new Date(data.expiresAt).toISOString() } : {}),
    ...(data.deliveryId.trim() ? { verified_delivery_id: data.deliveryId.trim() } : {}),
  }), data.kind === 'user_confirmed_fact' || data.kind === 'verified_episode'
    ? '提案已进入待确认区。'
    : '记忆已写入；如有同键异值，它会进入冲突状态。')
  if (!error.value) {
    showCreate.value = false
    draft.value = {
      key: '', kind: 'preference', value: '', classification: 'internal', expiresAt: '', deliveryId: '',
    }
  }
}

function selectItem(item: LongTermMemoryItem): void {
  selectedId.value = item.memory_id
  editMode.value = false
  deleteArmed.value = false
}

function selectProposal(item: MemoryProposal): void {
  selectedId.value = item.proposal_id
  editMode.value = false
  deleteArmed.value = false
}

function beginEdit(): void {
  if (!selectedItem.value?.value) return
  editValue.value = selectedItem.value.value
  editClassification.value = selectedItem.value.classification
  editMode.value = true
}

async function saveEdit(): Promise<void> {
  const item = selectedItem.value
  if (!item) return
  await run(
    () => editLongTermMemory(item.memory_id, editValue.value.trim(), editClassification.value),
    '修订已创建新版本，旧版本只保留墓碑。',
  )
  editMode.value = false
}

async function deleteSelected(): Promise<void> {
  const item = selectedItem.value
  if (!item) return
  if (!deleteArmed.value) {
    deleteArmed.value = true
    return
  }
  await run(() => deleteLongTermMemory(item.memory_id), '记忆已删除，不再进入后续 Context。')
  deleteArmed.value = false
}

async function downloadExport(): Promise<void> {
  if (busy.value) return
  busy.value = true
  try {
    const data = await exportLongTermMemory()
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `deskpilot-memory-${data.export_digest.slice(0, 12)}.json`
    anchor.click()
    URL.revokeObjectURL(url)
    message.value = '已导出含完整性摘要的记忆账本。'
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '导出失败。'
  } finally {
    busy.value = false
  }
}

function formatTime(value: string | null): string {
  return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(value)) : '不设期限'
}
</script>

<template>
  <section class="memory-control" aria-labelledby="memory-heading">
    <header class="memory-toolbar">
      <div>
        <h2 id="memory-heading">长期记忆账本</h2>
        <p>只有生效且无冲突的条目会进入 Context。记忆不能放宽 Policy。</p>
      </div>
      <div class="toolbar-actions">
        <button class="quiet-button" type="button" :disabled="busy" @click="downloadExport">导出账本</button>
        <button class="accent-button" type="button" @click="showCreate = !showCreate">
          {{ showCreate ? '收起新建' : '新建记忆' }}
        </button>
      </div>
    </header>

    <form v-if="showCreate" class="memory-create" @submit.prevent="createMemory">
      <label>记忆键<input v-model="draft.key" name="memory-key" required pattern="[a-z][a-z0-9_.-]*" placeholder="response.language"></label>
      <label>类型<select v-model="draft.kind" name="memory-kind">
        <option v-for="(label, value) in kindLabels" :key="value" :value="value">{{ label }}</option>
      </select></label>
      <label>数据级别<select v-model="draft.classification" name="memory-classification">
        <option value="public">公开</option><option value="internal">内部</option><option value="sensitive">敏感</option>
      </select></label>
      <label>到期时间<input v-model="draft.expiresAt" name="memory-expiry" type="datetime-local"></label>
      <label v-if="requiresDelivery" class="wide-field">已验证 Delivery ID<input v-model="draft.deliveryId" required name="delivery-id" placeholder="dlv_..."></label>
      <label class="wide-field">内容<textarea v-model="draft.value" name="memory-value" required rows="3" maxlength="4000" placeholder="写清希望系统记住的内容"></textarea></label>
      <div class="create-consequence wide-field">
        <p v-if="draft.kind === 'restrictive_permission'">该类型只增加限制，不会修改或放宽审批与权限 Policy。</p>
        <p v-else-if="['user_confirmed_fact', 'verified_episode'].includes(draft.kind)">保存后只形成待确认提案，不会立即进入模型 Context。</p>
        <p v-else>保存后可能立即生效；同键异值会冻结为可见冲突。</p>
        <button class="accent-button" type="submit" :disabled="busy || !draft.key.trim() || !draft.value.trim()">保存记忆</button>
      </div>
    </form>

    <p v-if="message" class="memory-message" role="status">{{ message }}</p>
    <p v-if="error" class="memory-error" role="alert">{{ error }}</p>

    <div v-if="loading" class="memory-state" aria-live="polite">正在核对加密账本…</div>
    <div v-else class="memory-layout">
      <aside class="memory-ledger" aria-label="长期记忆清单">
        <input v-model="query" class="memory-search" type="search" placeholder="搜索键、内容或来源" aria-label="搜索长期记忆">
        <div class="memory-filters" role="tablist" aria-label="记忆状态">
          <button v-for="item in (['all', 'active', 'pending_confirmation', 'conflict', 'deleted'] as Filter[])" :key="item" type="button" :class="{ active: filter === item }" @click="filter = item">
            {{ item === 'all' ? '全部' : statusLabels[item] }}
          </button>
        </div>

        <section v-if="pendingProposals.length && (filter === 'all' || filter === 'pending_confirmation')" class="ledger-group">
          <h3>等待你的决定 <span>{{ pendingProposals.length }}</span></h3>
          <button v-for="proposal in pendingProposals" :key="proposal.proposal_id" class="ledger-row" :class="{ selected: selectedId === proposal.proposal_id }" type="button" @click="selectProposal(proposal)">
            <span class="row-main"><strong>{{ proposal.key }}</strong><small>{{ kindLabels[proposal.kind] }} · {{ proposal.source_type }}</small></span>
            <span class="status-chip" data-status="pending_confirmation">待确认</span>
          </button>
        </section>

        <section class="ledger-group">
          <h3>版本账本 <span>{{ filteredItems.length }}</span></h3>
          <button v-for="item in filteredItems" :key="item.memory_id" class="ledger-row" :class="{ selected: selectedId === item.memory_id }" type="button" @click="selectItem(item)">
            <span class="row-main"><strong>{{ item.key }}</strong><small>v{{ item.version }} · {{ kindLabels[item.kind] }}</small></span>
            <span class="status-chip" :data-status="item.status">{{ statusLabels[item.status] }}</span>
          </button>
          <p v-if="!filteredItems.length" class="ledger-empty">当前筛选下没有记忆。</p>
        </section>
      </aside>

      <article class="memory-detail">
        <template v-if="selectedProposal">
          <div class="detail-heading"><div><small>待确认提案</small><h3>{{ selectedProposal.key }}</h3></div><span class="status-chip" data-status="pending_confirmation">待确认</span></div>
          <p class="memory-value">{{ selectedProposal.value ?? '内容已清除' }}</p>
          <dl class="evidence-grid">
            <div><dt>提议者</dt><dd>{{ selectedProposal.created_by }}</dd></div>
            <div><dt>来源</dt><dd>{{ selectedProposal.source_type }} / {{ selectedProposal.source_id }}</dd></div>
            <div><dt>置信度</dt><dd>{{ Math.round(selectedProposal.confidence * 100) }}%</dd></div>
            <div><dt>有效期</dt><dd>{{ formatTime(selectedProposal.expires_at) }}</dd></div>
          </dl>
          <code class="digest-line">proposal {{ selectedProposal.proposal_digest }}</code>
          <div class="decision-block">
            <p>确认会创建可召回版本；拒绝会清除提案密文，且不能恢复。</p>
            <div><button class="accent-button" type="button" :disabled="busy" @click="run(() => confirmMemoryProposal(selectedProposal!.proposal_id), '提案已确认。')">确认并保存</button><button class="danger-button" type="button" :disabled="busy" @click="run(() => rejectMemoryProposal(selectedProposal!.proposal_id), '提案已拒绝并清除。')">拒绝并清除</button></div>
          </div>
        </template>

        <template v-else-if="selectedItem">
          <div class="detail-heading"><div><small>{{ kindLabels[selectedItem.kind] }} · 版本 {{ selectedItem.version }}</small><h3>{{ selectedItem.key }}</h3></div><span class="status-chip" :data-status="selectedItem.status">{{ statusLabels[selectedItem.status] }}</span></div>
          <p v-if="!editMode" class="memory-value">{{ selectedItem.value ?? '该版本的受保护内容已清除。' }}</p>
          <form v-else class="edit-form" @submit.prevent="saveEdit">
            <label>修订内容<textarea v-model="editValue" required rows="5"></textarea></label>
            <label>数据级别<select v-model="editClassification"><option value="public">公开</option><option value="internal">内部</option><option value="sensitive">敏感</option></select></label>
            <p>保存会创建新版本，并删除当前版本的密文。</p>
            <div><button class="accent-button" type="submit" :disabled="busy || !editValue.trim()">保存新版本</button><button class="quiet-button" type="button" @click="editMode = false">取消</button></div>
          </form>
          <dl class="evidence-grid">
            <div><dt>为什么记住</dt><dd>{{ selectedItem.source_type }} / {{ selectedItem.source_id }}</dd></div>
            <div><dt>写入者</dt><dd>{{ selectedItem.created_by }}</dd></div>
            <div><dt>数据级别</dt><dd>{{ selectedItem.classification }}</dd></div>
            <div><dt>有效期</dt><dd>{{ formatTime(selectedItem.expires_at) }}</dd></div>
          </dl>
          <code class="digest-line">item {{ selectedItem.item_digest }}</code>

          <section v-if="selectedConflict" class="conflict-box">
            <h4>需要解决的冲突</h4>
            <p>冲突期间，这些版本都不会进入 Context。选择一个版本后，其余版本会删除密文并留下墓碑。</p>
            <button v-for="memoryId in selectedConflict.memory_ids" :key="memoryId" type="button" :disabled="busy" @click="run(() => resolveMemoryConflict(selectedConflict!.conflict_id, memoryId), '冲突已由你的选择解决。')">
              保留 {{ page.items.find((item) => item.memory_id === memoryId)?.value ?? memoryId.slice(0, 16) }}
            </button>
          </section>

          <section class="usage-ledger">
            <h4>实际使用记录 <span>{{ selectedUsage.length }}</span></h4>
            <div v-for="usage in selectedUsage" :key="usage.usage_id" class="usage-row">
              <strong>{{ usage.agent_id }}</strong><span>{{ usage.provider_id }} · {{ usage.provider_location }}</span><small>{{ formatTime(usage.supplied_at) }} · {{ usage.task_id }}</small>
            </div>
            <p v-if="!selectedUsage.length">还没有发送给任何 Agent 或 Provider。</p>
            <small v-if="selectedItem.deleted_at && selectedUsage.length" class="history-warning">删除会阻止未来使用，但不能撤回已经发送过的历史 Context。</small>
          </section>

          <div v-if="!['deleted', 'expired'].includes(selectedItem.status) && !editMode" class="detail-actions">
            <button class="quiet-button" type="button" :disabled="busy" @click="beginEdit">修订版本</button>
            <button class="danger-button" type="button" :disabled="busy" @click="deleteSelected">{{ deleteArmed ? '再次点击，确认删除' : '删除记忆' }}</button>
            <p v-if="deleteArmed">删除会立即阻止未来召回，密文会清除，只保留无明文墓碑。</p>
          </div>
        </template>
        <div v-else class="detail-empty"><h3>选择一条记忆</h3><p>这里会显示来源、版本、有效期和真实使用去向。</p></div>
      </article>
    </div>
  </section>
</template>

<style scoped>
/* finesse · register=product-workflow · A=cryogenic-steel+signal-cyan · B=condensed-command+mono-evidence · C=ledger+lineage-detail · D=state-feedback-only · E=sealed-evidence-dossier · SOUL=7 SPECTACLE=2 DENSITY=9 */
.memory-control {
  --mem-surface: rgba(10, 17, 20, .92);
  --mem-raised: rgba(16, 27, 31, .88);
  --mem-selected: rgba(85, 223, 207, .1);
  --mem-line: rgba(109, 230, 218, .16);
  --mem-line-strong: rgba(109, 230, 218, .38);
  --mem-ink: #e7f2f1;
  --mem-muted: #a5b7b9;
  --mem-quiet: #71878b;
  --mem-accent: #55dfcf;
  --mem-accent-ink: #061312;
  --mem-warn: #f0a868;
  --mem-danger: #ff7a70;
  --mem-success: #6ed39b;
  --mem-shadow: rgba(0, 8, 10, .42);
  --mem-font: "Bahnschrift", "Segoe UI Variable", "Segoe UI", sans-serif;
  --mem-mono: "Cascadia Code", Consolas, monospace;
  display: grid;
  gap: 18px;
  min-width: 0;
  padding: 24px;
  color: var(--mem-ink);
  font-family: var(--mem-font);
}
.memory-toolbar, .memory-layout, .memory-create { min-width: 0; }
.memory-toolbar { display: flex; align-items: end; justify-content: space-between; gap: 18px; }
.memory-toolbar h2 { margin: 0; font-size: clamp(24px, 3vw, 40px); letter-spacing: -.035em; overflow-wrap: anywhere; }
.memory-toolbar p, .create-consequence p { margin: 7px 0 0; color: var(--mem-muted); }
.toolbar-actions, .decision-block div, .edit-form div { display: flex; flex-wrap: wrap; gap: 10px; }
button, input, select, textarea { font: inherit; }
button { min-height: 44px; white-space: nowrap; cursor: pointer; }
button:disabled { cursor: not-allowed; opacity: .48; }
.accent-button, .quiet-button, .danger-button { min-height: 44px; padding: 0 15px; border-radius: 8px; font-weight: 700; }
.accent-button { border: 1px solid var(--mem-accent); background: var(--mem-accent); color: var(--mem-accent-ink); }
.quiet-button { border: 1px solid var(--mem-line); background: var(--mem-raised); color: var(--mem-ink); }
.danger-button { border: 1px solid var(--mem-danger); background: transparent; color: var(--mem-danger); }
.memory-create { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, .8fr) minmax(0, .7fr) minmax(0, .8fr); gap: 12px; padding: 18px; border: 1px solid var(--mem-line-strong); border-radius: 12px; background: var(--mem-surface); box-shadow: 0 18px 48px var(--mem-shadow); }
label { display: grid; gap: 7px; min-width: 0; color: var(--mem-muted); font-size: 13px; }
input, select, textarea { width: 100%; min-width: 0; min-height: 44px; padding: 10px 12px; border: 1px solid var(--mem-line); border-radius: 8px; outline: none; background: var(--mem-raised); color: var(--mem-ink); }
textarea { resize: vertical; }
input:focus, select:focus, textarea:focus, button:focus-visible { border-color: var(--mem-accent); box-shadow: 0 0 0 3px var(--mem-selected); outline: none; }
.wide-field { grid-column: 1 / -1; }
.create-consequence { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.memory-message, .memory-error, .memory-state { margin: 0; padding: 12px 15px; border: 1px solid var(--mem-line); border-radius: 8px; background: var(--mem-raised); }
.memory-message { color: var(--mem-success); }
.memory-error { color: var(--mem-danger); }
.memory-state { color: var(--mem-muted); }
.memory-layout { display: grid; grid-template-columns: minmax(290px, .72fr) minmax(0, 1.28fr); min-height: 620px; border: 1px solid var(--mem-line); border-radius: 14px; overflow: clip; background: var(--mem-surface); box-shadow: 0 24px 70px var(--mem-shadow); }
.memory-ledger { min-width: 0; padding: 16px; border-right: 1px solid var(--mem-line); background: var(--mem-raised); }
.memory-search { margin-bottom: 12px; }
.memory-filters { display: flex; gap: 6px; padding-bottom: 14px; overflow-x: auto; }
.memory-filters button { min-height: 36px; padding: 0 11px; border: 1px solid var(--mem-line); border-radius: 999px; background: transparent; color: var(--mem-muted); }
.memory-filters button.active { border-color: var(--mem-line-strong); background: var(--mem-selected); color: var(--mem-accent); }
.ledger-group { display: grid; gap: 7px; margin-top: 12px; }
.ledger-group h3, .usage-ledger h4 { display: flex; justify-content: space-between; margin: 0 0 5px; color: var(--mem-muted); font-size: 12px; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; line-height: 1.04; }
.ledger-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; width: 100%; padding: 10px 11px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--mem-ink); text-align: left; }
.ledger-row:hover, .ledger-row.selected { border-color: var(--mem-line); background: var(--mem-selected); }
.row-main { display: grid; gap: 4px; min-width: 0; }
.row-main strong, .row-main small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-main small, .ledger-empty { color: var(--mem-quiet); }
.status-chip { flex: 0 0 auto; padding: 4px 7px; border: 1px solid var(--mem-line); border-radius: 5px; color: var(--mem-muted); font: 11px/1.2 var(--mem-mono); }
.status-chip[data-status="active"] { color: var(--mem-success); }
.status-chip[data-status="conflict"], .status-chip[data-status="pending_confirmation"] { color: var(--mem-warn); }
.status-chip[data-status="deleted"], .status-chip[data-status="expired"] { color: var(--mem-quiet); }
.memory-detail { min-width: 0; padding: clamp(20px, 4vw, 38px); }
.detail-heading { display: flex; align-items: start; justify-content: space-between; gap: 14px; }
.detail-heading small { color: var(--mem-muted); }
.detail-heading h3 { margin: 5px 0 0; font-size: clamp(24px, 3vw, 38px); letter-spacing: -.03em; overflow-wrap: anywhere; }
.memory-value { margin: 26px 0; padding: 20px 0; border-block: 1px solid var(--mem-line); color: var(--mem-ink); font-size: clamp(17px, 2vw, 22px); line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.evidence-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; margin: 0; background: var(--mem-line); }
.evidence-grid div { min-width: 0; padding: 13px; background: var(--mem-surface); }
.evidence-grid dt { color: var(--mem-quiet); font-size: 12px; }
.evidence-grid dd { margin: 6px 0 0; overflow-wrap: anywhere; }
.digest-line { display: block; margin-top: 14px; color: var(--mem-quiet); font: 11px/1.5 var(--mem-mono); overflow-wrap: anywhere; }
.decision-block, .detail-actions, .conflict-box, .usage-ledger, .edit-form { margin-top: 24px; padding-top: 20px; border-top: 1px solid var(--mem-line); }
.decision-block p, .detail-actions p, .conflict-box p, .usage-ledger p, .edit-form p { color: var(--mem-muted); }
.conflict-box h4, .usage-ledger h4 { margin-top: 0; }
.conflict-box button { width: 100%; margin-top: 8px; padding: 8px 12px; border: 1px solid var(--mem-warn); border-radius: 8px; background: transparent; color: var(--mem-warn); text-align: left; white-space: normal; }
.usage-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 12px; padding: 11px 0; border-bottom: 1px solid var(--mem-line); }
.usage-row span, .usage-row small { color: var(--mem-muted); }
.usage-row small { grid-column: 1 / -1; font-family: var(--mem-mono); overflow-wrap: anywhere; }
.history-warning { display: block; margin-top: 12px; color: var(--mem-warn); }
.detail-actions { display: flex; flex-wrap: wrap; gap: 10px; }
.detail-actions p { flex-basis: 100%; margin: 0; }
.edit-form { display: grid; gap: 12px; }
.detail-empty { display: grid; min-height: 480px; place-content: center; text-align: center; }
.detail-empty h3 { margin: 0; }
.detail-empty p { color: var(--mem-muted); }
@media (max-width: 900px) {
  .memory-layout { grid-template-columns: minmax(0, 1fr); }
  .memory-ledger { border-right: 0; border-bottom: 1px solid var(--mem-line); }
  .memory-create { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 620px) {
  .memory-control { padding: 16px; }
  .memory-toolbar, .create-consequence { align-items: stretch; flex-direction: column; }
  .toolbar-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .memory-create, .evidence-grid { grid-template-columns: minmax(0, 1fr); }
  .wide-field { grid-column: auto; }
  .memory-filters { padding-bottom: 10px; }
  .usage-row { grid-template-columns: minmax(0, 1fr); }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
}
</style>
