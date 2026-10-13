<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Refresh, Timer, ChatDotRound, Bell } from '@element-plus/icons-vue'
import { api, ApiError, type SootheSnapshot, type SootheValues, type RuntimeReplySnapshot, type RuntimeReplyValues } from './api'

const props = defineProps<{ csrf: string }>()
const emit = defineEmits<{ dirty: [value: boolean]; busy: [value: boolean]; unauthorized: [] }>()
const soothe = ref<SootheSnapshot | null>(null)
const runtime = ref<RuntimeReplySnapshot | null>(null)
const sootheDraft = reactive<SootheValues>({ soothe_mode: 'model', soothe_fixed_reply: '收到，我们加急处理' })
const runtimeDraft = reactive<RuntimeReplyValues>({ conversation_quiet_window_seconds: 120, conversation_poll_interval_seconds: 30,
  attachment_dispatch_interval_seconds: 5, routing_normal_confidence_threshold: 0.8, routing_human_confidence_threshold: 0.5, owner_notify_enabled: true })
const errors = reactive({ soothe: '', runtime: '' })
const conflicts = reactive({ soothe: false, runtime: false })
const loading = reactive({ soothe: false, runtime: false })
const saving = ref(false)
const review = ref<'soothe' | 'runtime' | null>(null)
const sootheSaved = ref(false)
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value)) as T
const sootheDirty = computed(() => soothe.value !== null && !same(soothe.value.values, sootheDraft))
const runtimeDirty = computed(() => runtime.value !== null && !same(runtime.value.values, runtimeDraft))
const dirty = computed(() => sootheDirty.value || runtimeDirty.value)
watch(dirty, (value) => emit('dirty', value), { immediate: true })
watch(saving, (value) => emit('busy', value))

