<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox, type FormInstance, type FormRules } from 'element-plus'
import { ChatDotRound, Plus, Search, Setting, SwitchButton, Refresh, Check, ArrowRight, Edit, Delete } from '@element-plus/icons-vue'
import { api, ApiError, type GroupPolicy, type GroupSnapshot, type BackupTarget } from './api'
import ReplyPolicy from './ReplyPolicy.vue'
import Connections from './Connections.vue'
import RuntimeStatus from './RuntimeStatus.vue'
import ConfigHistory from './ConfigHistory.vue'
import ChatArchive from './ChatArchive.vue'
import handsinLogo from './assets/handsin-logo.png'

const authenticated = ref(false)
const configured = ref(true)
const booting = ref(true)
const busy = ref(false)
const saving = ref(false)
const loading = ref(false)
const error = ref('')
const password = ref('')
const csrf = ref('')
type Page = 'groups' | 'reply' | 'connections' | 'status' | 'history' | 'chats'
const page = ref<Page>(window.location.hash === '#chats' ? 'chats' : window.location.hash === '#history' ? 'history' : window.location.hash === '#status' ? 'status' : window.location.hash === '#connections' ? 'connections' : window.location.hash === '#reply' ? 'reply' : 'groups')
const pageLabels = { groups: '群管理', reply: '回复策略', connections: '模型与连接', status: '运行状态', history: '修改记录与恢复', chats: '聊天记录' }
const chatVisited = ref(page.value === 'chats')
const historyVisited = ref(page.value === 'history')
const historyBusy = ref(false)
const statusVisited = ref(page.value === 'status')
const connectionVisited = ref(page.value === 'connections')
const connectionDirty = ref(false)
const connectionBusy = ref(false)
const policyVisited = ref(page.value === 'reply')
const policyDirty = ref(false)
const policyBusy = ref(false)
const snapshot = ref<GroupSnapshot | null>(null)
const groups = ref<GroupPolicy[]>([])
const globalEnabled = ref(false)
const query = ref('')
const filter = ref('all')
const dialog = ref(false)
const review = ref(false)
const editingId = ref<string | null>(null)
const formRef = ref<FormInstance>()
const staffText = ref('')
const aliasText = ref('')
const lastSaved = ref('')
const conflict = ref(false)
const modes = { active: '正式运行', shadow: '影子观察', disabled: '已停用' }
const modeTypes = { active: 'success', shadow: 'warning', disabled: 'info' } as const

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T }
function blankGroup(): GroupPolicy {
  return { group_id: '', group_name: '', mode: 'disabled', owner_id: '', customer_service_ids: [], bot_aliases: [],
    require_customer_mention: true, allow_images: true, allow_files: true, reply_to_staff_when_mentioned: true, native_quote_replies: false, associate_customer_demands: false, conversation_quiet_window_seconds: null }
}
const form = reactive<GroupPolicy>(blankGroup())
const rules: FormRules = {
  group_id: [
    { required: true, message: '请输入群 ID', trigger: 'blur' },
    { pattern: /^[^\s@]+@chatroom$/, message: '群 ID 应以 @chatroom 结尾，不能含空格', trigger: 'blur' },
    { max: 128, message: '群 ID 最长 128 个字符', trigger: 'blur' },
    { validator: (_rule, value, callback) => {
      if (groups.value.some((group) => group.group_id === value.trim() && group.group_id !== editingId.value)) callback(new Error('这个群已在列表中'))
      else callback()
    }, trigger: 'blur' },
  ],
  group_name: [{ max: 120, message: '群名称最长 120 个字符', trigger: 'blur' }],
  owner_id: [{ max: 128, message: '负责人 ID 最长 128 个字符', trigger: 'blur' }],
}
const dirty = computed(() => snapshot.value !== null &&
  (globalEnabled.value !== snapshot.value.global_enabled || JSON.stringify(groups.value) !== JSON.stringify(snapshot.value.groups)))
