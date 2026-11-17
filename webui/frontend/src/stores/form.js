import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'

const KEY = 'muse_register_form_v2'

const defaults = {
  proxy: '',
  otpTimeout: 120,
  lang: 'ja',
  impersonate: 'chrome142',
  // 单次注册选项
  redeemMode: 'pool',          // 'pool' | 'single' | 'none'
  inviteCode: '',
  autoPoolOwnCode: true,
  sendCaa: true,
  sendFalco: true,
  resolveIp: true,
  firstName: '',
  lastName: '',
  dateOfBirth: '',
  // 全自动批量选项（对齐 musepy/runner.py）
  autoConcurrency: 3,
  autoMaxPerIp: 2,
  autoCoolDown: 1,
  autoTargetCount: 9,
  autoRedeemMode: 'pool',      // 'pool' | 'none'
  autoPoolOwnCodeBatch: true,
  autoSendCaa: true,
  autoSendFalco: true,
  autoResolveIp: true,
  autoStopOnReject: true,
}

export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive({ ...defaults, ...saved })

  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  return { form }
})