type Scope = 'soothe' | 'runtime'
type NumericKey = Exclude<keyof RuntimeReplyValues, 'owner_notify_enabled'>
interface NumericField { key: NumericKey; label: string; help: string; min: number; max?: number; step: number; precision?: number; unit: string }
const timingFields: NumericField[] = [
  { key: 'conversation_quiet_window_seconds', label: '客户停止发言后等待时间', help: '从客户最后一条消息开始计时；继续发言会重新计时。设为 0 表示不等待静默窗口；最长 86400 秒。', min: 0, max: 86400, step: 10, precision: 0, unit: '秒' },
  { key: 'conversation_poll_interval_seconds', label: '任务扫描间隔', help: '调度器按这个间隔检查待回复需求，范围 1–86400 秒。', min: 1, max: 86400, step: 1, precision: 0, unit: '秒' },
  { key: 'attachment_dispatch_interval_seconds', label: '附件发送间隔', help: '连续发送多份图片或文件时，每份之间的等待时间，范围 0–86400 秒。', min: 0, max: 86400, step: 0.5, unit: '秒' },
]
const routingFields: NumericField[] = [
  { key: 'routing_normal_confidence_threshold', label: '正常回复阈值', help: '控制正常回复的意图门槛，同时作为结构化查询的信息完整度门槛。', min: 0, max: 1, step: 0.01, unit: '' },
  { key: 'routing_human_confidence_threshold', label: '人工处理阈值', help: '意图置信度低于此值时保持静默；不能高于正常回复阈值。', min: 0, max: 1, step: 0.01, unit: '' },
]
const labels: Record<keyof RuntimeReplyValues, string> = {
  ...Object.fromEntries([...timingFields, ...routingFields].map((field) => [field.key, field.label])),
  owner_notify_enabled: '负责人私聊通知',
} as Record<keyof RuntimeReplyValues, string>
const pendingCount = computed(() => runtime.value ? Object.values(runtime.value.fields).filter((field) => field.pending_restart).length : 0)
const readyCount = computed(() => runtime.value ? Object.values(runtime.value.fields).filter((field) => !field.pending_restart).length : 0)
const editableCount = computed(() => runtime.value ? Object.values(runtime.value.fields).filter((field) => field.editable).length : 0)
const changes = computed(() => {
  if (review.value === 'soothe' && soothe.value) {
    const result = []
    const names = { model: '模型生成', fixed: '固定话术' }
    if (sootheDraft.soothe_mode !== soothe.value.values.soothe_mode)
      result.push({ label: '安抚话术来源', before: names[soothe.value.values.soothe_mode], after: names[sootheDraft.soothe_mode] })
    if (sootheDraft.soothe_fixed_reply !== soothe.value.values.soothe_fixed_reply)
      result.push({ label: '固定安抚文案', before: soothe.value.values.soothe_fixed_reply || '空', after: sootheDraft.soothe_fixed_reply || '空' })
    return result
  }
  if (review.value === 'runtime' && runtime.value) return (Object.keys(labels) as (keyof RuntimeReplyValues)[])
    .filter((key) => runtimeDraft[key] !== runtime.value!.values[key])
    .map((key) => ({ label: labels[key], before: display(runtime.value!.values[key]), after: display(runtimeDraft[key]) }))
  return []
})
function display(value: unknown) { return typeof value === 'boolean' ? (value ? '开启' : '关闭') : String(value) }
function status(key: keyof RuntimeReplyValues) {
  const field = runtime.value?.fields[key]
  if (!field) return ''
  return field.source === 'environment' ? '环境变量覆盖' : field.pending_restart ? '待重启' : '运行值一致'
}
function handleError(scope: Scope, exc: unknown) {
  errors[scope] = exc instanceof ApiError && exc.status === 404 ? '新页面的后端接口尚未加载，请重启 FastGptAssistant 服务后刷新。'
    : exc instanceof Error ? exc.message : '操作失败，请稍后重试'
  if (exc instanceof ApiError && exc.status === 401) emit('unauthorized')
  conflicts[scope] = exc instanceof ApiError && exc.status === 409
}
async function load(scope: Scope) {
  loading[scope] = true; errors[scope] = ''
  try {
    if (scope === 'soothe') {
      soothe.value = await api<SootheSnapshot>('/reply-policy/soothe')
      Object.assign(sootheDraft, clone(soothe.value.values)); sootheSaved.value = false
    } else {
      runtime.value = await api<RuntimeReplySnapshot>('/reply-policy/runtime')
      Object.assign(runtimeDraft, clone(runtime.value.values))
    }
    conflicts[scope] = false
  } catch (exc) { handleError(scope, exc) } finally { loading[scope] = false }
}
async function reload(scope: Scope) {
  if ((scope === 'soothe' ? sootheDirty.value : runtimeDirty.value)) {
    try { await ElMessageBox.confirm('重新加载会丢弃这个区域的未保存修改，其他区域的修改会保留。', '重新加载配置', { confirmButtonText: '重新加载', cancelButtonText: '保留修改', type: 'warning' }) }
    catch { return }
  }
  await load(scope)
}
function openReview(scope: Scope) {
  errors[scope] = ''
  if (scope === 'soothe') {
    if (sootheDraft.soothe_mode === 'fixed' && !sootheDraft.soothe_fixed_reply.trim()) {
      errors.soothe = '固定话术模式必须填写安抚文案'; return
    }
    sootheDraft.soothe_fixed_reply = sootheDraft.soothe_fixed_reply.trim()
  } else {
    for (const field of [...timingFields, ...routingFields]) {
      const value = runtimeDraft[field.key]
      if (typeof value !== 'number' || !Number.isFinite(value) || value < field.min || (field.max !== undefined && value > field.max) || (field.precision === 0 && !Number.isInteger(value))) {
        errors.runtime = `请检查「${field.label}」的数值范围`; return
      }
    }
    if (runtimeDraft.routing_human_confidence_threshold > runtimeDraft.routing_normal_confidence_threshold) {
      errors.runtime = '人工阈值不能高于正常阈值'; return
    }
  }
  review.value = scope
}
async function save() {
  const scope = review.value
  if (!scope || saving.value) return
  const base = scope === 'soothe' ? soothe.value : runtime.value
  if (!base) return
  saving.value = true; errors[scope] = ''
  const values = clone(scope === 'soothe' ? sootheDraft : runtimeDraft)
  try {
    if (scope === 'soothe') {
      const result = await api<{ revision: string; values: SootheValues; history_pending?: boolean }>('/reply-policy/soothe', { method: 'PUT', headers: { 'X-CSRF-Token': props.csrf }, body: JSON.stringify({ ...values, revision: base.revision }) })
      soothe.value = { revision: result.revision, values: result.values, apply_mode: 'next_policy_read' }
      Object.assign(sootheDraft, clone(result.values))
      sootheSaved.value = true
      ElMessage.success('安抚话术已保存，下次读取群策略时生效。')
      if (result.history_pending) ElMessage.warning('配置已保存，修改记录结果待核对，请到修改记录页检查。')
    } else {
      runtime.value = await api<RuntimeReplySnapshot>('/reply-policy/runtime', { method: 'PUT', headers: { 'X-CSRF-Token': props.csrf }, body: JSON.stringify({ ...values, revision: base.revision }) })
      Object.assign(runtimeDraft, clone(runtime.value.values))
      ElMessage.success(runtime.value.restart_required ? '参数已保存，当前运行值未改变，需要重启服务。' : '参数已保存，与当前运行值一致。')
      if (runtime.value.history_pending) ElMessage.warning('配置已保存，修改记录结果待核对，请到修改记录页检查。')
    }
    conflicts[scope] = false
  } catch (exc) { handleError(scope, exc) } finally { saving.value = false; review.value = null }
}
onMounted(async () => { await Promise.allSettled([load('soothe'), load('runtime')]) })
</script>

