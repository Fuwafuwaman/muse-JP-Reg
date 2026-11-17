<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listAccounts, deleteAccount, bulkDeleteAccounts, resetFailed,
  resetAccount, bulkResetAccounts, releaseStale,
} from '@/api/accounts'
import { getMailProviders } from '@/api/settings'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'
import { t } from '@/i18n'

const router = useRouter()
const statsStore = useStatsStore()
const runtime = useRuntimeStore()
const { dataVersion } = storeToRefs(runtime)

const PAGE_SIZE = 20
const rows = ref([])
const total = ref(0)
const page = ref(1)
const statusFilter = ref('')
const kindFilter = ref('')
const bulkStatus = ref('')
const selected = ref([])
const loading = ref(false)
// 号池现在可以混放多种邮箱，这两个用来显示「来源」列和按来源过滤
const providers = ref([])
const byKind = ref({})

const STATUS_TYPE = { available: 'success', in_use: 'warning', done: 'primary', failed: 'danger' }

// 列表里只列池子里真有号的来源，免得下拉框塞一堆空选项
const kindOptions = computed(() =>
  providers.value.filter((p) => p.pooled).map((p) => ({
    kind: p.kind,
    label: p.display_name,
    count: byKind.value[p.kind]?.total || 0,
  })),
)

function kindLabel(k) {
  return providers.value.find((p) => p.kind === k)?.display_name || k || 'outlook'
}

async function loadProviders() {
  try {
    providers.value = (await getMailProviders()).providers || []
  } catch (_) { /* 拿不到就退化成显示原始 kind 字符串 */ }
}

