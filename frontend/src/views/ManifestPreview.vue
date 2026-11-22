<template>
  <section class="manifest-preview" data-testid="manifest-preview">
    <header class="preview-head">
      <div>
        <h3>定时采集预演视图</h3>
        <p class="preview-desc">
          预演列出种子页的实际归属、清单之间的重叠关系，以及下一批还能领取哪些页面。
        </p>
      </div>
      <div class="preview-head-actions">
        <button class="btn" type="button" data-testid="preview-refresh" @click="reload">
          {{ loading ? '刷新中…' : '刷新预演' }}
        </button>
      </div>
    </header>

    <form class="filter-bar" data-testid="from-script-form" @submit.prevent="createFromScript">
      <label class="filter-item">
        <span>关联计划</span>
        <input v-model="scriptInput.plan" data-testid="from-script-plan" placeholder="如：SCRI-0003" required />
      </label>
      <label class="filter-item">
        <span>种子页范围</span>
        <input v-model="scriptInput.pages" data-testid="from-script-pages" placeholder="如：甲,乙" required />
      </label>
      <label class="filter-item">
        <span>清单编号（可留空）</span>
        <input v-model="scriptInput.code" data-testid="from-script-code" placeholder="留空自动编号" />
      </label>
      <button class="btn primary" type="submit" data-testid="from-script-submit" :disabled="creating">
        {{ creating ? '生成中…' : '从脚本输入生成清单' }}
      </button>
    </form>
    <p v-if="notice" class="preview-notice">{{ notice }}</p>

    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">清单数</span>
        <strong class="stat-value">{{ summary.清单数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">预演页数</span>
        <strong class="stat-value">{{ summary.预演页数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">重叠页数</span>
        <strong class="stat-value">{{ summary.重叠页数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已处理页</span>
        <strong class="stat-value">{{ summary.已处理页数 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可领页</span>
        <strong class="stat-value">{{ summary.可领页数 }}</strong>
      </article>
    </div>

    <h4 class="preview-subtitle">清单预演范围</h4>
    <table class="data-table">
      <thead>
        <tr>
          <th>清单编号</th>
          <th>关联计划</th>
          <th>种子页范围</th>
          <th>预演页数</th>
          <th>实际派发页</th>
          <th>待领页</th>
          <th>已处理页</th>
          <th>状态</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in manifests"
          :key="item.id"
          :data-testid="`preview-manifest-${item.清单编号}`"
        >
          <td>{{ item.清单编号 }}</td>
          <td>{{ item.关联计划 }}</td>
          <td>{{ item.种子页范围 }}</td>
          <td>{{ item.预演页数 }}</td>
          <td>{{ item.实际派发页 }}</td>
          <td>{{ item.待领页 }}</td>
          <td>{{ item.settled_pages.length }}</td>
          <td>{{ item.清单状态 }}</td>
        </tr>
        <tr v-if="!manifests.length">
          <td colspan="8" class="empty-state">暂无执行清单，可先登记或从脚本输入生成</td>
        </tr>
      </tbody>
    </table>

    <h4 class="preview-subtitle">清单重叠关系</h4>
    <table class="data-table" data-testid="preview-overlaps">
      <thead>
        <tr>
          <th>清单 A</th>
          <th>清单 B</th>
          <th>重叠页</th>
          <th>重叠数</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in overlaps" :key="`${item.清单A}-${item.清单B}`">
          <td>{{ item.清单A }}</td>
          <td>{{ item.清单B }}</td>
          <td>{{ item.重叠页.join('、') }}</td>
          <td>{{ item.重叠数 }}</td>
        </tr>
        <tr v-if="!overlaps.length">
          <td colspan="4" class="empty-state">当前清单之间没有重叠种子页</td>
        </tr>
      </tbody>
    </table>

    <h4 class="preview-subtitle">页面归属与下一批待领</h4>
    <table class="data-table" data-testid="preview-trace">
      <thead>
        <tr>
          <th>页面</th>
          <th>当前状态</th>
          <th>采到它的批次</th>
          <th>来源清单</th>
          <th>覆盖清单</th>
          <th>下一步</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in pageTrace" :key="item.页面" :data-testid="`preview-page-${item.页面}`">
          <td>{{ item.页面 }}</td>
          <td>{{ item.状态 }}</td>
          <td>{{ item.采集批次 || '—' }}</td>
          <td>{{ item.来源清单 || '—' }}</td>
          <td>{{ item.覆盖清单.join('、') }}</td>
          <td>
            <span v-if="item.状态 === '已处理'">无需再领</span>
            <span v-else-if="item.状态 === '已派发'">{{ item.当前批次 }} 领取中</span>
            <span v-else>下一批可领</span>
          </td>
        </tr>
        <tr v-if="!pageTrace.length">
          <td colspan="6" class="empty-state">暂无种子页可追溯</td>
        </tr>
      </tbody>
    </table>

    <h4 class="preview-subtitle">批次历史（实际派出的范围）</h4>
    <table class="data-table" data-testid="preview-batches">
      <thead>
        <tr>
          <th>批次编号</th>
          <th>来源清单</th>
          <th>预演页数</th>
          <th>实际页数</th>
          <th>实际派出页面</th>
          <th>状态</th>
          <th>派发时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in batches" :key="item.id" :data-testid="`preview-batch-${item.批次编号}`">
          <td>{{ item.批次编号 }}</td>
          <td>{{ item.来源清单 || '—' }}</td>
          <td>{{ item.预演页数 }}</td>
          <td>{{ item.实际页数 }}</td>
          <td>{{ item.页面列表.join('、') || '—' }}</td>
          <td>{{ item.status }}</td>
          <td>{{ item.派发时间 || '—' }}</td>
        </tr>
        <tr v-if="!batches.length">
          <td colspan="7" class="empty-state">暂无由清单派发的批次</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

const emit = defineEmits<{ created: [] }>()

type PreviewPage = {
  页面: string
  状态: string
  采集批次: string
  当前批次: string
  来源清单: string
  覆盖清单: string[]
}

type PreviewManifest = {
  id: number
  清单编号: string
  关联计划: string
  种子页范围: string
  预演页数: number
  实际派发页: number
  待领页: number
  清单状态: string
  settled_pages: string[]
}

type PreviewBatch = {
  id: number
  批次编号: string
  来源清单: string
  预演页数: number
  实际页数: number
  页面列表: string[]
  status: string
  派发时间: string
}

type PreviewPayload = {
  summary: Record<string, number>
  manifests: PreviewManifest[]
  overlaps: { 清单A: string; 清单B: string; 重叠页: string[]; 重叠数: number }[]
  page_trace: PreviewPage[]
  batches: PreviewBatch[]
}

const emptySummary = () => ({
  清单数: 0,
  预演页数: 0,
  重叠页数: 0,
  已处理页数: 0,
  可领页数: 0,
})

const loading = ref(false)
const creating = ref(false)
const notice = ref('')
const errorMessage = ref('')
const scriptInput = ref({ plan: '', pages: '', code: '' })
const summary = ref<Record<string, number>>(emptySummary())
const manifests = ref<PreviewManifest[]>([])
const overlaps = ref<PreviewPayload['overlaps']>([])
const pageTrace = ref<PreviewPage[]>([])
const batches = ref<PreviewBatch[]>([])

async function createFromScript() {
  creating.value = true
  notice.value = ''
  errorMessage.value = ''
  try {
    const response = await request('/api/collection_manifest/from-script', {
      method: 'POST',
      body: JSON.stringify({
        values: {
          清单编号: scriptInput.value.code.trim(),
          计划编号: scriptInput.value.plan.trim(),
          页面: scriptInput.value.pages.trim(),
        },
      }),
    })
    const result = (await response.json()) as { ok: boolean; message: string }
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    scriptInput.value = { plan: '', pages: '', code: '' }
    await reload()
    emit('created')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单生成失败'
  } finally {
    creating.value = false
  }
}

async function reload() {
  loading.value = true
  errorMessage.value = ''
  try {
    const payload = await fetchJson<PreviewPayload>('/api/collection_manifest/preview')
    summary.value = { ...emptySummary(), ...payload.summary }
    manifests.value = payload.manifests
    overlaps.value = payload.overlaps
    pageTrace.value = payload.page_trace
    batches.value = payload.batches
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预演数据读取失败'
  } finally {
    loading.value = false
  }
}

defineExpose({ reload })
onMounted(reload)
</script>