const anyDirty = computed(() => dirty.value || policyDirty.value || connectionDirty.value)
async function changePage(value: Page) {
  if (saving.value || policyBusy.value || connectionBusy.value || historyBusy.value) return
  page.value = value
  window.history.replaceState(null, '', `#${value}`)
  if (value === 'reply') policyVisited.value = true
  else if (value === 'connections') connectionVisited.value = true
  else if (value === 'status') statusVisited.value = true
  else if (value === 'history') historyVisited.value = true
  else if (value === 'chats') chatVisited.value = true
  else if (value === 'groups' && !dirty.value) await load()
}
async function configurationRestored(target: BackupTarget) {
  policyVisited.value = false; policyDirty.value = false
  if (target === 'environment') { connectionVisited.value = false; connectionDirty.value = false }
  else await load()
}
const counts = computed(() => ({ total: groups.value.length, active: groups.value.filter((g) => g.mode === 'active').length,
  shadow: groups.value.filter((g) => g.mode === 'shadow').length }))
const visibleGroups = computed(() => groups.value.filter((group) =>
  (filter.value === 'all' || group.mode === filter.value) &&
  `${group.group_name} ${group.group_id}`.toLowerCase().includes(query.value.toLowerCase().trim())))
const changes = computed(() => {
  if (!snapshot.value) return []
  const changes: { label: string; detail: string; kind: string }[] = []
  if (globalEnabled.value !== snapshot.value.global_enabled)
    changes.push({ label: '全局群处理', detail: globalEnabled.value ? '关闭 → 开启' : '开启 → 关闭', kind: '开关' })
  const labels: Record<string, string> = { group_name: '群名称', mode: '运行模式', owner_id: '负责人', customer_service_ids: '客服名单',
    bot_aliases: '机器人昵称', require_customer_mention: '客户必须 @', allow_images: '接收图片', allow_files: '接收文件', reply_to_staff_when_mentioned: '回复客服 @', native_quote_replies: '引用原问题回复', associate_customer_demands: '同一事项只回复一次', conversation_quiet_window_seconds: '客户发言后等待时间' }
  for (const group of groups.value) {
    const previous = snapshot.value.groups.find((g) => g.group_id === group.group_id)
    if (!previous) changes.push({ label: group.group_name || group.group_id, detail: `新增群 · ${modes[group.mode]}`, kind: '新增' })
    else {
      const fields = Object.keys(labels).filter((key) => JSON.stringify(previous[key as keyof GroupPolicy]) !== JSON.stringify(group[key as keyof GroupPolicy]))
      if (fields.length) changes.push({ label: group.group_name || group.group_id,
        detail: fields.map((key) => {
          const render = (value: unknown) => value == null ? '跟随默认' : typeof value === 'boolean' ? (value ? '开启' : '关闭')
            : Array.isArray(value) ? (value.join('、') || '空')
            : key === 'mode' ? modes[value as keyof typeof modes] : (String(value) || '空')
          return `${labels[key]}：${render(previous[key as keyof GroupPolicy])} → ${render(group[key as keyof GroupPolicy])}`
        }).join('；'), kind: '修改' })
    }
  }
  for (const old of snapshot.value.groups) if (!groups.value.some((g) => g.group_id === old.group_id))
    changes.push({ label: old.group_name || old.group_id, detail: '从授权列表移除，后续消息将不再处理', kind: '移除' })
  return changes
})

