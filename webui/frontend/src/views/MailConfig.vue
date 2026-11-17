<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getMailConfig, getMailProviders, saveMailConfig, testMail } from '@/api/settings'
import FooterToolbar from '@/components/FooterToolbar.vue'

const providers = ref([])
const source = ref('icloud_relay')
const form = ref({})
const saved = ref({})
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)

const current = computed(
  () => providers.value.find((p) => p.kind === source.value) || null,
)
const fields = computed(() => current.value?.config_fields || [])
const canTest = computed(() => !!current.value && !current.value.pooled)

function phFor(f) {
  if (f.type === 'password' && saved.value[f.key] === '***') {
    return '已设置（留空则不修改）'
  }
  return f.placeholder || ''
}

async function load() {
  loading.value = true
  try {
    const [pr, cfg] = await Promise.all([getMailProviders(), getMailConfig()])
    providers.value = pr.providers || []
    saved.value = cfg.config || {}
    source.value = saved.value.mail_source || pr.current || 'icloud_relay'

    const next = {}
    for (const p of providers.value) {
      for (const f of p.config_fields) {
        next[f.key] = f.type === 'password' ? '' : (saved.value[f.key] ?? '')
      }
    }
    form.value = next
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function save() {
  const payload = { mail_source: source.value }
  for (const f of fields.value) {
    const v = (form.value[f.key] ?? '').trim()
    if (f.type === 'password' && !v) {
      if (saved.value[f.key] === '***') continue
    }
    payload[f.key] = v
  }

  const missing = fields.value
    .filter((f) => f.required)
    .filter((f) => {
      const v = (form.value[f.key] ?? '').trim()
      return !v && !(f.type === 'password' && saved.value[f.key] === '***')
    })
  if (missing.length) {
    ElMessage.warning('还没填：' + missing.map((f) => f.label).join('、'))
    return
  }

  saving.value = true
  try {
    await saveMailConfig(payload)
    ElMessage.success('邮箱来源已保存')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  try {
    const r = await testMail()
    ElMessage.success(r.message || '连通正常')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    testing.value = false
  }
}

onActivated(() => load())
load()
</script>

<template>
  <div class="page" v-loading="loading">
    <el-card shadow="never" style="max-width: 720px">
      <template #header>
        <span class="section-title" style="margin: 0">邮箱来源配置</span>
      </template>
      <p class="hint">
        <code>muse.ai</code> 注册走无密码邮箱 6 位验证码（OTP）认证。默认推荐使用 <b>iCloud 中转 / OTP 链接池</b>（直接联动本地 4173 工作台或 <code>otp-urls.txt</code>）。
      </p>

      <el-form label-position="top">
        <el-form-item label="当前启用的邮箱来源">
          <el-radio-group v-model="source">
            <el-radio v-for="p in providers" :key="p.kind" :value="p.kind">
              {{ p.display_name }}
            </el-radio>
          </el-radio-group>
        </el-form-item>

        <el-form-item v-if="current">
          <div class="caps">
            <el-tag size="small" :type="current.pooled ? 'warning' : 'success'">
              {{ current.pooled ? '号池型：从邮箱列表逐个领取' : '自建型：自动生成地址，无限量' }}
            </el-tag>
            <el-tag size="small" :type="current.ephemeral ? 'success' : 'info'">
              {{ current.ephemeral ? '每次新地址' : '固定邮箱地址' }}
            </el-tag>
            <el-tag v-if="current.line_segments > 0" size="small" type="info">
              导入格式 {{ current.line_segments }} 段
            </el-tag>
          </div>
        </el-form-item>

        <el-form-item v-for="f in fields" :key="f.key" :label="f.label">
          <el-input
            v-model="form[f.key]"
            :type="f.type === 'password' ? 'password' : 'text'"
            :show-password="f.type === 'password'"
            :placeholder="phFor(f)"
          />
          <div v-if="f.help" class="hint" style="margin-top: 4px">{{ f.help }}</div>
        </el-form-item>

        <el-alert
          v-if="current && !current.pooled && fields.length"
          type="warning" :closable="false" show-icon
          title="自建邮箱需确保域名 catch-all 收件转发正常，能接收来自 muse.ai / meta.com 的验证码邮件。"
        />

        <el-alert
          v-if="current && current.pooled"
          type="info" :closable="false" show-icon
          :title="`${current.display_name} 无需配置全局密钥，请前往「导入邮箱」页一键同步 4173 或粘贴邮箱列表。`"
        />
      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        当前邮箱来源：{{ current?.display_name || source }}
      </template>
      <el-button v-if="canTest" :loading="testing" @click="test">测试连通性</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>

<style scoped>
.caps {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
