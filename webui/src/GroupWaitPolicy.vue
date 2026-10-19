<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Timer, Refresh } from '@element-plus/icons-vue'
import { api, ApiError, type GroupSnapshot } from './api'

const props = defineProps<{ csrf: string; defaultSeconds: number | null; busy: boolean }>()
const emit = defineEmits<{ dirty: [value: boolean]; busy: [value: boolean]; unauthorized: []; saved: [] }>()
const snapshot = ref<GroupSnapshot | null>(null)
const rows = ref<{ id: string; name: string; inherit: boolean; seconds: number | undefined }[]>([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const conflict = ref(false)
const review = ref(false)
const search = ref('')
const locked = computed(() => saving.value || props.busy)
const shown = computed(() => rows.value.filter(row => `${row.name} ${row.id}`.toLowerCase().includes(search.value.trim().toLowerCase())))
const changes = computed(() => rows.value.flatMap(row => {
  const before = snapshot.value?.groups.find(group => group.group_id === row.id)?.conversation_quiet_window_seconds
  const after = row.inherit ? null : row.seconds
  return before !== after ? [{ id: row.id, name: row.name || row.id, before, after }] : []
}))
const dirty = computed(() => snapshot.value !== null && changes.value.length > 0)
watch(dirty, value => emit('dirty', value), { immediate: true })
watch(saving, value => emit('busy', value))
watch(() => props.defaultSeconds, value => {
  if (value !== null) for (const row of rows.value) if (row.inherit) row.seconds = value
})
function display(value: number | null | undefined) {
  return value == null ? '跟随默认' : `${value} 秒`
}
function handleError(exc: unknown) {
  error.value = exc instanceof Error ? exc.message : '操作失败'
  conflict.value = exc instanceof ApiError && exc.status === 409
  if (exc instanceof ApiError && exc.status === 401) emit('unauthorized')
}
async function load() {
  loading.value = true; error.value = ''
  try {
    const result = await api<GroupSnapshot>('/groups')
    if (result.groups.some(group => !('conversation_quiet_window_seconds' in group)))
      throw new Error('按群等待时间的后端尚未加载，请重启 FastGptAssistant 服务后刷新。')
    snapshot.value = result
    rows.value = result.groups.map(group => ({ id: group.group_id, name: group.group_name,
      inherit: group.conversation_quiet_window_seconds === null,
      seconds: group.conversation_quiet_window_seconds ?? props.defaultSeconds ?? 120 }))
    conflict.value = false
  } catch (exc) { handleError(exc) } finally { loading.value = false }
}
async function reload() {
  if (dirty.value) {
    try { await ElMessageBox.confirm('重新加载会丢弃本区域未保存的群等待时间。', '重新加载', { confirmButtonText: '重新加载', cancelButtonText: '保留修改' }) }
    catch { return }
  }
  await load()
}
function openReview() {
  error.value = ''
  if (rows.value.some(row => !row.inherit && (typeof row.seconds !== 'number' || !Number.isInteger(row.seconds) || row.seconds < 0 || row.seconds > 86400))) {
    error.value = '每个群的等待时间须为 0–86400 的整数秒。'; return
  }
  review.value = true
}
async function save() {
  if (!snapshot.value || locked.value) return
  saving.value = true; error.value = ''
  const groups = snapshot.value.groups.map(group => {
    const row = rows.value.find(item => item.id === group.group_id)!
    return { ...group, conversation_quiet_window_seconds: row.inherit ? null : row.seconds! }
  })
  try {
    const result = await api<{ revision: string; history_pending?: boolean }>('/groups', {
      method: 'PUT', headers: { 'X-CSRF-Token': props.csrf },
      body: JSON.stringify({ revision: snapshot.value.revision, global_enabled: snapshot.value.global_enabled, groups }),
    })
    snapshot.value = { ...snapshot.value, revision: result.revision, groups }
    conflict.value = false
    review.value = false
    emit('saved')
    ElMessage.success('各群等待时间已保存，下次调度读取时生效，无需重启。')
    if (result.history_pending) ElMessage.warning('修改记录结果待核对，请到修改记录页检查。')
  } catch (exc) { handleError(exc); review.value = false } finally { saving.value = false }
}
async function refreshIfClean() { if (!dirty.value && !saving.value) await load() }
defineExpose({ refreshIfClean })
onMounted(load)
</script>

<template>
  <section class="policy-card" v-loading="loading">
    <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Timer /></el-icon><div><h2>各群客户发言后等待时间</h2><p>从该事项最后一条客户消息计时；继续补充会重新计时。</p></div></div><el-tag type="success" effect="plain">支持热加载</el-tag></div>
    <el-alert v-if="error" :title="error" :type="conflict ? 'warning' : 'error'" :closable="false" show-icon class="error-alert"><template v-if="conflict">草稿仍保留。群配置已被修改，请记录需要保留的内容后重新加载。</template></el-alert>
    <p class="policy-help">未单独配置的群跟随当前默认值：<strong>{{ defaultSeconds === null ? '未读取' : `${defaultSeconds} 秒` }}</strong>。0 秒表示不额外等待；实际回复仍需完成模型处理和任务扫描。</p>
    <el-input v-model="search" placeholder="搜索群名称或群 ID" clearable aria-label="搜索等待时间配置的群" class="group-wait-search" />
    <div v-for="row in shown" :key="row.id" class="group-wait-row">
      <div class="group-wait-name"><strong>{{ row.name || '未命名群' }}</strong><small>{{ row.id }}</small></div>
      <div class="group-wait-controls"><el-checkbox v-model="row.inherit" :disabled="locked">跟随默认</el-checkbox><el-input-number v-if="!row.inherit" v-model="row.seconds" :min="0" :max="86400" :precision="0" :step="10" controls-position="right" :disabled="locked" :aria-label="`${row.name || row.id}等待秒数`" /><span>{{ row.inherit ? (defaultSeconds === null ? '默认值未读取' : `${defaultSeconds} 秒`) : '秒' }}</span></div>
    </div>
    <el-empty v-if="snapshot && !shown.length" :description="rows.length ? '没有匹配的群' : '请先在群管理中添加客户群'" :image-size="64" />
    <div class="policy-card-footer"><span :class="{ 'policy-unsaved': dirty }">{{ dirty ? `${changes.length} 个群待保存` : '按群独立设置，保存后热加载' }}</span><div><el-button :icon="Refresh" :disabled="locked" @click="reload">重新加载群配置</el-button><el-button type="primary" :disabled="!dirty || locked || conflict" @click="openReview">查看并保存等待时间</el-button></div></div>
    <el-dialog v-model="review" title="确认各群等待时间" width="640px" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
      <el-alert title="保存后按新等待时间调度，包括尚未发送的需求，无需重启。" type="info" :closable="false" show-icon />
      <div class="change-list"><div v-for="change in changes" :key="change.id" class="change-item"><el-tag size="small" type="info">修改</el-tag><div><strong>{{ change.name }}</strong><p>{{ display(change.before) }} → {{ display(change.after) }}</p></div></div></div>
      <template #footer><el-button :disabled="saving" @click="review = false">返回编辑</el-button><el-button type="primary" :loading="saving" @click="save">确认保存 {{ changes.length }} 个群</el-button></template>
    </el-dialog>
  </section>
</template>
