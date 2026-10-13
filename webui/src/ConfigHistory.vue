<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Clock, FolderOpened, Document } from '@element-plus/icons-vue'
import { api, ApiError, type BackupEntry, type BackupTarget, type HistoryEntry, type PageResult, type RestorePreview, type RestoreResult } from './api'

const props = defineProps<{ csrf: string; active: boolean; hasDrafts: boolean }>()
const emit = defineEmits<{ busy: [value: boolean]; unauthorized: []; restored: [target: BackupTarget] }>()
const records = ref<HistoryEntry[]>([])
const historyCursor = ref<string | null>(null)
const backups = ref<BackupEntry[]>([])
const backupCursor = ref<string | null>(null)
const target = ref<BackupTarget>('groups')
const historyLoading = ref(false)
const backupLoading = ref(false)
const previewLoading = ref('')
const restoring = ref(false)
const historyError = ref('')
const backupError = ref('')
const restoreError = ref('')
const preview = ref<RestorePreview | null>(null)
const reviewOpen = ref(false)
const shownChanges = ref(100)
const scopes = { groups: '群管理', soothe: '安抚话术', reply: '回复节奏与路由', connections: '模型与连接', environment: '环境配置' }
const targets = { groups: '群策略', environment: '环境配置' }
const statusLabels = { applied: '已完成', failed: '未完成', prepared: '结果待核对' }
const canConfirm = computed(() => !!preview.value?.can_restore && !props.hasDrafts && !restoreError.value && !restoring.value)
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
const size = (value: number) => value < 1024 ? `${value} B` : `${(value / 1024).toFixed(1)} KB`
function previewValue(field: string, value: string) {
  if (field === 'groups.mode' || field === 'groups.added') {
    const modes: Record<string, string> = { active: '正式运行', shadow: '影子观察', disabled: '已停用' }
    return value.replace(/(?:^|(?<= · ))(active|shadow|disabled)$/, mode => modes[mode] ?? mode)
  }
  if (field === 'global.soothe_mode') return { fixed: '固定话术', model: '模型生成' }[value as 'fixed' | 'model'] ?? value
  return value
}
watch(restoring, value => emit('busy', value))

function errorMessage(exc: unknown) {
  if (exc instanceof ApiError && exc.status === 401) emit('unauthorized')
  return exc instanceof Error ? exc.message : '操作失败，请重试'
}
async function loadHistory(more = false) {
  if (historyLoading.value) return
  historyLoading.value = true; historyError.value = ''
  try {
    const result = await api<PageResult<HistoryEntry>>(`/history${more && historyCursor.value ? `?before=${encodeURIComponent(historyCursor.value)}` : ''}`)
    records.value = more ? [...records.value, ...result.items] : result.items
    historyCursor.value = result.next_cursor
  } catch (exc) {
    historyError.value = exc instanceof ApiError && exc.status === 404 ? '服务尚未加载修改记录接口，请重启服务后刷新。' : errorMessage(exc)
  } finally { historyLoading.value = false }
}
async function loadBackups(more = false) {
  if (backupLoading.value) return
  const selected = target.value
  backupLoading.value = true; backupError.value = ''
  try {
    const result = await api<PageResult<BackupEntry>>(`/history/backups/${selected}${more && backupCursor.value ? `?before=${encodeURIComponent(backupCursor.value)}` : ''}`)
    if (selected !== target.value) return
    backups.value = more ? [...backups.value, ...result.items] : result.items
    backupCursor.value = result.next_cursor
  } catch (exc) { if (selected === target.value) backupError.value = errorMessage(exc) }
  finally { backupLoading.value = false }
}
async function refresh() { await Promise.allSettled([loadHistory(), loadBackups()]) }
async function chooseTarget(value: BackupTarget) {
  if (backupLoading.value || restoring.value || previewLoading.value) return
  target.value = value; backups.value = []; backupCursor.value = null
  await loadBackups()
}
async function showPreview(backup_id: string) {
  if (previewLoading.value || restoring.value) return
  previewLoading.value = backup_id; backupError.value = ''; restoreError.value = ''
  try {
    preview.value = await api<RestorePreview>(`/history/backups/${target.value}/preview`, {
      method: 'POST', headers: { 'X-CSRF-Token': props.csrf }, body: JSON.stringify({ backup_id }),
    })
    shownChanges.value = 100; reviewOpen.value = true
  } catch (exc) { backupError.value = errorMessage(exc) }
  finally { previewLoading.value = '' }
}
async function restore() {
  if (!canConfirm.value || !preview.value) return
  const selected = preview.value
  restoring.value = true; restoreError.value = ''
  try {
    const result = await api<RestoreResult>(`/history/backups/${selected.target}/restore`, {
      method: 'POST', headers: { 'X-CSRF-Token': props.csrf }, body: JSON.stringify({
        backup_id: selected.backup_id, current_revision: selected.current_revision,
        backup_revision: selected.backup_revision, plan_revision: selected.plan_revision,
      }),
    })
    reviewOpen.value = false; preview.value = null
    emit('restored', selected.target)
    ElMessage.success(result.apply_mode === 'restart' ? '已恢复可管理参数，需要重启服务生效。' : '已恢复群策略，下次读取时生效。')
    if (result.history_pending) ElMessage.warning('配置已恢复，修改记录结果待核对，请刷新记录检查。')
    await refresh()
  } catch (exc) { restoreError.value = errorMessage(exc) }
  finally { restoring.value = false }
}
watch(() => props.active, active => { if (active) void refresh() })
onMounted(() => { if (props.active) void refresh() })
</script>

