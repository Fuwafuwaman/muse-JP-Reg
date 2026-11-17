<script setup>
import { computed, onActivated, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listInvites, importInvites, deleteInvites, resetInvites } from '@/api/invites'
import { copyText, fmtTime } from '@/api/request'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'
import { t } from '@/i18n'

const statsStore = useStatsStore()
const runtime = useRuntimeStore()
const { dataVersion } = storeToRefs(runtime)

const draft = ref('')
const maxUses = ref(30)
const importing = ref(false)

const rows = ref([])
const total = ref(0)
const inviteStats = ref({ available: 0, exhausted: 0, invalid: 0, total: 0, remaining_uses: 0 })
const statusFilter = ref('')
const page = ref(1)
const PAGE_SIZE = 30
const selected = ref([])
const loading = ref(false)

const STATUS_MAP = {
  available: { type: 'success', text: '可用' },
  exhausted: { type: 'info', text: '已用满' },
  invalid: { type: 'danger', text: '无效' },
}

const statusLabel = (st) => t((STATUS_MAP[st] || {}).text || st)
const statusType = (st) => (STATUS_MAP[st] || {}).type || 'info'

const availableCodesText = computed(() =>
  rows.value.filter((r) => r.status === 'available').map((r) => r.code).join('\n'),
)

async function load(resetPage = false) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const r = await listInvites({
      status: statusFilter.value,
      limit: PAGE_SIZE,
      offset: (page.value - 1) * PAGE_SIZE,
    })
    rows.value = r.items || []
    total.value = r.total || 0
    if (r.stats) inviteStats.value = r.stats
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function doImport() {
  if (!draft.value.trim()) {
    ElMessage.warning(t('请先粘贴邀请码'))
    return
  }
  importing.value = true
  try {
    const r = await importInvites(draft.value, maxUses.value || 30)
    ElMessage.success(t('新增 {n} 个邀请码', { n: r.inserted }) + (r.skipped ? t('（跳过已存在 {n} 个）', { n: r.skipped }) : ''))
    draft.value = ''
    statsStore.refresh()
    await load(true)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    importing.value = false
  }
}

async function confirm(msg) {
  try {
    await ElMessageBox.confirm(msg, t('确认'), { type: 'warning', confirmButtonText: t('确定'), cancelButtonText: t('取消') })
    return true
  } catch (_) { return false }
}

