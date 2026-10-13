<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Refresh } from '@element-plus/icons-vue'
import { api, ApiError, type ConnectionSnapshot, type ConnectionValues, type ConnectionSecret, type SecretEdit, type ServiceName, type ProbeResult } from './api'

const props = defineProps<{ csrf: string }>()
const emit = defineEmits<{ dirty: [value: boolean]; busy: [value: boolean]; unauthorized: [] }>()
const base = ref<ConnectionSnapshot | null>(null)
const draft = reactive<ConnectionValues>({ llm_base_url: '', llm_model: '', llm_vision_model: '', fastgpt_api_url: '', fastgpt_dataset_id: '', gewe_api_url: '', gewe_app_id: '', lark_app_id: '', lark_bitable_app_token: '' })
const secretKeys: ConnectionSecret[] = ['llm_api_key', 'fastgpt_api_key', 'gewe_token', 'lark_app_secret']
const edits = reactive(Object.fromEntries(secretKeys.map((key) => [key, { action: 'keep', value: '' }])) as Record<ConnectionSecret, SecretEdit>)
const loading = ref(false)
const saving = ref(false)
const testing = ref<ServiceName | null>(null)
const error = ref('')
const conflict = ref(false)
const review = ref(false)
const results = reactive<Partial<Record<ServiceName, ProbeResult>>>({})
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const busy = computed(() => saving.value || testing.value !== null)
const dirty = computed(() => base.value !== null && ((Object.keys(draft) as (keyof ConnectionValues)[]).some((key) => draft[key] !== base.value!.values[key]) || secretKeys.some((key) => edits[key].action !== 'keep')))
watch(dirty, (value) => emit('dirty', value), { immediate: true })
watch(busy, (value) => emit('busy', value))
watch([draft, edits], () => { for (const key of Object.keys(results) as ServiceName[]) delete results[key] }, { deep: true })