<template>
  <div class="content history-content">
    <div class="page-heading"><div><span class="eyebrow">CONFIGURATION HISTORY</span><h1>修改记录与恢复</h1><p>查看配置修改，预览历史备份，确认后恢复可管理的参数。</p></div><el-button :icon="Refresh" :loading="historyLoading || backupLoading" :disabled="restoring || !!previewLoading" @click="refresh">刷新记录</el-button></div>
    <div class="history-intro"><el-icon><Clock /></el-icon><p>修改记录从本版本启用后开始保存。已有历史备份仍可预览；记录只保存修改字段，不保存参数值或密钥。</p></div>
    <section class="policy-card">
      <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Document /></el-icon><div><h2>修改记录</h2><p>展示后台保存和恢复操作的时间、范围、修改字段与结果。人工直接编辑文件不会产生记录。</p></div></div><el-tag type="info">北京时间</el-tag></div>
      <el-alert v-if="historyError" :title="historyError" type="error" :closable="false" show-icon class="error-alert" />
      <div v-if="!records.length && !historyLoading && !historyError" class="runtime-no-errors">暂无修改记录。保存配置后，记录会显示在这里。</div>
      <div class="history-records" v-loading="historyLoading">
        <article v-for="entry in records" :key="entry.id"><div class="history-record-heading"><div><el-tag :type="entry.operation === 'restore' ? 'warning' : 'info'" size="small">{{ entry.operation === 'restore' ? '恢复备份' : '保存配置' }}</el-tag><strong>{{ scopes[entry.scope] }}</strong></div><time>{{ date(entry.created_at) }}</time><el-tag :type="entry.status === 'applied' ? 'success' : 'warning'" size="small">{{ statusLabels[entry.status] }}</el-tag></div><p>{{ entry.fields.map(field => field.label).join('、') }}</p><small v-if="entry.status === 'prepared'">已记录操作意图，完成结果未能确认；请核对当前配置。</small><small v-else-if="entry.status === 'failed'">操作未完成，请核对当前配置后重试。</small><div v-if="entry.backup_id" class="history-backup-reference">操作前备份：<code>{{ entry.backup_id }}</code></div><div v-if="entry.source_backup_id" class="history-backup-reference">恢复来源：<code>{{ entry.source_backup_id }}</code></div></article>
      </div>
      <div v-if="historyCursor" class="history-more"><el-button :loading="historyLoading" :disabled="restoring" @click="loadHistory(true)">加载更早记录</el-button></div>
    </section>
    <section class="policy-card">
      <div class="policy-card-heading"><div class="policy-section-title"><el-icon><FolderOpened /></el-icon><div><h2>配置备份</h2><p>备份保存的是修改前的配置。恢复前先预览差异，再备份当前配置；预览不会写入文件。</p></div></div></div>
      <div class="history-tabs"><button v-for="(label, key) in targets" :key="key" :class="{current: target === key}" :disabled="backupLoading || restoring || !!previewLoading" @click="chooseTarget(key)">{{ label }}</button></div>
      <div class="policy-rule-note">{{ target === 'groups' ? '恢复群名单、全局开关、群处理规则和安抚话术；保留当前文件中的其他配置。下次读取群策略时生效。' : '恢复 WebUI 开放的回复参数、连接参数和四项密钥；保留当前文件的其他参数、注释和换行。客服服务需重启生效。' }}</div>
      <el-alert v-if="backupError" :title="backupError" type="error" :closable="false" show-icon class="history-error" />
      <el-alert v-if="hasDrafts" title="其他配置页有未保存的草稿。可以预览备份，请先保存或放弃草稿，再确认恢复。" type="warning" :closable="false" show-icon class="history-error" />
      <div v-if="!backups.length && !backupLoading && !backupError" class="runtime-no-errors history-empty">暂无{{ targets[target] }}备份。下一次保存配置时会自动生成。</div>
      <div class="history-backups" v-loading="backupLoading"><article v-for="backup in backups" :key="backup.backup_id"><div><strong>{{ date(backup.created_at) }}</strong><small>{{ size(backup.size_bytes) }} · {{ targets[target] }}修改前快照</small><code>{{ backup.backup_id }}</code></div><el-button :loading="previewLoading === backup.backup_id" :disabled="restoring || (!!previewLoading && previewLoading !== backup.backup_id)" @click="showPreview(backup.backup_id)">预览差异</el-button></article></div>
      <div v-if="backupCursor" class="history-more"><el-button :loading="backupLoading" :disabled="restoring || !!previewLoading" @click="loadBackups(true)">加载更早备份</el-button></div>
      <p class="policy-help">备份可能包含凭证，页面不提供原文件下载。语法损坏的当前文件需在本机修复后，再使用页面恢复。</p>
    </section>
    <el-dialog v-model="reviewOpen" title="确认备份恢复" width="700px" :close-on-click-modal="false" :close-on-press-escape="!restoring" :show-close="!restoring">
      <template v-if="preview">
        <p class="history-preview-time">恢复目标：{{ targets[preview.target] }} · {{ date(preview.created_at) }}</p>
        <el-alert :title="preview.apply_mode === 'restart' ? '仅恢复 WebUI 已开放参数；密钥若有变化也会一并恢复，当前运行客户端需重启生效。' : '群规则和安抚话术将恢复为备份中的设置，下一次读取时生效，可能改变机器人参与方式。'" type="warning" :closable="false" show-icon />
        <el-alert v-if="restoreError" :title="restoreError" type="error" :closable="false" show-icon class="history-error"><template>当前配置尚未确认恢复。请关闭窗口，重新预览后再操作。</template></el-alert>
        <el-alert v-if="hasDrafts" title="请先处理其他页面未保存的草稿，再确认恢复。" type="warning" :closable="false" class="history-error" />
        <div v-if="!preview.changes.length" class="runtime-no-errors history-empty">可管理参数与备份一致，无需恢复。</div>
        <div class="change-list"><div v-for="(change, index) in preview.changes.slice(0, shownChanges)" :key="index" class="change-item"><el-tag :type="change.blocked ? 'danger' : change.secret ? 'warning' : 'info'" size="small">{{ change.blocked ? '不可恢复' : change.secret ? '密钥' : '修改' }}</el-tag><div><strong>{{ change.label }}</strong><p>{{ previewValue(change.field, change.before) }} → {{ previewValue(change.field, change.after) }}</p><small v-if="change.secret">将使用备份中的密钥值，内容始终隐藏。</small><small v-if="change.blocked">由服务环境变量覆盖，不能通过页面恢复。</small></div></div></div>
        <el-button v-if="preview.changes.length > shownChanges" text @click="shownChanges += 100">显示更多差异（剩余 {{ preview.changes.length - shownChanges }} 项）</el-button>
        <p class="muted">确认前重新核对当前文件、备份和恢复条件；有变化会拒绝旧预览。写入前自动备份当前配置，并记录恢复操作。</p>
      </template>
      <template #footer><el-button :disabled="restoring" @click="reviewOpen = false">返回列表</el-button><el-button type="primary" :disabled="!canConfirm" :loading="restoring" @click="restore">确认恢复 {{ preview?.changes.length ?? 0 }} 项修改</el-button></template>
    </el-dialog>
  </div>
</template>
