<script setup lang="ts">
import { computed, ref } from 'vue'
import {
  ApiProblemError,
  cancelWorkbenchExecution,
  commitArtifactExport,
  createResearchWorkbenchTask,
  getTaskWorkbench,
  prepareArtifactExport,
  runWorkbenchStep,
} from '../api'
import type {
  ArtifactExport,
  TaskWorkbench,
  WorkbenchAction,
  WorkbenchNode,
  WorkbenchStepCommand,
} from '../types'

const goal = ref('研究一个主题，并生成带有可核验引用的独立 HTML 页面')
const privacyMode = ref<'local_preferred' | 'balanced'>('balanced')
const workbench = ref<TaskWorkbench | null>(null)
const busyAction = ref<WorkbenchAction | 'create' | 'commit_export' | null>(null)
const error = ref<string | null>(null)
const targetPath = ref('')
const exportPreview = ref<ArtifactExport | null>(null)

const run = computed(() => workbench.value?.executions.runs.at(-1) ?? null)
const actionMap = computed(() => new Map(
  (workbench.value?.actions ?? []).map((item) => [item.action, item]),
))
const nodeMap = computed(() => new Map(
  (run.value?.nodes ?? []).map((item) => [item.local_key, item]),
))
const verdictMap = computed(() => new Map(
  (workbench.value?.verification?.verdicts ?? []).map((item) => [item.claim_id, item]),
))

const stages: Array<{
  key: string
  label: string
  action: WorkbenchAction
  command: WorkbenchStepCommand
}> = [
  { key: 'research', label: '研究取证', action: 'run_research', command: 'research:run' },
  { key: 'research', label: '独立核验', action: 'verify_claims', command: 'claims:verify' },
  { key: 'build_html', label: '构建 Artifact', action: 'build_artifact', command: 'artifacts:build' },
  { key: 'browser_verify', label: '隔离浏览器验收', action: 'verify_browser', command: 'browser:verify' },
  { key: 'final_acceptance', label: '形成交付清单', action: 'finalize_delivery', command: 'final-acceptance:run' },
]

const stageLabel: Record<string, string> = {
  idle: '尚未创建',
  planned: '计划已绑定',
  researching: '研究执行中',
  awaiting_verification: '等待独立核验',
  building_artifact: 'Artifact 构建中',
  verifying_browser: '浏览器验收中',
  ready_to_deliver: '可形成交付',
  delivered: '已交付，可导出',
  exported: '已精确导出',
  blocked: '已阻断',
}

function shortDigest(value: string | null | undefined): string {
  return value ? `${value.slice(0, 10)}…${value.slice(-6)}` : '—'
}

function nodeState(node: WorkbenchNode | undefined): string {
  if (!node) return 'locked'
  return node.status
}

function problemMessage(caught: unknown): string {
  if (caught instanceof ApiProblemError) return `${caught.message}（${caught.code}）`
  return caught instanceof Error ? caught.message : '操作失败，请重试。'
}

async function refresh(): Promise<void> {
  if (!workbench.value) return
  workbench.value = await getTaskWorkbench(workbench.value.task.task_id)
}