interface Field { key: keyof ConnectionValues; label: string; hint: string; placeholder: string }
interface Section { id: ServiceName; title: string; description: string; fields: Field[]; secret: ConnectionSecret; secretLabel: string }
const sections: Section[] = [
  { id: 'llm', title: '对话与视觉模型', description: '负责需求理解、回复组织和图片分析。视觉模型留空时沿用主模型。', secret: 'llm_api_key', secretLabel: '模型 API Key', fields: [
    { key: 'llm_base_url', label: '模型服务地址', hint: '填写服务根地址或 /v1 地址，须支持本项目现有的对话接口。', placeholder: 'https://api.example.com/v1' },
    { key: 'llm_model', label: '主模型名称', hint: '填写服务商提供的模型 ID。', placeholder: '例如：deepseek-flash' },
    { key: 'llm_vision_model', label: '视觉模型名称', hint: '留空沿用主模型；模型列表测试不验证图片识别能力。', placeholder: '留空沿用主模型' },
  ] },
  { id: 'fastgpt', title: 'FastGPT 知识库', description: '用于官网知识检索，连接测试会发起一次知识库搜索。', secret: 'fastgpt_api_key', secretLabel: 'FastGPT API Key', fields: [
    { key: 'fastgpt_api_url', label: 'FastGPT 服务地址', hint: '填写 FastGPT 服务根地址，末尾 /api 也可接受。', placeholder: 'http://localhost:3001' },
    { key: 'fastgpt_dataset_id', label: '知识库 ID', hint: '从 FastGPT 知识库详情页获取。', placeholder: '填写知识库 ID' },
  ] },
  { id: 'gewe', title: 'GeWe 微信网关', description: '连接测试只查询实例在线状态，不发送微信消息、不修改回调。', secret: 'gewe_token', secretLabel: 'GeWe Token', fields: [
    { key: 'gewe_api_url', label: '微信网关地址', hint: '填写 GeWe API 服务根地址。', placeholder: 'http://api.geweapi.com' },
    { key: 'gewe_app_id', label: '实例 App ID', hint: '从 GeWe 后台实例详情获取。', placeholder: '填写实例 App ID' },
  ] },
  { id: 'lark', title: '飞书主数据来源', description: '验证应用凭证和表格访问；不会执行数据同步或下载附件。同步脚本在下次运行时读取新配置。', secret: 'lark_app_secret', secretLabel: '飞书 App Secret', fields: [
    { key: 'lark_app_id', label: '飞书应用 ID', hint: '从飞书开放平台的应用凭证页面获取。', placeholder: 'cli_…' },
    { key: 'lark_bitable_app_token', label: '多维表格标识', hint: '填写表格 URL 的 base/ 后标识，不是访问令牌。', placeholder: '填写多维表格 app token' },
  ] },
]
const pending = computed(() => base.value ? [...Object.values(base.value.fields), ...Object.values(base.value.secrets)].filter((field) => field.pending_restart).length : 0)
const configuredSecrets = computed(() => base.value ? Object.values(base.value.secrets).filter((field) => field.configured).length : 0)
const changes = computed(() => {
  if (!base.value) return []
  const result = []
  for (const section of sections) {
    for (const field of section.fields) if (draft[field.key] !== base.value.values[field.key])
      result.push({ label: field.label, detail: `${base.value.values[field.key] || '空'} → ${draft[field.key] || '空'}` })
    const action = edits[section.secret].action
    if (action !== 'keep') result.push({ label: section.secretLabel, detail: action === 'set' ? '替换为新密钥（内容不展示）' : '清除已保存密钥' })
  }
  return result
})
function handleError(exc: unknown) {
  error.value = exc instanceof ApiError && exc.status === 404 ? '连接配置的后端接口尚未加载，请重启 FastGptAssistant 服务后刷新。'
    : exc instanceof Error ? exc.message : '操作失败'
  if (exc instanceof ApiError && exc.status === 401) emit('unauthorized')
  conflict.value = exc instanceof ApiError && exc.status === 409
}
function apply(snapshot: ConnectionSnapshot) {
  base.value = snapshot
  Object.assign(draft, clone(snapshot.values))
  for (const key of secretKeys) Object.assign(edits[key], { action: 'keep', value: '' })
}
async function load() {
  loading.value = true; error.value = ''
  try { apply(await api<ConnectionSnapshot>('/connections')); conflict.value = false }
  catch (exc) { handleError(exc) } finally { loading.value = false }
}
async function reload() {
  if (dirty.value) {
    try { await ElMessageBox.confirm('重新加载会丢弃本页未保存的修改。', '重新加载连接配置', { confirmButtonText: '重新加载', cancelButtonText: '保留修改' }) }
    catch { return }
  }
  await load()
}
function payload() {
  if (!base.value) throw new Error('请先加载连接配置')
  for (const section of sections) {
    for (const field of section.fields) {
      const value = draft[field.key]
      if (/\p{Cc}/u.test(value) || value.includes('${')) throw new Error(`「${field.label}」不能包含控制字符或环境变量表达式`)
      if (field.key.endsWith('_url')) {
        let url: URL
        try { url = new URL(value) } catch { throw new Error(`请填写有效的「${field.label}」`) }
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error(`「${field.label}」不能包含用户名、密码、查询参数或片段`)
      }
    }
  }
  if (!draft.llm_model.trim()) throw new Error('请填写主模型名称')
  const secrets: Partial<Record<ConnectionSecret, { action: string; value?: string }>> = {}
  for (const key of secretKeys) {
    const edit = edits[key]
    if (edit.action === 'set') {
      const value = edit.value.trim()
      if (!value || /\p{Cc}/u.test(value) || value.includes('${')) throw new Error('替换密钥需要非空值，且不能包含控制字符或环境变量表达式')
      secrets[key] = { action: 'set', value }
    } else secrets[key] = { action: edit.action }
  }
  return { revision: base.value.revision, values: clone(draft), secrets }
}
function openReview() {
  try { payload(); error.value = ''; review.value = true } catch (exc) { handleError(exc) }
}
async function save() {
  if (busy.value) return
  let body
  try { body = payload() } catch (exc) { handleError(exc); review.value = false; return }
  saving.value = true; error.value = ''
  try {
    apply(await api<ConnectionSnapshot>('/connections', { method: 'PUT', headers: { 'X-CSRF-Token': props.csrf }, body: JSON.stringify(body) }))
    conflict.value = false
    ElMessage.success(base.value?.restart_required ? '连接配置已保存，客服服务重启后读取新值。' : '配置已保存，与服务启动时读取值一致。')
    if (base.value?.history_pending) ElMessage.warning('配置已保存，修改记录结果待核对，请到修改记录页检查。')
  } catch (exc) { handleError(exc) } finally { saving.value = false; review.value = false }
}
async function test(section: Section) {
  if (busy.value) return
  let body
  try { body = payload() } catch (exc) { handleError(exc); return }
  const address = section.id === 'lark' ? 'https://open.feishu.cn' : section.id === 'llm' ? draft.llm_base_url : section.id === 'fastgpt' ? draft.fastgpt_api_url : draft.gewe_api_url
  try { await ElMessageBox.confirm(`将向 ${address} 发起连接验证，使用当前填写的配置及保留的密钥。未保存的修改也会用于测试，测试不会保存配置。`, `测试${section.title}`, { confirmButtonText: '开始测试', cancelButtonText: '取消' }) }
  catch { return }
  testing.value = section.id; error.value = ''
  try { results[section.id] = await api<ProbeResult>(`/connections/test/${section.id}`, { method: 'POST', headers: { 'X-CSRF-Token': props.csrf }, body: JSON.stringify(body) }) }
  catch (exc) { handleError(exc) } finally { testing.value = null }
}
onMounted(load)
</script>