async function deleteSelected() {
  const codes = selected.value.map((r) => r.code)
  if (!codes.length) return
  if (!(await confirm(t('确定删除选中的 {n} 个邀请码？', { n: codes.length })))) return
  try {
    const r = await deleteInvites({ codes })
    ElMessage.success(t('已删除 {n} 个', { n: r.deleted }))
    statsStore.refresh()
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function deleteByStatus(st) {
  const label = st === 'all' ? t('全部邀请码') : t('全部 {st} 状态的邀请码', { st })
  if (!(await confirm(t('确定删除 {label}？', { label })))) return
  try {
    const r = await deleteInvites({ status: st })
    ElMessage.success(t('已删除 {n} 个', { n: r.deleted }))
    statsStore.refresh()
    load(true)
  } catch (e) { ElMessage.error(e.message) }
}

async function resetOne(code) {
  try {
    await resetInvites([code])
    ElMessage.success(t('已重置为可用 (0/{n})', { n: maxUses.value }))
    statsStore.refresh()
    load()
  } catch (e) { ElMessage.error(e.message) }
}

async function deleteOne(code) {
  if (!(await confirm(t('删除邀请码 {code}？', { code })))) return
  try {
    await deleteInvites({ codes: [code] })
    ElMessage.success(t('已删除'))
    statsStore.refresh()
    load()
  } catch (e) { ElMessage.error(e.message) }
}

watch(page, () => load())
watch(dataVersion, () => load())
onActivated(() => load())
load()
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="9" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">{{ t('添加种子邀请码') }}</span></template>
          <p class="hint" style="line-height: 1.65" v-html="t('HTML:邀请码种子说明')">
          </p>
          <el-form label-position="top">
            <el-form-item :label="t('单码最大可用次数')">
              <el-input-number v-model="maxUses" :min="1" :max="100" />
            </el-form-item>
            <el-form-item :label="t('邀请码列表')">
              <el-input
                v-model="draft" type="textarea" :rows="9" class="mono"
                placeholder="MUSE-XXXX-YYYY&#10;MUSE-AAAA-BBBB"
              />
            </el-form-item>
            <el-button type="primary" :loading="importing" @click="doImport">{{ t('导入邀请码池') }}</el-button>
          </el-form>
        </el-card>
      </el-col>

      <el-col :md="15" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
              <span class="section-title" style="margin: 0" v-html="t('HTML:邀请码池统计', { a: inviteStats.available, r: inviteStats.remaining_uses })">
              </span>
              <el-space wrap>
                <el-select v-model="statusFilter" :placeholder="t('全部状态')" size="small" style="width: 120px" @change="load(true)">
                  <el-option :label="t('全部状态')" value="" />
                  <el-option :label="t('可用')" value="available" />
                  <el-option :label="t('已用满')" value="exhausted" />
                  <el-option :label="t('无效')" value="invalid" />
                </el-select>
                <el-button size="small" @click="load(false)"><el-icon><Refresh /></el-icon>{{ t('刷新') }}</el-button>
                <el-button size="small" :disabled="!availableCodesText" @click="copyText(availableCodesText)">{{ t('复制当页可用码') }}</el-button>
                <el-button size="small" type="danger" plain :disabled="!selected.length" @click="deleteSelected">
                  {{ t('删除选中 ({n})', { n: selected.length }) }}
                </el-button>
                <el-dropdown trigger="click" @command="deleteByStatus">
                  <el-button size="small" type="danger" plain>
                    {{ t('清理') }}<el-icon class="el-icon--right"><ArrowDown /></el-icon>
                  </el-button>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item command="exhausted">{{ t('清除全部已用满') }}</el-dropdown-item>
                      <el-dropdown-item command="invalid">{{ t('清除全部无效码') }}</el-dropdown-item>
                      <el-dropdown-item command="all" divided>{{ t('清空全部邀请码') }}</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </el-space>
            </div>
          </template>

          <el-table
            v-loading="loading" :data="rows" size="small" stripe
            @selection-change="(v) => (selected = v)"
          >
            <el-table-column type="selection" width="42" />
            <el-table-column :label="t('邀请码')" min-width="170">
              <template #default="{ row }">
                <el-button size="small" text type="primary" class="mono" @click="copyText(row.code)">
                  {{ row.code }}<el-icon style="margin-left: 4px"><CopyDocument /></el-icon>
                </el-button>
              </template>
            </el-table-column>
            <el-table-column :label="t('状态')" width="95">
              <template #default="{ row }">
                <StatusDot :type="statusType(row.status)" :text="statusLabel(row.status)" />
              </template>
            </el-table-column>
            <el-table-column :label="t('使用进度')" width="120" align="center">
              <template #default="{ row }">
                <span class="mono">{{ row.used_count }} / {{ row.max_uses }}</span>
                <el-tag v-if="row.in_flight" size="small" type="warning" style="margin-left: 4px">
                  +{{ row.in_flight }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="source" :label="t('来源')" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                <el-tag v-if="row.source === 'manual'" size="small" type="info" effect="plain">{{ t('手动导入') }}</el-tag>
                <el-tag v-else size="small" type="success" effect="plain">{{ row.source }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('入池时间')" width="155">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column :label="t('操作')" width="120" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="resetOne(row.code)">{{ t('重置') }}</el-button>
                <el-button size="small" text type="danger" @click="deleteOne(row.code)">{{ t('删除') }}</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty :description="t('邀请码池为空（可先添加 1 枚种子邀请码，或选择不兑码仅注册激活）')" :image-size="68" />
            </template>
          </el-table>

          <div style="display: flex; justify-content: center; margin-top: 14px">
            <el-pagination
              v-model:current-page="page" :page-size="PAGE_SIZE" :total="total"
              layout="prev, pager, next, total" background
            />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