function handleError(exc: unknown) {
  error.value = exc instanceof Error ? exc.message : '操作失败'
  if (exc instanceof ApiError && exc.status === 401) { authenticated.value = false; csrf.value = '' }
  if (exc instanceof ApiError && exc.status === 409) conflict.value = true
}
async function load() {
  loading.value = true; error.value = ''
  try {
    snapshot.value = await api<GroupSnapshot>('/groups')
    groups.value = clone(snapshot.value.groups)
    globalEnabled.value = snapshot.value.global_enabled
    conflict.value = false
  } catch (exc) { handleError(exc) } finally { loading.value = false }
}
async function refresh() {
  if (dirty.value) {
    try { await ElMessageBox.confirm('重新加载会丢弃当前未保存的修改。', '重新加载配置', { confirmButtonText: '重新加载', cancelButtonText: '保留修改', type: 'warning' }) }
    catch { return }
  }
  await load()
}
async function login() {
  if (!password.value || busy.value) return
  busy.value = true; error.value = ''
  try {
    const result = await api<{ csrf_token: string }>('/login', { method: 'POST', body: JSON.stringify({ password: password.value }) })
    password.value = ''; csrf.value = result.csrf_token; authenticated.value = true
    await load()
  } catch (exc) { handleError(exc) } finally { busy.value = false }
}
async function logout() {
  if (historyBusy.value || saving.value || policyBusy.value || connectionBusy.value) return
  if (anyDirty.value) {
    try { await ElMessageBox.confirm('退出会丢弃未保存的修改。', '退出管理后台', { confirmButtonText: '退出', cancelButtonText: '继续编辑' }) }
    catch { return }
  }
  try {
    await api('/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf.value } })
    authenticated.value = false; csrf.value = ''; snapshot.value = null; groups.value = []; error.value = ''; policyVisited.value = false; policyDirty.value = false; page.value = 'groups'; connectionVisited.value = false; connectionDirty.value = false
    statusVisited.value = false
    chatVisited.value = false
    historyVisited.value = false; historyBusy.value = false
    window.history.replaceState(null, '', '#groups')
  } catch (exc) { handleError(exc) }
}
function editGroup(group?: GroupPolicy) {
  editingId.value = group?.group_id ?? null
  Object.assign(form, clone(group ?? blankGroup()))
  staffText.value = form.customer_service_ids.join('\n'); aliasText.value = form.bot_aliases.join('\n')
  dialog.value = true
}
function parseList(value: string) { return value.split(/\r?\n/).map((v) => v.trim()).filter(Boolean) }
async function acceptGroup() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  const staff = parseList(staffText.value); const aliases = parseList(aliasText.value)
  if (staff.length > 200 || aliases.length > 50 || [...staff, ...aliases].some((v) => v.length > 128)) {
    ElMessage.error('客服名单最多 200 项、昵称最多 50 项，每项最长 128 个字符'); return
  }
  if (new Set(staff).size !== staff.length || new Set(aliases).size !== aliases.length) { ElMessage.error('名单中有重复项，请检查'); return }
  const row = { ...clone(form), group_id: form.group_id.trim(), group_name: form.group_name.trim(), owner_id: form.owner_id.trim(), customer_service_ids: staff, bot_aliases: aliases }
  const index = groups.value.findIndex((g) => g.group_id === editingId.value)
  if (index >= 0) groups.value[index] = row
  else groups.value.push(row)
  dialog.value = false
  ElMessage.info('已加入待保存修改，请点击右上角「查看并保存」')
}
async function removeGroup(group: GroupPolicy) {
  try { await ElMessageBox.confirm(`移除「${group.group_name || group.group_id}」后，该群将不再被机器人处理。`, '移除授权群', { confirmButtonText: '加入待移除修改', cancelButtonText: '取消', type: 'warning' }) }
  catch { return }
  groups.value = groups.value.filter((g) => g.group_id !== group.group_id)
}
async function save() {
  if (!snapshot.value || saving.value) return
  saving.value = true; error.value = ''
  try {
    const result = await api<{ revision: string; history_pending?: boolean }>('/groups', { method: 'PUT', headers: { 'X-CSRF-Token': csrf.value },
      body: JSON.stringify({ revision: snapshot.value.revision, global_enabled: globalEnabled.value, groups: groups.value }) })
    snapshot.value = { ...snapshot.value, revision: result.revision, groups: clone(groups.value), global_enabled: globalEnabled.value }
    review.value = false; conflict.value = false
    lastSaved.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Shanghai' })
    ElMessage.success('保存成功。下一次读取群策略时生效，无需重启。')
    if (result.history_pending) ElMessage.warning('配置已保存，修改记录结果待核对，请到修改记录页检查。')
  } catch (exc) { handleError(exc); review.value = false } finally { saving.value = false }
}
function preventUnload(event: BeforeUnloadEvent) { if (anyDirty.value) { event.preventDefault(); event.returnValue = '' } }
onMounted(async () => {
  window.addEventListener('beforeunload', preventUnload)
  try {
    const session = await api<{ configured: boolean; authenticated: boolean; csrf_token: string }>('/session')
    configured.value = session.configured; authenticated.value = session.authenticated; csrf.value = session.csrf_token || ''
    if (session.authenticated) await load()
  } catch (exc) { handleError(exc) } finally { booting.value = false }
})
onBeforeUnmount(() => window.removeEventListener('beforeunload', preventUnload))
</script>