<template>
  <div class="connections-page">
  <div class="content connections-content" v-loading="loading">
    <div class="page-heading"><div><span class="eyebrow">MODELS & CONNECTIONS</span><h1>模型与连接</h1><p>管理智能客服使用的模型、知识库、微信网关和主数据来源。</p></div><el-button :icon="Refresh" :disabled="busy" @click="reload">重新加载连接配置</el-button></div>
    <el-alert v-if="error" :title="error" :type="conflict ? 'warning' : 'error'" :closable="false" show-icon class="error-alert"><template v-if="conflict">修改仍保留在页面。请记录需要保留的内容，再重新加载。</template></el-alert>
    <div class="policy-status-grid"><div class="policy-status"><span>已配置密钥</span><strong>{{ base ? configuredSecrets : '—' }}<small>组凭证</small></strong></div><div class="policy-status"><span>已保存待重启</span><strong>{{ base ? pending : '—' }}<small>项配置</small></strong></div><div class="policy-status policy-status-note"><span>密钥管理</span><p>保留、替换或清除<br>已保存密钥不回显</p></div></div>
    <section v-for="section in sections" :key="section.id" class="policy-card connection-card">
      <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Connection /></el-icon><div><h2>{{ section.title }}</h2><p>{{ section.description }}</p></div></div><el-button size="small" :disabled="!base || busy || conflict" :loading="testing === section.id" @click="test(section)">测试当前填写配置</el-button></div>
      <div class="connection-fields"><div v-for="field in section.fields" :key="field.key" class="connection-field"><label :for="field.key" class="policy-label">{{ field.label }}</label><el-input :id="field.key" v-model="draft[field.key]" :placeholder="field.placeholder" :maxlength="field.key.endsWith('_url') ? 2048 : 256" :disabled="!base || busy || !base.fields[field.key].editable" /><p class="policy-help">{{ field.hint }}</p><div v-if="base" class="policy-field-status"><el-tag size="small" :type="base.fields[field.key].pending_restart ? 'warning' : 'info'" effect="plain">{{ base.fields[field.key].source === 'environment' ? '环境变量覆盖' : base.fields[field.key].pending_restart ? '待重启' : '启动值一致' }}</el-tag><span>服务启动值：{{ base.active_values[field.key] || '空' }}</span></div><p v-if="base?.fields[field.key].source === 'environment'" class="policy-source">来源：{{ base.fields[field.key].source_key }}</p></div></div>
      <div class="connection-secret"><div class="connection-secret-heading"><label :for="`${section.secret}-action`" class="policy-label">{{ section.secretLabel }}</label><el-tag v-if="base" :type="base.secrets[section.secret].configured ? 'success' : 'info'" size="small" effect="plain">{{ base.secrets[section.secret].configured ? '已配置' : '未配置' }}</el-tag><el-tag v-if="base?.secrets[section.secret].pending_restart" type="warning" size="small" effect="plain">待重启</el-tag></div><div class="connection-secret-input"><el-select :id="`${section.secret}-action`" v-model="edits[section.secret].action" :aria-label="`${section.secretLabel}操作`" :disabled="!base || busy || !base.secrets[section.secret].editable"><el-option label="保留已保存密钥" value="keep" /><el-option label="替换为新密钥" value="set" /><el-option label="清除已保存密钥" value="clear" /></el-select><el-input v-if="edits[section.secret].action === 'set'" v-model="edits[section.secret].value" :aria-label="`新${section.secretLabel}`" type="password" show-password autocomplete="off" maxlength="8192" placeholder="输入新密钥，原密钥不回显" :disabled="busy" /><span v-else class="muted">{{ edits[section.secret].action === 'keep' ? '保留当前配置，不需要重复输入。' : '保存后会清除这项密钥。' }}</span></div><p v-if="base?.secrets[section.secret].source === 'environment'" class="policy-source">此凭证由环境变量 {{ base.secrets[section.secret].source_key }} 提供，页面只读。</p><p v-if="section.id === 'fastgpt' && base?.fastgpt_compatibility_key_configured" class="policy-help">兼容密钥已配置：清除应用 API Key 后仍可能使用兼容密钥。本页不编辑兼容密钥。</p></div>
      <el-alert v-if="results[section.id]" :title="results[section.id]!.message" :type="results[section.id]!.level === 'passed' ? 'success' : results[section.id]!.level === 'partial' ? 'warning' : 'error'" show-icon :closable="false" class="connection-test-result" />
    </section>
    <div class="policy-restart-help"><h3>配置与验证是两个状态</h3><p>连接测试使用本页草稿，测试通过不代表配置已经保存或服务已经使用新值。保存后重启客服服务，再刷新核对启动值。飞书同步脚本在下次执行时读取新配置。</p></div>
  </div>
  <footer class="savebar"><span :class="{ pending: dirty }"><i></i>{{ dirty ? `${changes.length} 项待保存修改` : pending ? `${pending} 项连接配置已保存，等待重启` : '连接配置已加载' }}</span><div><el-button :disabled="!dirty || busy" @click="reload">放弃连接修改</el-button><el-button type="primary" :disabled="!dirty || busy || conflict" @click="openReview">查看并保存连接</el-button></div></footer>
  <el-dialog v-model="review" title="确认模型与连接配置" width="640px" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving"><el-alert title="保存后，客服服务重启时读取新配置。密钥内容不会出现在修改对比中。" type="warning" :closable="false" show-icon /><div class="change-list"><div v-for="change in changes" :key="change.label" class="change-item"><el-tag size="small" type="info">修改</el-tag><div><strong>{{ change.label }}</strong><p>{{ change.detail }}</p></div></div></div><p class="muted">保存前备份原文件；版本冲突会拒绝覆盖。回复策略与本页共用环境配置文件。</p><template #footer><el-button :disabled="saving" @click="review = false">返回编辑</el-button><el-button type="primary" :loading="saving" @click="save">确认保存 {{ changes.length }} 项修改</el-button></template></el-dialog>
  </div>
</template>
