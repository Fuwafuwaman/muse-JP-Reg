<script setup>
import { computed, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, isValidProxy, proxySchemeLabel } from '@/stores/proxy'
import { testProxies } from '@/api/proxy'
import { copyText } from '@/api/request'
import { t } from '@/i18n'

const proxyStore = useProxyStore()
const { list, count } = storeToRefs(proxyStore)

const draft = ref('')
const testResults = ref({})
const testingAll = ref(false)

const rows = computed(() =>
  list.value.map((p, i) => ({
    index: i + 1, proxy: p, valid: isValidProxy(p), result: testResults.value[p] || null,
  })),
)
const invalidCount = computed(() => rows.value.filter((r) => !r.valid).length)

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error(t('测试失败: ') + e.message)
  }
}
async function testOne(proxy) { await runTest([proxy]) }
async function testAll() {
  if (!count.value) return
  testingAll.value = true
  try { await runTest([...list.value]) }
  finally { testingAll.value = false }
}

function save() {
  if (!draft.value.trim()) { ElMessage.warning(t('请先粘贴代理')); return }
  const r = proxyStore.setFromText(draft.value)
  draft.value = ''
  ElMessage.success(t('已保存 {n} 个代理', { n: r.kept })
    + (r.duplicated ? t('（去重 {n} 个）', { n: r.duplicated }) : ''))
}
function append() {
  if (!draft.value.trim()) { ElMessage.warning(t('请先粘贴代理')); return }
  const r = proxyStore.append(draft.value)
  draft.value = ''
  ElMessage.success(t('已追加 {n} 个新代理', { n: r.added }))
}
async function clearAll() {
  if (!count.value) return
  try {
    await ElMessageBox.confirm(t('确定清空全部 {n} 个代理？', { n: count.value }), t('确认'), { type: 'warning', confirmButtonText: t('确定'), cancelButtonText: t('取消') })
    proxyStore.clear()
    ElMessage.success(t('已清空'))
  } catch (_) {}
}
function editInDraft() {
  draft.value = proxyStore.text
  ElMessage.info(t('已载入当前代理池，编辑后点「覆盖保存」'))
}
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">{{ t('批量导入 sticky 代理池') }}</span></template>
          <p class="hint" style="line-height: 1.65" v-html="t('HTML:代理池说明')"></p>
          <el-input
            v-model="draft" type="textarea" :rows="11" class="mono"
            placeholder="jp.proxy.example:8000:user:pass&#10;http://user:pass@1.2.3.4:8080&#10;socks5://127.0.0.1:7897"
          />
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" @click="save">{{ t('覆盖保存') }}</el-button>
            <el-button @click="append">{{ t('追加合并') }}</el-button>
            <el-button @click="editInDraft">{{ t('载入当前池') }}</el-button>
          </div>
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">
                {{ t('当前代理池（{n} 条{extra}）', { n: count, extra: invalidCount ? t('，{m} 条格式异常', { m: invalidCount }) : '' }) }}
              </span>
              <div style="display: flex; gap: 8px">
                <el-button size="small" type="primary" plain :loading="testingAll" :disabled="!count" @click="testAll">{{ t('测试全部') }}</el-button>
                <el-button size="small" :disabled="!count" @click="copyText(proxyStore.text)">{{ t('复制全部') }}</el-button>
                <el-button size="small" type="danger" plain :disabled="!count" @click="clearAll">{{ t('清空') }}</el-button>
              </div>
            </div>
          </template>

          <el-table :data="rows" size="small" stripe max-height="440">
            <el-table-column prop="index" label="#" width="48" />
            <el-table-column prop="proxy" :label="t('代理地址')" min-width="200" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.proxy }}</span></template>
            </el-table-column>
            <el-table-column :label="t('格式')" width="70">
              <template #default="{ row }">
                <el-tag :type="row.valid ? 'success' : 'danger'" size="small" effect="light">
                  {{ t(row.valid ? '正常' : '异常') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('生效协议')" width="115">
              <template #default="{ row }">
                <span class="mono" style="font-size: 12px">{{ proxySchemeLabel(row.proxy) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('连通性与出口 IP')" min-width="160">
              <template #default="{ row }">
                <template v-if="!row.result">
                  <span class="hint">{{ t('未测') }}</span>
                </template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">{{ t('测试中…') }}</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">{{ t('正常 {n}ms', { n: row.result.latency_ms }) }}</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 6px">{{ row.result.ip }}</span>
                </template>
                <el-tooltip v-else :content="row.result.error || t('连接失败')" placement="top">
                  <el-tag type="danger" size="small">{{ t('失败') }}</el-tag>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column :label="t('操作')" width="120" fixed="right">
              <template #default="{ row }">
                <el-button
                  size="small" text type="primary"
                  :loading="row.result && row.result.status === 'testing'"
                  @click="testOne(row.proxy)"
                >{{ t('测试') }}</el-button>
                <el-button size="small" text type="danger" @click="proxyStore.remove(row.proxy)">{{ t('删除') }}</el-button>
              </template>
            </el-table-column>
            <template #empty>{{ t('暂无代理，请在左侧粘贴导入') }}</template>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