<template>
  <div v-if="booting" class="boot"><img class="brand-logo" :src="handsinLogo" alt="Handsin 瀚思" /><p>正在连接管理后台…</p></div>
  <div v-else-if="!authenticated" class="login-layout">
    <div class="login-brand"><img class="brand-logo" :src="handsinLogo" alt="Handsin 瀚思" /><span>AI 客服工作空间</span></div>
    <div class="login-story"><span class="eyebrow">CUSTOMER SERVICE WORKSPACE</span><h1>让每一次服务，<br>都恰到好处。</h1><p>管理客户群与协作规则，<br>让智能客服与团队有序配合。</p><div class="story-line"></div><span class="story-note">瀚思 · 服务配置中心</span></div>
    <section class="login-card">
      <span class="eyebrow">管理后台</span><h2>欢迎回来</h2><p class="muted">使用本机设置的管理员密码登录。</p>
      <el-alert v-if="!configured" title="首次使用：请先设置管理员密码" type="info" :closable="false" show-icon>
        <p>在项目根目录运行：</p><code>.venv\Scripts\python.exe scripts/setup_admin.py</code>
        <p>设置完成后刷新此页面。</p>
      </el-alert>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="error-alert" />
      <form @submit.prevent="login">
        <label for="admin-password">管理员密码</label>
        <el-input id="admin-password" v-model="password" type="password" show-password autocomplete="current-password" placeholder="请输入管理员密码" size="large" :disabled="!configured" />
        <el-button type="primary" size="large" native-type="submit" :loading="busy" :disabled="!configured || !password" class="login-submit">登录管理后台 <el-icon><ArrowRight /></el-icon></el-button>
      </form>
      <p class="login-foot">仅授权管理员可访问。局域网使用管理员提供的地址，公网登录请使用 HTTPS。</p>
    </section>
  </div>

  <div v-else class="workspace">
    <aside class="sidebar">
      <div class="brand"><img class="brand-logo" :src="handsinLogo" alt="Handsin 瀚思" /><small>AI 客服 · 服务配置中心</small></div>
      <div class="nav-caption">工作空间</div>
      <button class="nav-item" :class="{ selected: page === 'groups' }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage('groups')"><el-icon><ChatDotRound /></el-icon>群管理<span class="nav-count">{{ groups.length }}</span></button>
      <button class="nav-item" :class="{ selected: page === 'reply' }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage('reply')"><el-icon><Setting /></el-icon>回复策略</button>
      <button class="nav-item" :class="{ selected: page === 'connections' }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage('connections')"><el-icon><Setting /></el-icon>模型与连接</button>
      <button class="nav-item" :class="{ selected: page === 'status' }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage('status')"><el-icon><Refresh /></el-icon>运行状态</button>
      <button class="nav-item" :class="{ selected: page === 'history' }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage('history')"><el-icon><Refresh /></el-icon>修改记录与恢复</button>
      <button class="nav-item" :class="{ selected: page === 'chats' }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage('chats')"><el-icon><ChatDotRound /></el-icon>聊天记录</button>
      <div class="sidebar-note"><el-icon><Setting /></el-icon><p>配置服务方式<small>让机器人与客服有序协作</small></p></div>
      <div class="sidebar-bottom"><span class="avatar">管</span><div>管理员<small>本地配置管理</small></div><el-button text :icon="SwitchButton" aria-label="退出登录" title="退出登录" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="logout" /></div>
    </aside>
    <main class="main-area">
      <header class="topbar"><span>工作空间 <span class="slash">/</span> <strong>{{ pageLabels[page] }}</strong></span><span class="topbar-note"><i></i>{{ page === 'groups' ? '群策略支持热加载' : page === 'status' ? '状态仅供查看' : page === 'chats' ? '历史记录只读查看' : '配置与运行状态分别展示' }}</span></header>
      <nav class="mobile-section-tabs"><button v-for="(label, value) in pageLabels" :key="value" :class="{ current: page === value }" :disabled="saving || policyBusy || connectionBusy || historyBusy" @click="changePage(value)">{{ label }}</button></nav>
      <RuntimeStatus v-if="statusVisited" v-show="page === 'status'" :active="page === 'status'" @navigate="changePage" @unauthorized="authenticated = false; csrf = ''" />
      <ConfigHistory v-if="historyVisited" v-show="page === 'history'" :active="page === 'history'" :csrf="csrf" :has-drafts="anyDirty" @busy="historyBusy = $event" @restored="configurationRestored" @unauthorized="authenticated = false; csrf = ''" />
      <ChatArchive v-if="chatVisited" v-show="page === 'chats'" :active="page === 'chats'" :csrf="csrf" @unauthorized="authenticated = false; csrf = ''" />
      <ReplyPolicy v-if="policyVisited" v-show="page === 'reply'" :csrf="csrf" @dirty="policyDirty = $event" @busy="policyBusy = $event" @groups-saved="!dirty && load()" @unauthorized="authenticated = false; csrf = ''" />
      <Connections v-if="connectionVisited" v-show="page === 'connections'" :csrf="csrf" @dirty="connectionDirty = $event" @busy="connectionBusy = $event" @unauthorized="authenticated = false; csrf = ''" />
      <div v-show="page === 'groups'" class="content">
        <div class="page-heading"><div><span class="eyebrow">GROUP MANAGEMENT</span><h1>客户群管理</h1><p>为每个客户群设置机器人参与方式与团队协作规则。</p></div><el-button type="primary" :icon="Plus" size="large" :disabled="!snapshot || loading || saving" @click="editGroup()">添加客户群</el-button></div>
        <el-alert v-if="error" :title="error" :type="conflict ? 'warning' : 'error'" show-icon :closable="false" class="error-alert"><template v-if="conflict">当前修改仍保留在页面中。请记录需要保留的修改，再重新加载最新配置。</template></el-alert>
        <div class="stats-grid"><div class="stat"><span>已配置客户群</span><strong>{{ counts.total }}<small>个群</small></strong></div><div class="stat"><span><i class="dot green"></i>正式运行</span><strong>{{ counts.active }}<small>个群</small></strong></div><div class="stat"><span><i class="dot amber"></i>影子观察</span><strong>{{ counts.shadow }}<small>个群</small></strong></div><div class="stat gate-stat"><div><span>全局群处理</span><strong class="gate-label">{{ globalEnabled ? '已开启' : '已关闭' }}</strong></div><el-switch v-model="globalEnabled" :disabled="!snapshot || loading || saving" aria-label="全局群处理" /><p>{{ globalEnabled ? '按各群的策略处理消息' : '所有客户群暂停处理消息' }}</p></div></div>
        <section class="group-panel" v-loading="loading">
          <div class="panel-heading"><div><h2>授权群列表 <span>{{ groups.length }}</span></h2><p>仅列表中的群会进入群消息处理流程。</p></div><div class="panel-actions"><el-input v-model="query" placeholder="搜索群名称或 ID" :prefix-icon="Search" clearable class="search-input" aria-label="搜索客户群" /><el-button :icon="Refresh" :disabled="saving" @click="refresh">重新加载</el-button></div></div>
          <div class="filter-row"><button v-for="option in [{ value: 'all', label: '全部客户群' }, { value: 'active', label: '正式运行' }, { value: 'shadow', label: '影子观察' }, { value: 'disabled', label: '已停用' }]" :key="option.value" :class="{ current: filter === option.value }" @click="filter = option.value">{{ option.label }}</button></div>
          <el-table :data="visibleGroups" class="groups-table" row-key="group_id" empty-text="没有符合条件的客户群">
            <el-table-column label="客户群" min-width="260"><template #default="{ row }"><div class="group-cell"><span class="group-icon"><el-icon><ChatDotRound /></el-icon></span><div><strong>{{ row.group_name || '未命名客户群' }}</strong><small>{{ row.group_id }}</small></div></div></template></el-table-column>
            <el-table-column label="运行模式" width="125"><template #default="{ row }"><el-tag :type="modeTypes[row.mode as keyof typeof modes]" effect="light" round>{{ modes[row.mode as keyof typeof modes] }}</el-tag></template></el-table-column>
            <el-table-column label="触发条件" min-width="150"><template #default="{ row }"><span>{{ row.require_customer_mention ? '客户 @ 时处理' : '客户发言即处理' }}</span><small class="cell-secondary">{{ row.allow_images ? '图片' : '不接收图片' }} · {{ row.allow_files ? '文件' : '不接收文件' }}</small></template></el-table-column>
            <el-table-column label="团队协作" min-width="145"><template #default="{ row }"><span>{{ row.owner_id ? '已设置负责人' : '未设置负责人' }}</span><small class="cell-secondary">{{ new Set([...row.customer_service_ids, ...(row.owner_id ? [row.owner_id] : [])]).size }} 位客服</small></template></el-table-column>
            <el-table-column label="操作" width="144" fixed="right"><template #default="{ row }"><el-button text :icon="Edit" aria-label="编辑客户群" title="编辑" :disabled="saving" @click="editGroup(row)" /><el-button text :icon="Delete" class="delete-button" aria-label="移除客户群" title="移除" :disabled="saving" @click="removeGroup(row)" /></template></el-table-column>
          </el-table>
          <div v-if="snapshot && !groups.length" class="empty-action"><p>添加第一个客户群，设置机器人如何参与服务。</p><el-button :icon="Plus" @click="editGroup()">添加客户群</el-button></div>
          <div class="panel-foot"><span><el-icon><Check /></el-icon> 保存后自动加载群策略，无需重启服务</span><span>配置版本 {{ snapshot?.revision.slice(0, 8) || '—' }}</span></div>
        </section>
        <div class="guide-grid"><div><span class="guide-number">01</span><h3>正式运行</h3><p>按群策略接收需求，并向客户发送回复。</p></div><div><span class="guide-number">02</span><h3>影子观察</h3><p>保留处理与观察流程，不向该群发送机器人回复。</p></div><div><span class="guide-number">03</span><h3>已停用</h3><p>暂停该群的机器人消息处理，保留群配置。</p></div></div>
      </div>
      <footer v-show="page === 'groups'" class="savebar"><span :class="{ pending: dirty }"><i></i>{{ dirty ? `${changes.length} 项待保存修改` : lastSaved ? `已于 ${lastSaved} 保存 · 下次读取群策略时生效` : '配置已加载' }}</span><div><el-button :disabled="!dirty || saving" @click="refresh">放弃修改</el-button><el-button type="primary" :disabled="!dirty || saving || conflict" :loading="saving" @click="review = true">查看并保存</el-button></div></footer>
    </main>
  </div>

  <el-dialog v-model="dialog" :title="editingId ? '编辑客户群' : '添加客户群'" width="650px" class="edit-dialog" :close-on-click-modal="false" destroy-on-close @closed="formRef?.clearValidate()">
    <p class="dialog-intro">设置群身份、运行方式与客服协作。提交后加入待保存修改。</p>
    <el-form ref="formRef" :model="form" :rules="rules" label-position="top">
      <div class="form-grid"><el-form-item label="群名称" prop="group_name"><el-input v-model="form.group_name" placeholder="例如：品牌客户对接群" maxlength="120" /></el-form-item><el-form-item label="群 ID" prop="group_id"><el-input v-model="form.group_id" placeholder="数字@chatroom" :disabled="editingId !== null" /><span class="field-help">从微信回调日志的 chat 字段获取。</span></el-form-item></div>
      <el-form-item label="运行模式"><el-radio-group v-model="form.mode"><el-radio-button value="disabled">停用</el-radio-button><el-radio-button value="shadow">影子观察</el-radio-button><el-radio-button value="active">正式运行</el-radio-button></el-radio-group><span class="field-help">新群默认停用，可先用影子模式观察，再开启正式回复。</span></el-form-item>
      <div class="form-divider">团队协作</div>
      <el-form-item label="负责人 ID" prop="owner_id"><el-input v-model="form.owner_id" placeholder="wxid_… 或企业微信成员 ID@openim" /><span class="field-help">负责人自动视为客服。私聊提醒是否可达，需在实际微信环境验证。</span></el-form-item>
      <div class="form-grid"><el-form-item label="其他客服 ID"><el-input v-model="staffText" type="textarea" :rows="3" placeholder="每行一个客服 ID" /><span class="field-help">用于识别人工客服，避免机器人抢答。</span></el-form-item><el-form-item label="机器人昵称"><el-input v-model="aliasText" type="textarea" :rows="3" placeholder="每行一个昵称，例如：瀚思商品助理" /><span class="field-help">用于识别消息中的 @机器人。</span></el-form-item></div>
      <div class="form-divider">消息处理</div>
      <div class="switch-field"><div><strong>客户必须 @机器人</strong><small>关闭后，客户普通发言也会进入处理流程。</small></div><el-switch v-model="form.require_customer_mention" aria-label="客户必须 @机器人" /></div>
      <div class="switch-field"><div><strong>客服 @机器人时回复</strong><small>客服未 @ 时，机器人保持静默。</small></div><el-switch v-model="form.reply_to_staff_when_mentioned" aria-label="客服 @机器人时回复" /></div>
      <div class="switch-field"><div><strong>引用原问题回复</strong><small>安抚和澄清时引用原文本；引用信息不足时，在回复中说明对应事项。</small></div><el-switch v-model="form.native_quote_replies" aria-label="引用原问题回复" /></div>
      <div class="switch-field"><div><strong>同一事项只回复一次</strong><small>关联说明、补充问题和附件，保留不同订单与独立需求。</small></div><el-switch v-model="form.associate_customer_demands" aria-label="同一事项只回复一次" /></div>
      <div class="media-checks"><el-checkbox v-model="form.allow_images">允许接收图片</el-checkbox><el-checkbox v-model="form.allow_files">允许接收文件</el-checkbox></div>
    </el-form>
    <template #footer><el-button @click="dialog = false">取消</el-button><el-button type="primary" @click="acceptGroup">加入待保存修改</el-button></template>
  </el-dialog>
  <el-dialog v-model="review" title="确认群策略修改" width="640px" :close-on-click-modal="false" :close-on-press-escape="!saving" :show-close="!saving">
    <el-alert title="保存后，下次读取群策略时生效，无需重启。" type="info" :closable="false" show-icon />
    <div class="change-list"><div v-for="(change, index) in changes" :key="index" class="change-item"><el-tag size="small" :type="change.kind === '移除' ? 'danger' : 'info'">{{ change.kind }}</el-tag><div><strong>{{ change.label }}</strong><p>{{ change.detail }}</p></div></div></div>
    <p class="muted">保存前会备份原配置。若其他操作已修改文件，本次保存将被拒绝。</p>
    <template #footer><el-button :disabled="saving" @click="review = false">返回编辑</el-button><el-button type="primary" :loading="saving" @click="save">确认保存 {{ changes.length }} 项修改</el-button></template>
  </el-dialog>
</template>
