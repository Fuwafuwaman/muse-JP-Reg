/**
 * 轻量 i18n（无第三方依赖）
 *
 * 约定：以「中文原文」作为 key，`ja` 词典保存日文译文。
 *  - 中文界面下不需要任何词典条目，命中不到 key 时原样返回中文；
 *  - 缺译的条目自动回退成中文，不会出现空白。
 *
 * 用法：
 *   import { t } from '@/i18n'
 *   t('开始注册')                      // 简单文本
 *   t('已删除 {n} 个', { n: 3 })       // 带占位符
 *   模板里：{{ t('开始注册') }} / :placeholder="t('请选择')" / v-html="t('带 <code>标记</code> 的文案')"
 */
import { computed, ref, watch } from 'vue'
import ja from './ja.js'
import elementJa from 'element-plus/es/locale/lang/ja'
import elementZh from 'element-plus/es/locale/lang/zh-cn'

const KEY = 'muse_ui_lang_v1'

/** 可选界面语言，顺序即切换器顺序 */
export const LANGS = [
  { value: 'ja', label: '日本語' },
  { value: 'zh', label: '中文' },
]

function initialLang() {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'ja' || saved === 'zh') return saved
  } catch (_) { /* 隐私模式下 localStorage 可能不可用 */ }
  // 本仓库是 JP 分叉：默认日文
  return 'ja'
}

export const lang = ref(initialLang())

export function setLang(v) {
  if (v === 'ja' || v === 'zh') lang.value = v
}
export function toggleLang() {
  lang.value = lang.value === 'ja' ? 'zh' : 'ja'
}

const TABLES = { ja }

/** 取当前语言译文（读 lang.value 以便在模板/计算属性中保持响应式） */
export function t(text, vars) {
  if (text === undefined || text === null) return ''
  const table = TABLES[lang.value]
  let s = table ? table[text] : undefined
  if (s === undefined) s = text
  if (vars) {
    for (const k of Object.keys(vars)) s = s.split(`{${k}}`).join(String(vars[k]))
  }
  return s
}

/** Element Plus 内置组件（分页 / 日期 / 下拉空白态…）的语言包 */
export const elementLocale = computed(() => (lang.value === 'ja' ? elementJa : elementZh))

/**
 * 后端（Python）日志/错误是中文，UI 只做「词条级」替换：
 * 长词优先，避免「注册完成」被拆成「登録完了」以外的碎片。
 */
const LOG_SUBS = [
  ['注册完成', '登録完了'], ['注册成功', '登録成功'], ['注册失败', '登録失敗'],
  ['开始注册', '登録開始'], ['启动注册', '登録開始'], ['正在注册', '登録中'],
  ['邮箱池无可用号', 'メールプールに空きなし'],
  ['触发停批保护暂停', '停止保護で一時停止を発動'],
  ['代理池已满载', 'プロキシプールが満杯'],
  ['共享代理节点', 'プロキシを共有'],
  ['无可用', '空きなし'], ['已满载', '満杯'], ['满载', '満杯'],
  ['已兑换', '交換済み'], ['兑换', '交換'],
  ['邀请码', '招待コード'], ['专属邀请码', '専用招待コード'],
  ['地区受限', '地域制限'], ['地区', '地域'], ['受限', '制限'],
  ['已注册', '登録済み'], ['未注册', '未登録'], ['已激活', '有効化済み'], ['激活', '有効化'],
  ['账号', 'アカウント'], ['邮箱', 'メール'], ['代理', 'プロキシ'], ['号池', 'プール'],
  ['超时', 'タイムアウト'], ['重试', '再試行'], ['拒绝', '拒否'], ['异常', '異常'],
  ['失败', '失敗'], ['错误', 'エラー'], ['警告', '警告'], ['完成', '完了'],
  ['成功', '成功'], ['无效', '無効'], ['有效', '有効'], ['未找到', '見つかりません'],
  ['已保存', '保存済み'], ['已删除', '削除済み'], ['已清空', '消去済み'],
  ['启动', '起動'], ['停止', '停止'], ['暂停', '一時停止'], ['恢复', '再開'],
  ['等待', '待機'], ['检测', '検出'], ['检测失败', '検出失敗'], ['切换', '切替'],
  ['换 IP', 'IP 切替'], ['换IP', 'IP切替'], ['并发', '並列'], ['批量', 'バッチ'],
  ['数量', '件数'], ['状态', 'ステータス'], ['阶段', 'フェーズ'], ['运行', '実行'],
  ['任务', 'タスク'], ['退出', '終了'], ['开始', '開始'], ['处理', '処理'],
  ['失败原因', '失敗理由'], ['密钥', 'キー'], ['令牌', 'トークン'], ['凭证', '認証情報'],
  ['注册', '登録'], ['数据', 'データ'], ['时间', '時刻'], ['备注', '備考'],
]

/** 本地化一行后端日志（中文界面下原样返回） */
export function localizeLog(text) {
  if (text === undefined || text === null) return ''
  const s = String(text)
  if (lang.value !== 'ja') return s
  if (ja[s] !== undefined) return ja[s]
  let out = s
  for (const [from, to] of LOG_SUBS) out = out.split(from).join(to)
  return out
}


/** 时间等 Intl 格式化用的 locale id */
export function localeId() {
  return lang.value === 'ja' ? 'ja-JP' : 'zh-CN'
}

watch(lang, (v) => {
  try { localStorage.setItem(KEY, v) } catch (_) {}
  if (typeof document !== 'undefined') document.documentElement.lang = v === 'ja' ? 'ja' : 'zh-CN'
}, { immediate: true })
