<script setup lang="ts">
import { onMounted, ref } from 'vue'
import {
  downloadEvaluationReport,
  getEvaluationReport,
  listEvaluationRuns,
  replayEvaluation,
  runGoldenEvaluation,
} from '../api'
import type { EvaluationReport, EvaluationRun } from '../types'

const runs = ref<EvaluationRun[]>([])
const active = ref<EvaluationRun | null>(null)
const report = ref<EvaluationReport | null>(null)
const busy = ref(false)
const message = ref<string | null>(null)

onMounted(() => void refresh())

async function refresh(): Promise<void> {
  try {
    const [page, latestReport] = await Promise.all([listEvaluationRuns(), getEvaluationReport()])
    runs.value = page.runs
    report.value = latestReport
  } catch (error) {
    message.value = error instanceof Error ? error.message : '读取评测历史失败。'
  }
}

async function exportReport(): Promise<void> {
  if (busy.value) return
  busy.value = true; message.value = null
  try { await downloadEvaluationReport() } catch (error) {
    message.value = error instanceof Error ? error.message : '评测报告导出失败。'
  } finally { busy.value = false }
}

async function run(): Promise<void> {
  if (busy.value) return
  busy.value = true; message.value = null
  try { active.value = await runGoldenEvaluation(); await refresh() } catch (error) {
    message.value = error instanceof Error ? error.message : '黄金任务运行失败。'
  } finally { busy.value = false }
}

async function replay(runId: string): Promise<void> {
  if (busy.value) return
  busy.value = true; message.value = null
  try { active.value = await replayEvaluation(runId); await refresh() } catch (error) {
    message.value = error instanceof Error ? error.message : 'Trace replay 失败。'
  } finally { busy.value = false }
}
</script>

<template>
  <section class="lab" aria-labelledby="evaluation-heading">
    <article class="panel hero">
      <span class="eyebrow">VERSIONED GOLDEN TASKS</span>
      <h2 id="evaluation-heading">评测与 Trace Replay</h2>
      <p>运行仓库内固定 20 项安全与恢复套件；不会读取生产知识源，也不接受上传脚本或命令。</p>
      <div class="actions">
        <button type="button" :disabled="busy" @click="run">运行黄金套件</button>
        <button class="secondary" type="button" :disabled="busy" @click="exportReport">导出 v1 报告</button>
      </div>
      <p v-if="message" class="message" role="status">{{ message }}</p>
    </article>
    <article v-if="report" class="panel report" aria-labelledby="report-heading">
      <div class="report-title">
        <div><span class="eyebrow">EVALUATION REPORT V1</span><h3 id="report-heading">跨运行趋势</h3></div>
        <small>{{ report.report_digest.slice(0, 16) }}</small>
      </div>
      <div class="metrics report-metrics">
        <div><small>运行通过率</small><strong>{{ Math.round(report.run_success_rate * 100) }}%</strong></div>
        <div><small>运行 p50 / p95</small><strong>{{ report.run_duration_p50_ms ?? '—' }} / {{ report.run_duration_p95_ms ?? '—' }}</strong></div>
        <div><small>Case p50 / p95</small><strong>{{ report.case_duration_p50_ms ?? '—' }} / {{ report.case_duration_p95_ms ?? '—' }}</strong></div>
        <div><small>失败运行</small><strong>{{ report.failed_run_count }}</strong></div>
      </div>
      <div v-if="Object.keys(report.failure_counts).length" class="failures">
        <span v-for="(count, code) in report.failure_counts" :key="code">{{ code }} × {{ count }}</span>
      </div>
      <p v-else>当前窗口没有失败分类。</p>
      <ol v-if="report.trend.length" class="trend" aria-label="运行趋势">
        <li v-for="point in report.trend" :key="point.run_id">
          <span>{{ point.run_id.slice(0, 12) }}</span>
          <meter min="0" max="1" :value="point.success_rate">{{ point.success_rate }}</meter>
          <small>{{ Math.round(point.success_rate * 100) }}% · {{ point.duration_ms }} ms</small>
        </li>
      </ol>
    </article>
    <article v-if="active" class="panel metrics">
      <div><small>成功率</small><strong>{{ Math.round(active.success_rate * 100) }}%</strong></div>
      <div><small>安全通过率</small><strong>{{ Math.round(active.safety_rate * 100) }}%</strong></div>
      <div><small>总耗时</small><strong>{{ active.duration_ms }} ms</strong></div>
      <div><small>Replay</small><strong>{{ active.replay_match === null ? '首次记录' : active.replay_match ? '一致' : '漂移' }}</strong></div>
    </article>
    <article v-if="active" class="panel">
      <h3>Trace chain</h3>
      <ol class="traces">
        <li v-for="trace in active.traces" :key="trace.sequence">
          <span>#{{ trace.sequence }} {{ trace.case_id }}</span>
          <strong :class="trace.status">{{ trace.status }}</strong>
          <small>{{ trace.duration_ms }} ms · {{ trace.output_digest.slice(0, 12) }}</small>
        </li>
      </ol>
    </article>
    <article class="panel">
      <h3>持久化运行</h3>
      <ul v-if="runs.length" class="runs">
        <li v-for="item in runs" :key="item.run_id">
          <button class="run-row" type="button" @click="active = item">
            <span>{{ item.run_id.slice(0, 16) }}</span><strong>{{ item.status }}</strong>
          </button>
          <button type="button" :disabled="busy" @click="replay(item.run_id)">Replay</button>
        </li>
      </ul>
      <p v-else>尚无评测运行。</p>
    </article>
  </section>
</template>

<style scoped>
.lab { display: grid; gap: 18px; padding: 26px; }.panel { padding: 22px; border: 1px solid rgba(151,166,199,.16); border-radius: 18px; background: rgba(13,19,35,.78); }.hero h2,.report h3 { margin: 6px 0; }.hero p,.panel p,small { color: #8b96ad; }.actions,.report-title { display: flex; align-items: center; justify-content: space-between; gap: 10px; }.actions { justify-content: flex-start; }button { padding: 9px 14px; border: 0; border-radius: 9px; background: #527cff; color: #fff; font-weight: 700; }.secondary { background: #283653; }button:disabled { opacity: .45; }.metrics { display: grid; grid-template-columns: repeat(4,1fr); gap: 12px; }.metrics div { display: grid; gap: 5px; }.metrics strong { font-size: 24px; }.report-metrics strong { font-size: 18px; }.traces,.runs,.trend { display: grid; gap: 8px; padding: 0; list-style: none; }.traces li { display: grid; grid-template-columns: 1fr auto; gap: 5px; padding: 12px; border-bottom: 1px solid rgba(151,166,199,.12); }.traces small { grid-column: 1/-1; }.trend li { display: grid; grid-template-columns: 130px 1fr auto; gap: 12px; align-items: center; }.trend meter { width: 100%; }.failures { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0; }.failures span { padding: 6px 9px; border-radius: 8px; background: rgba(255,143,143,.12); color: #ffaaaa; }.passed { color: #58e0b8; }.failed,.message { color: #ff8f8f; }.runs li { display: flex; gap: 8px; }.run-row { display: flex; flex: 1; justify-content: space-between; background: #18223c; }@media(max-width:760px){.metrics{grid-template-columns:repeat(2,1fr)}.trend li{grid-template-columns:1fr}.actions{align-items:stretch;flex-direction:column}}
</style>