async function createWorkbench(): Promise<void> {
  if (!goal.value.trim() || busyAction.value) return
  busyAction.value = 'create'
  error.value = null
  exportPreview.value = null
  try {
    workbench.value = await createResearchWorkbenchTask({
      goal: goal.value.trim(),
      privacy_mode: privacyMode.value,
      constraints: [],
    })
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

async function runStep(action: WorkbenchAction, command: WorkbenchStepCommand): Promise<void> {
  if (!run.value || busyAction.value || !actionMap.value.get(action)?.enabled) return
  busyAction.value = action
  error.value = null
  try {
    await runWorkbenchStep(run.value.run_id, command)
    await refresh()
  } catch (caught) {
    error.value = problemMessage(caught)
    await refresh().catch(() => undefined)
  } finally {
    busyAction.value = null
  }
}

async function stopExecution(): Promise<void> {
  if (!run.value || busyAction.value || !actionMap.value.get('stop_execution')?.enabled) return
  busyAction.value = 'stop_execution'
  error.value = null
  try {
    await cancelWorkbenchExecution(run.value.run_id)
    await refresh()
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

function freshKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`
  return `${prefix}-${suffix}`
}

async function previewExport(): Promise<void> {
  const delivery = workbench.value?.delivery
  if (!delivery || !targetPath.value.trim() || busyAction.value) return
  busyAction.value = 'prepare_export'
  error.value = null
  try {
    exportPreview.value = await prepareArtifactExport(
      delivery.delivery_id,
      targetPath.value.trim(),
      freshKey('prepare'),
    )
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}

async function commitExport(): Promise<void> {
  if (!exportPreview.value || busyAction.value) return
  busyAction.value = 'commit_export'
  error.value = null
  try {
    exportPreview.value = await commitArtifactExport(
      exportPreview.value.export_id,
      exportPreview.value.confirmation_digest,
      freshKey('commit'),
    )
    await refresh()
  } catch (caught) {
    error.value = problemMessage(caught)
  } finally {
    busyAction.value = null
  }
}
</script>

<template>
  <section class="research-workbench" aria-labelledby="research-workbench-title">
    <aside class="task-rail">
      <header>
        <span class="rail-kicker">PHASE 76</span>
        <h2 id="research-workbench-title">研究交付台</h2>
        <p>一个任务，一条可证明的交付链。</p>
      </header>

      <form class="goal-form" @submit.prevent="createWorkbench">
        <label for="research-goal">任务目标</label>
        <textarea id="research-goal" v-model="goal" rows="5" maxlength="4000" />
        <label for="research-privacy">联网策略</label>
        <select id="research-privacy" v-model="privacyMode">
          <option value="balanced">平衡模式</option>
          <option value="local_preferred">本地优先</option>
        </select>
        <button class="rail-primary" type="submit" :disabled="Boolean(busyAction) || !goal.trim()">
          {{ busyAction === 'create' ? '建立任务中…' : '建立研究任务' }}
        </button>
      </form>

      <div class="stage-summary" aria-live="polite">
        <span>当前边界</span>
        <strong>{{ stageLabel[workbench?.stage ?? 'idle'] }}</strong>
        <small v-if="workbench">{{ workbench.task.task_id }}</small>
      </div>

      <button
        class="stop-control"
        type="button"
        :disabled="Boolean(busyAction) || !actionMap.get('stop_execution')?.enabled"
        @click="stopExecution"
      >
        <span aria-hidden="true" />
        {{ busyAction === 'stop_execution' ? '正在停止…' : '停止运行' }}
      </button>
    </aside>

    <main class="task-dossier">
      <header class="dossier-header">
        <div>
          <span class="section-index">01 / EXECUTION</span>
          <h2>{{ workbench?.task.goal ?? '建立任务后开始执行' }}</h2>
        </div>
        <span class="proof-state" :data-stage="workbench?.stage ?? 'idle'">
          {{ stageLabel[workbench?.stage ?? 'idle'] }}
        </span>
      </header>

      <p v-if="error" class="workbench-error" role="alert">{{ error }}</p>

      <section class="conversation" aria-labelledby="conversation-heading">
        <h3 id="conversation-heading">任务上下文</h3>
        <div v-if="workbench?.conversation.length" class="message-list">
          <article v-for="message in workbench.conversation" :key="message.message_id">
            <span>{{ message.role === 'user' ? '你' : 'DeskPilot' }}</span>
            <p>{{ message.content ?? '内容已转为本地引用。' }}</p>
          </article>
        </div>
        <p v-else class="empty-copy">目标只绑定到当前 task，不读取其他会话内容。</p>
      </section>

      <section class="execution-flow" aria-labelledby="execution-heading">
        <div class="section-title">
          <div>
            <span class="section-index">02 / VERIFIED EDGES</span>
            <h3 id="execution-heading">执行与解锁</h3>
          </div>
          <small>后继节点只接受 verified edge</small>
        </div>
        <ol>
          <li v-for="(item, index) in stages" :key="`${item.action}-${index}`" :data-state="nodeState(nodeMap.get(item.key))">
            <div class="step-number">{{ String(index + 1).padStart(2, '0') }}</div>
            <div class="step-copy">
              <strong>{{ item.label }}</strong>
              <small>{{ actionMap.get(item.action)?.explanation ?? '等待服务器投影' }}</small>
            </div>
            <span class="edge-state">{{ nodeState(nodeMap.get(item.key)) }}</span>
            <button
              type="button"
              :disabled="Boolean(busyAction) || !actionMap.get(item.action)?.enabled"
              @click="runStep(item.action, item.command)"
            >
              {{ busyAction === item.action ? '执行中…' : '执行' }}
            </button>
          </li>
        </ol>
      </section>

      <section v-if="workbench?.delivery" class="export-section" aria-labelledby="export-heading">
        <div class="section-title">
          <div>
            <span class="section-index">03 / EXACT EXPORT</span>
            <h3 id="export-heading">精确导出 HTML</h3>
          </div>
          <small>目标存在时拒绝写入</small>
        </div>
        <div class="export-form">
          <label for="export-target">绝对目标路径</label>
          <div>
            <input id="export-target" v-model="targetPath" placeholder="D:\Reports\research.html" />
            <button type="button" :disabled="Boolean(busyAction) || !targetPath.trim()" @click="previewExport">
              {{ busyAction === 'prepare_export' ? '校验中…' : '预览写入' }}
            </button>
          </div>
        </div>
        <article v-if="exportPreview" class="export-receipt" :data-status="exportPreview.status">
          <div>
            <span>{{ exportPreview.status === 'committed' ? '不可变导出回执' : '待确认写入' }}</span>
            <strong>{{ exportPreview.target_path }}</strong>
          </div>
          <dl>
            <div><dt>字节</dt><dd>{{ exportPreview.byte_count }}</dd></div>
            <div><dt>源摘要</dt><dd>{{ shortDigest(exportPreview.source_digest) }}</dd></div>
            <div><dt>确认摘要</dt><dd>{{ shortDigest(exportPreview.confirmation_digest) }}</dd></div>
            <div><dt>回执摘要</dt><dd>{{ shortDigest(exportPreview.receipt_digest) }}</dd></div>
          </dl>
          <button
            v-if="exportPreview.status === 'prepared'"
            class="confirm-write"
            type="button"
            :disabled="Boolean(busyAction)"
            @click="commitExport"
          >
            {{ busyAction === 'commit_export' ? '写入中…' : '确认写入此路径' }}
          </button>
        </article>
      </section>
    </main>

    <aside class="evidence-drawer">
      <header>
        <span class="section-index">EVIDENCE DOSSIER</span>
        <h2>证据卷宗</h2>
        <p>这里只陈列持久化证据，不根据界面事件推测结果。</p>
      </header>

      <section>
        <h3>Claim / Citation</h3>
        <article v-for="claim in workbench?.research?.claims ?? []" :key="claim.claim_id" class="claim-card">
          <div>
            <span class="verdict-mark" :data-verdict="verdictMap.get(claim.claim_id)?.outcome ?? 'pending'" />
            <strong>{{ verdictMap.get(claim.claim_id)?.outcome ?? 'pending' }}</strong>
          </div>
          <p>{{ claim.statement }}</p>
          <small>{{ claim.citation_ids.length }} 条引用 · {{ shortDigest(claim.claim_id.replace('clm_', '')) }}</small>
        </article>
        <p v-if="!workbench?.research?.claims.length" class="empty-copy">研究完成后显示待核验 Claim。</p>
      </section>

      <section>
        <h3>Artifact Workspace</h3>
        <article v-for="artifact in workbench?.workspace?.artifacts ?? []" :key="artifact.artifact_id" class="fact-row">
          <strong>{{ artifact.relative_path }}</strong>
          <span>{{ artifact.active_revision.byte_count }} B</span>
          <small>PatchReceipt {{ shortDigest(artifact.active_revision.patch_receipt_id.replace('ptr_', '')) }}</small>
          <small>Revision {{ shortDigest(artifact.active_revision.revision_id.replace('rev_', '')) }}</small>
        </article>
        <p v-if="!workbench?.workspace" class="empty-copy">verified Claim 解锁后才会建立工作区。</p>
      </section>

      <section>
        <h3>隔离 Browser Verifier</h3>
        <div v-if="workbench?.browser" class="browser-proof" :data-status="workbench.browser.status">
          <strong>{{ workbench.browser.status === 'passed' ? '验收通过' : '验收失败' }}</strong>
          <dl>
            <div><dt>外部请求</dt><dd>{{ workbench.browser.external_request_count }}</dd></div>
            <div><dt>控制台错误</dt><dd>{{ workbench.browser.console_error_count }}</dd></div>
            <div><dt>页面错误</dt><dd>{{ workbench.browser.page_error_count }}</dd></div>
            <div><dt>视口</dt><dd>{{ workbench.browser.viewport_width }}×{{ workbench.browser.viewport_height }}</dd></div>
          </dl>
        </div>
        <p v-else class="empty-copy">Artifact 完成前不会启动浏览器验收。</p>
      </section>

      <footer>
        <span>Projection</span>
        <code>{{ shortDigest(workbench?.projection_digest) }}</code>
      </footer>
    </aside>
  </section>
</template>

<style scoped>
/* finesse · register=product-workbench · A=deep-ocean+oxidized-rust · B=humanist-sans+mono-proof · C=three-column-task-dossier · D=state-feedback-only · E=matte-control-surface · SOUL=7 SPECTACLE=2 DENSITY=8 */
:global(:root) {
  --rw-ocean: #0b2731;
  --rw-ocean-input: #102f38;
  --rw-ocean-text: #edf3ee;
  --rw-ocean-field-text: #f6f4ed;
  --rw-ocean-border: #294b52;
  --rw-ocean-border-strong: #3c5960;
  --rw-ocean-divider: #31505a;
  --rw-ocean-muted: #9aafb1;
  --rw-ocean-faint: #789095;
  --rw-surface: #e9eeeb;
  --rw-surface-raised: #dce3df;
  --rw-surface-input: #f3f6f3;
  --rw-surface-active: #e1e6dd;
  --rw-ink: #183037;
  --rw-muted: #66767a;
  --rw-line: #bac5c0;
  --rw-rust: #a84d32;
  --rw-rust-light: #d78a68;
  --rw-rust-border: #a95a45;
  --rw-rust-text: #ffd8ca;
  --rw-error-surface: #f0ddd7;
  --rw-error-text: #743721;
  --rw-verified: #2b7661;
  --rw-verified-surface: #dce8e2;
  --rw-shadow: rgba(0, 18, 24, 0.24);
  --rw-soft-line: rgba(121, 111, 94, 0.25);
  --rw-shell-line: rgba(169, 196, 199, 0.24);
  --rw-font-ui: "Segoe UI", "Microsoft YaHei UI", sans-serif;
  --rw-font-proof: ui-monospace, "Cascadia Mono", monospace;
}
.research-workbench {
  min-height: calc(100dvh - 108px);
  display: grid;
  grid-template-columns: minmax(180px, 0.68fr) minmax(0, 1.8fr) minmax(240px, 0.9fr);
  overflow: hidden;
  background: var(--rw-surface);
  color: var(--rw-ink);
  border: 1px solid var(--rw-shell-line);
  box-shadow: 0 26px 70px var(--rw-shadow);
  font-family: var(--rw-font-ui);
}

.task-rail {
  display: flex;
  flex-direction: column;
  padding: 28px 22px 22px;
  background: var(--rw-ocean);
  color: var(--rw-ocean-text);
  border-right: 1px solid var(--rw-ocean-border);
}

.task-rail h2,
.evidence-drawer h2,
.dossier-header h2,
.section-title h3,
.conversation h3 { margin: 0; }
.task-rail header p, .evidence-drawer header p { margin: 8px 0 0; font-size: 13px; line-height: 1.55; opacity: 0.68; }
.rail-kicker, .section-index { color: var(--rw-rust); font: 700 10px/1.2 var(--rw-font-proof); letter-spacing: 0.13em; }
.task-rail .rail-kicker { color: var(--rw-rust-light); }
.task-rail h2 { margin-top: 8px; font-size: 25px; letter-spacing: -0.04em; }

.goal-form { margin-top: 30px; display: grid; gap: 9px; }
.goal-form label, .export-form label { font-size: 11px; font-weight: 700; letter-spacing: 0.08em; }
.goal-form textarea, .goal-form select {
  width: 100%; border: 1px solid var(--rw-ocean-border-strong); border-radius: 3px; background: var(--rw-ocean-input); color: var(--rw-ocean-field-text);
  padding: 10px; font: inherit; resize: vertical;
}
.goal-form textarea:focus, .goal-form select:focus { outline: 2px solid var(--rw-rust-light); outline-offset: 1px; }
.rail-primary { margin-top: 5px; min-height: 44px; border: 0; border-radius: 3px; background: var(--rw-surface-raised); color: var(--rw-ink); font-weight: 800; cursor: pointer; white-space: nowrap; }
.rail-primary:disabled { opacity: 0.45; cursor: not-allowed; }

.stage-summary { margin-top: 28px; padding-top: 18px; border-top: 1px solid var(--rw-ocean-divider); display: grid; gap: 5px; }
.stage-summary span { color: var(--rw-ocean-muted); font-size: 11px; }
.stage-summary strong { font-size: 14px; }
.stage-summary small { color: var(--rw-ocean-faint); font: 10px/1.4 var(--rw-font-proof); overflow-wrap: anywhere; }
.stop-control { margin-top: auto; display: flex; align-items: center; justify-content: center; gap: 9px; min-height: 44px; border: 1px solid var(--rw-rust-border); border-radius: 3px; color: var(--rw-rust-text); background: transparent; font-weight: 750; cursor: pointer; white-space: nowrap; }
.stop-control span { width: 9px; height: 9px; background: currentColor; }
.stop-control:disabled { opacity: 0.28; cursor: not-allowed; }

.task-dossier { padding: 30px 32px 48px; min-width: 0; overflow-y: auto; }
.dossier-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; padding-bottom: 24px; border-bottom: 2px solid var(--rw-ink); }
.dossier-header h2 { margin-top: 8px; max-width: 720px; min-width: 0; overflow-wrap: anywhere; font-size: clamp(22px, 2vw, 34px); font-weight: 730; line-height: 1.06; letter-spacing: -0.035em; }
.proof-state { flex: none; padding: 7px 9px; border: 1px solid var(--rw-line); font-size: 11px; font-weight: 800; }
.proof-state[data-stage='exported'], .proof-state[data-stage='delivered'] { border-color: var(--rw-verified); color: var(--rw-verified); }
.workbench-error { padding: 12px 14px; border: 1px solid var(--rw-rust); background: var(--rw-error-surface); color: var(--rw-error-text); font-size: 13px; }

.conversation, .execution-flow, .export-section { padding: 26px 0; border-bottom: 1px solid var(--rw-line); }
.conversation h3, .section-title h3 { margin-top: 5px; font-size: 19px; font-weight: 750; line-height: 1.25; }
.message-list { margin-top: 14px; display: grid; gap: 10px; }
.message-list article { display: grid; grid-template-columns: 64px 1fr; gap: 14px; }
.message-list span { color: var(--rw-rust); font-size: 11px; font-weight: 800; }
.message-list p { margin: 0; font-size: 14px; line-height: 1.6; }
.empty-copy { margin: 12px 0 0; color: var(--rw-muted); font-size: 12px; line-height: 1.55; }
.section-title { display: flex; align-items: end; justify-content: space-between; gap: 20px; }
.section-title > small { color: var(--rw-muted); font-size: 11px; }
.execution-flow ol { list-style: none; margin: 18px 0 0; padding: 0; border-top: 1px solid var(--rw-line); }
.execution-flow li { display: grid; grid-template-columns: 42px minmax(0, 1fr) 132px 72px; align-items: center; gap: 12px; min-height: 72px; border-bottom: 1px solid var(--rw-line); }
.step-number { font: 700 12px/1 var(--rw-font-proof); color: var(--rw-rust); }
.step-copy { display: grid; gap: 4px; }
.step-copy strong { font-size: 14px; }
.step-copy small { color: var(--rw-muted); font-size: 11px; }
.edge-state { color: var(--rw-muted); font: 700 10px/1 var(--rw-font-proof); text-transform: uppercase; }
.execution-flow li[data-state='ready'] .edge-state { color: var(--rw-rust); }
.execution-flow li[data-state='verified'] .edge-state { color: var(--rw-verified); }
.execution-flow li[data-state='ready'] { background: var(--rw-surface-active); }
.execution-flow li[data-state='ready'] .step-number { animation: ready-pulse 1.4s ease-in-out infinite; }
.execution-flow button, .export-form button, .confirm-write { min-height: 44px; border: 1px solid var(--rw-ink); border-radius: 2px; background: transparent; color: var(--rw-ink); font-weight: 750; cursor: pointer; white-space: nowrap; }
.execution-flow button:disabled, .export-form button:disabled { opacity: 0.27; cursor: not-allowed; }

.export-form { margin-top: 18px; display: grid; gap: 7px; }
.export-form > div { display: grid; grid-template-columns: 1fr 108px; }
.export-form input { min-width: 0; border: 1px solid var(--rw-ink); border-right: 0; border-radius: 2px 0 0 2px; background: var(--rw-surface-input); padding: 10px 12px; color: var(--rw-ink); font: 12px/1.4 var(--rw-font-proof); }
.export-receipt { margin-top: 16px; padding: 15px; border: 1px solid var(--rw-rust); background: var(--rw-surface-active); }
.export-receipt > div { display: grid; gap: 5px; }
.export-receipt > div span { color: var(--rw-rust); font-size: 10px; font-weight: 800; text-transform: uppercase; }
.export-receipt > div strong { overflow-wrap: anywhere; font: 12px/1.45 var(--rw-font-proof); }
.export-receipt dl, .browser-proof dl { display: grid; grid-template-columns: 1fr 1fr; margin: 14px 0; gap: 9px 18px; }
.export-receipt dl div, .browser-proof dl div { min-width: 0; }
.export-receipt dt, .browser-proof dt { color: var(--rw-muted); font-size: 10px; }
.export-receipt dd, .browser-proof dd { margin: 3px 0 0; font: 700 11px/1.3 var(--rw-font-proof); overflow-wrap: anywhere; }
.confirm-write { width: 100%; background: var(--rw-rust); border-color: var(--rw-rust); color: var(--rw-ocean-text); }
.export-receipt[data-status='committed'] { border-color: var(--rw-verified); background: var(--rw-verified-surface); }

.evidence-drawer { padding: 30px 22px 20px; min-width: 0; overflow-y: auto; background: var(--rw-surface-raised); border-left: 1px solid var(--rw-line); }
.evidence-drawer h2 { margin-top: 7px; font-size: 24px; font-weight: 760; line-height: 1.1; }
.evidence-drawer section { padding: 21px 0; border-bottom: 1px solid var(--rw-line); }
.evidence-drawer section h3 { margin: 0 0 12px; font-size: 12px; letter-spacing: 0.06em; }
.claim-card { padding: 12px 0; border-top: 1px solid var(--rw-soft-line); }
.claim-card > div { display: flex; align-items: center; gap: 7px; }
.claim-card > div strong { font: 750 10px/1 var(--rw-font-proof); text-transform: uppercase; }
.verdict-mark { width: 8px; height: 8px; border: 1px solid var(--rw-muted); }
.verdict-mark[data-verdict='verified'] { border-color: var(--rw-verified); background: var(--rw-verified); }
.verdict-mark[data-verdict='unsupported'], .verdict-mark[data-verdict='contradicted'] { border-color: var(--rw-rust); background: var(--rw-rust); }
.claim-card p { margin: 8px 0; font-size: 12px; line-height: 1.55; }
.claim-card small, .fact-row small { color: var(--rw-muted); font: 10px/1.45 var(--rw-font-proof); }
.fact-row { display: grid; grid-template-columns: 1fr auto; gap: 5px 10px; }
.fact-row strong { overflow-wrap: anywhere; font-size: 12px; }
.fact-row span { font-size: 10px; }
.fact-row small { grid-column: 1 / -1; overflow-wrap: anywhere; }
.browser-proof { padding: 12px; border: 1px solid var(--rw-rust); background: var(--rw-error-surface); }
.browser-proof[data-status='passed'] { border-color: var(--rw-verified); background: var(--rw-verified-surface); }
.browser-proof > strong { font-size: 13px; }
.browser-proof dl { margin-bottom: 0; }
.evidence-drawer footer { padding-top: 20px; display: flex; justify-content: space-between; color: var(--rw-muted); font-size: 10px; }
.evidence-drawer footer code { color: var(--rw-ink); }

@keyframes ready-pulse { 50% { color: var(--ink); } }
@media (prefers-reduced-motion: reduce) { .execution-flow li[data-state='ready'] .step-number { animation: none; } }
@media (max-width: 1120px) {
  .research-workbench { grid-template-columns: 220px minmax(460px, 1fr); }
  .evidence-drawer { grid-column: 1 / -1; border-left: 0; border-top: 1px solid var(--rw-line); display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; }
  .evidence-drawer header, .evidence-drawer footer { grid-column: 1 / -1; }
}
@media (max-width: 900px) {
  .research-workbench { display: block; }
  .task-rail { min-height: 520px; }
  .task-dossier { padding: 24px 18px 38px; }
  .evidence-drawer { display: block; }
  .execution-flow li { grid-template-columns: 34px 1fr 64px; padding: 10px 0; }
  .edge-state { grid-column: 2; }
  .execution-flow button { grid-row: 1 / span 2; grid-column: 3; }
  .dossier-header { display: grid; }
}
</style>