async function load(resetPage) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const { items, total: t, by_kind } = await listAccounts({
      status: statusFilter.value,
      kind: kindFilter.value,
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    rows.value = items
    total.value = t
    byKind.value = by_kind || {}
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function afterMutate() { load(); statsStore.refresh() }

async function confirm(msg, title = t('确认')) {
  try { await ElMessageBox.confirm(msg, title, { type: 'warning', confirmButtonText: t('确定'), cancelButtonText: t('取消') }); return true }
  catch (_) { return false }
}

async function resetFailedAll() {
  if (!(await confirm(t('把所有 failed 号重置为 available？')))) return
  try { const r = await resetFailed(); ElMessage.success(t('重置 {n} 个', { n: r.reset })); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function releaseStaleAll() {
  try { const r = await releaseStale(); ElMessage.success(t('释放 {n} 个卡死号', { n: r.released })); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function resetSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(t('重置选中的 {n} 个号为 available？（已保存凭证不变）', { n: emails.length })))) return
  try { const r = await bulkResetAccounts(emails); ElMessage.success(t('已重置 {n} 个', { n: r.reset })); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteSelected() {
  const emails = selected.value.map((r) => r.email)
  if (!emails.length) return
  if (!(await confirm(t('确定删除选中的 {n} 个号？(不可恢复)', { n: emails.length })))) return
  try { const r = await bulkDeleteAccounts({ emails }); ElMessage.success(t('已删除 {n} 个', { n: r.deleted })); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function bulkDeleteByStatus() {
  if (!bulkStatus.value) { ElMessage.warning(t('请先选择要删除的状态')); return }
  const tip = bulkStatus.value === 'all'
    ? t('这会删除邮箱列表里所有号（含未注册的），确定？')
    : t('确定删除全部 {st} 状态的号？', { st: bulkStatus.value })
  if (!(await confirm(tip))) return
  try {
    const r = await bulkDeleteAccounts({ status: bulkStatus.value })
    ElMessage.success(t('已删除 {n} 个 {st} 号', { n: r.deleted, st: bulkStatus.value }))
    bulkStatus.value = ''
    afterMutate()
  } catch (e) { ElMessage.error(e.message) }
}
function useAccount(email) {
  router.push({ path: '/register', query: { email } })
}
async function resetOne(email) {
  if (!(await confirm(t('重置 {email} 为 available？', { email })))) return
  try { await resetAccount(email); ElMessage.success(t('已重置')); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}
async function deleteOne(email) {
  if (!(await confirm(t('删除 {email}？', { email })))) return
  try { await deleteAccount(email); ElMessage.success(t('已删除')); afterMutate() }
  catch (e) { ElMessage.error(e.message) }
}

watch(page, () => load())
watch(dataVersion, () => load())
onActivated(() => load())
loadProviders()
</script>
<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <span class="section-title" style="margin: 0">{{ t('邮箱列表') }}</span>
      </template>

      <el-space wrap style="margin-bottom: 12px">
        <el-select v-model="statusFilter" :placeholder="t('全部')" style="width: 130px" @change="load(true)">
          <el-option :label="t('全部')" value="" />
          <el-option :label="t('available')" value="available" />
          <el-option :label="t('in_use')" value="in_use" />
          <el-option :label="t('done')" value="done" />
          <el-option :label="t('failed')" value="failed" />
        </el-select>
        <!-- 号池混放多种邮箱时才有意义，只有一种来源就不显示 -->
        <el-select
          v-if="kindOptions.length > 1"
          v-model="kindFilter" :placeholder="t('全部来源')" style="width: 190px" @change="load(true)"
        >
          <el-option :label="t('全部来源')" value="" />
          <el-option
            v-for="o in kindOptions" :key="o.kind"
            :label="`${o.label} (${o.count})`" :value="o.kind"
          />
        </el-select>
        <el-button @click="load(false)"><el-icon><Refresh /></el-icon>{{ t('刷新') }}</el-button>
        <el-button @click="resetFailedAll">{{ t('重试 failed') }}</el-button>
        <el-button @click="releaseStaleAll">{{ t('释放卡死号') }}</el-button>
      </el-space>

      <el-space wrap style="margin-bottom: 12px">
        <el-button type="primary" plain :disabled="!selected.length" @click="resetSelected">
          {{ t('重置选中 ({n})', { n: selected.length }) }}
        </el-button>
        <el-button type="danger" plain :disabled="!selected.length" @click="deleteSelected">
          {{ t('删除选中 ({n})', { n: selected.length }) }}
        </el-button>
        <el-select v-model="bulkStatus" :placeholder="t('— 按状态批量删 —')" style="width: 180px">
          <el-option :label="t('删全部 failed')" value="failed" />
          <el-option :label="t('删全部 done')" value="done" />
          <el-option :label="t('删全部 available')" value="available" />
          <el-option :label="t('删全部 in_use')" value="in_use" />
          <el-option :label="t('删全部（危险）')" value="all" />
        </el-select>
        <el-button @click="bulkDeleteByStatus">{{ t('执行') }}</el-button>
      </el-space>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table
        v-else
        v-loading="loading" :data="rows" size="small" stripe
        @selection-change="(v) => (selected = v)"
      >
        <el-table-column type="selection" width="44" />
        <el-table-column prop="email" :label="t('邮箱')" min-width="220" show-overflow-tooltip />
        <el-table-column v-if="kindOptions.length > 1" :label="t('来源')" width="130">
          <template #default="{ row }">
            <el-tag size="small" type="info">{{ kindLabel(row.kind) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('状态')" width="110">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[row.status] || 'info'" :text="t(row.status)" />
          </template>
        </el-table-column>
        <el-table-column prop="fail_reason" :label="t('失败原因')" min-width="180" show-overflow-tooltip />
        <el-table-column :label="t('操作')" width="220" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text @click="useAccount(row.email)">{{ t('使用') }}</el-button>
            <el-button
              v-if="row.status === 'done' || row.status === 'failed'"
              size="small" text type="primary" @click="resetOne(row.email)"
            >{{ t('重置') }}</el-button>
            <el-button size="small" text type="danger" @click="deleteOne(row.email)">{{ t('删除') }}</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty :description="t('暂无数据，去「导入邮箱」添加接码号')" :image-size="70" />
        </template>
      </el-table>

      <div style="display: flex; justify-content: center; margin-top: 14px">
        <el-pagination
          v-model:current-page="page" :page-size="PAGE_SIZE" :total="total"
          layout="prev, pager, next, total" background
        />
      </div>
    </el-card>
  </div>
</template>