<template>
  <div class="reply-policy-page">
  <div class="content reply-policy-content">
    <div class="page-heading"><div><span class="eyebrow">REPLY POLICY</span><h1>回复策略</h1><p>调整机器人如何回应需求，让回复节奏与团队协作保持一致。</p></div><el-tag type="info" effect="plain">分别保存 · 清晰生效</el-tag></div>
    <div class="policy-status-grid"><div class="policy-status"><span><el-icon><Check /></el-icon>运行值一致</span><strong>{{ runtime ? readyCount : '—' }}<small>项参数</small></strong></div><div class="policy-status"><span><el-icon><Timer /></el-icon>已保存待重启</span><strong>{{ runtime ? pendingCount : '—' }}<small>项参数</small></strong></div><div class="policy-status policy-status-note"><span>生效方式</span><p>安抚话术热加载<br>回复节奏与路由参数重启生效</p></div></div>
    <section class="policy-card" v-loading="loading.soothe">
      <div class="policy-card-heading"><div class="policy-section-title"><el-icon><ChatDotRound /></el-icon><div><h2>安抚话术</h2><p>用于非标准需求的客户回复，标准需求的澄清和资料不足回复使用原有规则。</p></div></div><el-tag type="success" effect="plain">支持热加载</el-tag></div>
      <el-alert v-if="errors.soothe" :title="errors.soothe" :type="conflicts.soothe ? 'warning' : 'error'" :closable="false" show-icon class="error-alert"><template v-if="conflicts.soothe">页面修改仍保留。请记录需要保留的内容，再重新加载。</template></el-alert>
      <div class="policy-soothe-grid"><div><label class="policy-label">话术来源</label><el-radio-group v-model="sootheDraft.soothe_mode" :disabled="!soothe || saving"><el-radio-button value="fixed">固定话术</el-radio-button><el-radio-button value="model">模型生成</el-radio-button></el-radio-group><p class="policy-help">{{ sootheDraft.soothe_mode === 'fixed' ? '每次使用同一句文案，便于统一措辞。' : '每个非标准需求由模型生成一句安抚回复；模型失败时使用既有兜底。' }}</p><div class="policy-preview"><span>客户回复预览</span><p>{{ sootheDraft.soothe_mode === 'fixed' ? sootheDraft.soothe_fixed_reply || '请填写固定安抚文案' : '文案由模型根据当前需求生成' }}</p></div></div><div><label for="soothe-text" class="policy-label">固定安抚文案 <small>{{ sootheDraft.soothe_mode === 'model' ? '模型模式下暂不使用' : '必填' }}</small></label><el-input id="soothe-text" v-model="sootheDraft.soothe_fixed_reply" type="textarea" :rows="4" maxlength="1000" show-word-limit :disabled="!soothe || saving" placeholder="例如：收到，我们加急处理" /><p class="policy-help">填写客户实际会看到的纯文本，最多 1000 个字符。</p></div></div>
      <div class="policy-card-footer"><span :class="{ 'policy-unsaved': sootheDirty }">{{ sootheDirty ? '有未保存的话术修改' : sootheSaved ? '已保存 · 下次读取群策略时生效' : '仅修改话术，不改动群名单和其他群策略' }}</span><div><el-button :icon="Refresh" :disabled="saving" @click="reload('soothe')">重新加载话术</el-button><el-button type="primary" :disabled="!sootheDirty || saving || conflicts.soothe" @click="openReview('soothe')">查看并保存话术</el-button></div></div>
    </section>
    <section class="policy-card" v-loading="loading.runtime">
      <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Timer /></el-icon><div><h2>回复节奏与路由</h2><p>调整客户需求的等待、识别与通知方式。</p></div></div><el-tag :type="pendingCount ? 'warning' : 'info'" effect="plain">{{ pendingCount ? '存在待重启参数' : '重启后生效' }}</el-tag></div>
      <el-alert v-if="errors.runtime" :title="errors.runtime" :type="conflicts.runtime ? 'warning' : 'error'" :closable="false" show-icon class="error-alert"><template v-if="conflicts.runtime">页面修改仍保留。请记录需要保留的内容，再重新加载。</template></el-alert>
      <el-alert v-if="runtime && editableCount < 6" title="部分参数由服务环境变量覆盖，页面将其设为只读。" type="info" show-icon :closable="false" class="error-alert" />
      <div class="policy-subheading"><h3>回复节奏</h3><p>等待时间与扫描间隔共同影响客户感知的回复延迟。</p></div>
      <div class="policy-numeric-grid"><div v-for="field in timingFields" :key="field.key" class="policy-field"><label :for="field.key" class="policy-label">{{ field.label }}</label><div class="policy-input-row"><el-input-number :key="field.key + String(!runtime || saving || !runtime.fields[field.key].editable)" :id="field.key" v-model="runtimeDraft[field.key]" :min="field.min" :max="field.max" :step="field.step" :precision="field.precision" controls-position="right" :disabled="!runtime || saving || !runtime.fields[field.key].editable" /><span>{{ field.unit }}</span></div><p class="policy-help">{{ field.help }}</p><div v-if="runtime" class="policy-field-status"><el-tag size="small" :type="runtime.fields[field.key].pending_restart ? 'warning' : 'info'" effect="plain">{{ status(field.key) }}</el-tag><span>当前运行：{{ display(runtime.active_values[field.key]) }} {{ field.unit }}</span></div><p v-if="runtime?.fields[field.key].source === 'environment'" class="policy-source">来源：{{ runtime.fields[field.key].source_key }}</p></div></div>
      <div class="policy-subheading"><h3>路由策略</h3><p>意图置信度决定是否回复，信息完整度决定是否需要澄清。</p></div>
      <div class="policy-numeric-grid policy-routing-grid"><div v-for="field in routingFields" :key="field.key" class="policy-field"><label :for="field.key" class="policy-label">{{ field.label }}</label><el-input-number :key="field.key + String(!runtime || saving || !runtime.fields[field.key].editable)" :id="field.key" v-model="runtimeDraft[field.key]" :min="field.min" :max="field.max" :step="field.step" controls-position="right" :disabled="!runtime || saving || !runtime.fields[field.key].editable" /><p class="policy-help">{{ field.help }}</p><div v-if="runtime" class="policy-field-status"><el-tag size="small" :type="runtime.fields[field.key].pending_restart ? 'warning' : 'info'" effect="plain">{{ status(field.key) }}</el-tag><span>当前运行：{{ display(runtime.active_values[field.key]) }}</span></div><p v-if="runtime?.fields[field.key].source === 'environment'" class="policy-source">来源：{{ runtime.fields[field.key].source_key }}</p></div></div>
      <div class="policy-rule-note">范围：0 ≤ 人工阈值 ≤ 正常回复阈值 ≤ 1。正常回复阈值同时影响结构化查询的澄清门槛；调整数值不会改变既有路由顺序。</div>
      <div class="policy-subheading"><h3><el-icon><Bell /></el-icon>负责人通知</h3></div><div class="switch-field policy-owner"><div><strong>负责人私聊通知</strong><small>允许机器人按既有规则私聊提醒群负责人。负责人需在群管理中配置。</small><div v-if="runtime" class="policy-field-status"><el-tag size="small" :type="runtime.fields.owner_notify_enabled.pending_restart ? 'warning' : 'info'" effect="plain">{{ status('owner_notify_enabled') }}</el-tag><span>当前运行：{{ display(runtime.active_values.owner_notify_enabled) }}</span></div><p v-if="runtime?.fields.owner_notify_enabled.source === 'environment'" class="policy-source">来源：{{ runtime.fields.owner_notify_enabled.source_key }}</p></div><el-switch v-model="runtimeDraft.owner_notify_enabled" aria-label="负责人私聊通知" :disabled="!runtime || saving || !runtime.fields.owner_notify_enabled.editable" /></div>
      <div class="policy-card-footer"><span :class="{ 'policy-unsaved': runtimeDirty }">{{ runtimeDirty ? '有未保存的参数修改' : pendingCount ? `${pendingCount} 项参数已保存，等待服务重启` : '配置与当前运行值一致' }}</span><div><el-button :icon="Refresh" :disabled="saving" @click="reload('runtime')">重新加载参数</el-button><el-button type="primary" :disabled="!runtimeDirty || saving || conflicts.runtime" @click="openReview('runtime')">查看并保存参数</el-button></div></div>
    </section>
    <div v-if="runtime?.restart_required" class="policy-restart-help"><h3>使已保存的参数生效</h3><p>在管理员 PowerShell 中执行以下命令，服务重启后刷新本页，核对“当前运行”值。</p><code>powershell -NoProfile -ExecutionPolicy Bypass -File "D:\瀚思AI客服\scripts\restart_app.ps1"</code><small>重启前请确认当前没有正在投递的附件。</small></div>
  </div>
  <el-dialog :model-value="review !== null" :title="review === 'soothe' ? '确认安抚话术修改' : '确认回复参数修改'" width="640px" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving" @close="review = null">
    <el-alert :title="review === 'soothe' ? '保存后，下次读取群策略时生效，无需重启。' : '保存只更新配置文件，当前运行值保持原值，重启后生效。'" :type="review === 'soothe' ? 'info' : 'warning'" :closable="false" show-icon />
    <div class="change-list"><div v-for="change in changes" :key="change.label" class="change-item"><el-tag size="small" type="info">修改</el-tag><div><strong>{{ change.label }}</strong><p>{{ change.before }} → {{ change.after }}</p></div></div></div><p class="muted">保存前会备份原文件；其他页面或人工修改了文件，本次保存将被拒绝。</p>
    <template #footer><el-button :disabled="saving" @click="review = null">返回编辑</el-button><el-button type="primary" :loading="saving" @click="save">确认保存 {{ changes.length }} 项修改</el-button></template>
  </el-dialog>
  </div>
</template>
