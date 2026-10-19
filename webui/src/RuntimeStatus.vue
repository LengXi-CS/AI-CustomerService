<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Refresh, Monitor, Timer, Warning, ArrowRight, Box } from '@element-plus/icons-vue'
import { api, ApiError, type RuntimeStatusSnapshot, type TaskState } from './api'

const props = defineProps<{ active: boolean }>()
const emit = defineEmits<{ unauthorized: []; navigate: [page: 'reply' | 'connections'] }>()
const snapshot = ref<RuntimeStatusSnapshot | null>(null)
const loading = ref(false)
const error = ref('')
const automatic = ref(true)
let timer: ReturnType<typeof setTimeout> | undefined
let controller: AbortController | undefined
let generation = 0
let disposed = false

function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
function schedule() {
  clearTimer()
  if (!disposed && props.active && automatic.value && !document.hidden)
    timer = setTimeout(() => { void refresh() }, 15000)
}
function pause() {
  clearTimer(); generation++; controller?.abort(); controller = undefined; loading.value = false
}
async function refresh() {
  if (loading.value || !props.active || disposed || document.hidden) return
  clearTimer()
  const current = ++generation
  controller = new AbortController()
  loading.value = true; error.value = ''
  try {
    const result = await api<RuntimeStatusSnapshot>('/status', { signal: controller.signal })
    if (current === generation) snapshot.value = result
  } catch (exc) {
    if (current !== generation) return
    error.value = exc instanceof ApiError && exc.status === 404
      ? '服务尚未加载运行状态接口，请在管理员 PowerShell 中重启服务后刷新。'
      : exc instanceof Error ? exc.message : '无法读取运行状态'
    if (exc instanceof ApiError && exc.status === 401) emit('unauthorized')
  } finally {
    if (current === generation) { loading.value = false; controller = undefined; schedule() }
  }
}
function visibilityChanged() { if (document.hidden) pause(); else if (props.active) void refresh() }
watch(() => props.active, (active) => { if (active) void refresh(); else pause() })
watch(automatic, schedule)
onMounted(() => { document.addEventListener('visibilitychange', visibilityChanged); if (props.active) void refresh() })
onBeforeUnmount(() => { disposed = true; pause(); document.removeEventListener('visibilitychange', visibilityChanged) })

function date(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
}
const duration = computed(() => {
  const seconds = snapshot.value?.service.uptime_seconds
  if (seconds == null) return '—'
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds / 3600) % 24, minutes = Math.floor(seconds / 60) % 60
  return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${minutes} 分钟` : minutes ? `${minutes} 分钟` : `${seconds} 秒`
})
const labels = { starting: '正在启动', healthy: '本地服务正常', degraded: '需要检查', stopped: '服务已停止' }
const statusType = computed(() => snapshot.value?.service.status === 'healthy' ? 'success' : 'warning')
const docker = computed(() => snapshot.value?.docker)
const aidnet = computed(() => snapshot.value?.aidnet)
const processLabels = { running: '运行中', stopped: '未运行', unknown: '未确认' }
const aidnetStatusType = computed(() => aidnet.value?.status === 'healthy' ? 'success' : aidnet.value?.status === 'degraded' ? 'danger' : 'warning')
const dockerEngineLabels = { running: '已连接', unavailable: '无法连接', cli_missing: '无法检测', unknown: '未确认' }
const containerLabels = { created: '未启动', running: '运行中', paused: '已暂停', restarting: '重启中', removing: '移除中', exited: '已停止', dead: '异常退出', missing: '未找到', unknown: '未确认' }
const dockerStatusType = computed(() => docker.value?.status === 'healthy' ? 'success' : docker.value?.status === 'degraded' ? 'danger' : 'warning')
const containerHealth = computed(() => {
  if (!docker.value || docker.value.frpc.status !== 'running') return '未检测'
  const health = docker.value.frpc.health
  return health === 'healthy' ? '通过' : health === 'unhealthy' ? '未通过' : health === 'starting' ? '启动中' : '未配置'
})
const scopes = { reply: '回复策略', connections: '模型与连接' }
const states: { key: TaskState; label: string; help: string }[] = [
  { key: 'waiting_quiet', label: '等待静默', help: '等待客户停止发言后处理' },
  { key: 'processing', label: '处理中', help: '任务已被处理流程领取' },
  { key: 'human_active', label: '人工接管', help: '客服正在参与处理' },
  { key: 'responded', label: '已回复', help: '处理流程已标记回复完成' },
  { key: 'closed', label: '已关闭', help: '任务已结束或取消' },
  { key: 'blocked', label: '已阻断', help: '处理受到策略或内容条件限制' },
]
</script>

<template>
  <div class="content runtime-content">
    <div class="page-heading"><div><span class="eyebrow">SERVICE STATUS</span><h1>运行状态</h1><p>查看服务、Docker、AidNet、任务与配置生效情况，掌握当前处理状态。</p></div><el-button :icon="Refresh" :loading="loading" @click="refresh">刷新状态</el-button></div>
    <div class="runtime-refresh"><span>最近成功读取：{{ date(snapshot?.checked_at) }}（北京时间）</span><div><span>每 15 秒自动刷新</span><el-switch v-model="automatic" aria-label="自动刷新运行状态" /></div></div>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="error-alert"><template v-if="snapshot">以下保留最近一次成功读取的数据，当前状态尚未确认。</template></el-alert>
    <div v-if="!snapshot" class="runtime-empty"><el-icon><Monitor /></el-icon><p>{{ loading ? '正在读取服务状态…' : '暂未获取运行状态，请刷新重试。' }}</p></div>
    <template v-else>
      <div class="policy-status-grid runtime-overview">
        <div class="policy-status"><span><el-icon><Monitor /></el-icon>服务状态</span><div class="runtime-status-label"><el-tag :type="statusType" effect="light">{{ labels[snapshot.service.status] }}</el-tag></div><p class="policy-help">任务调度器：{{ snapshot.service.poller === 'running' ? '运行中' : snapshot.service.poller === 'starting' ? '尚未启动' : '已停止' }}</p></div>
        <div class="policy-status"><span><el-icon><Timer /></el-icon>已运行</span><strong class="runtime-duration">{{ duration }}</strong><p class="policy-help">启动于 {{ date(snapshot.service.started_at) }}</p></div>
        <div class="policy-status"><span>已保存、待重启的配置</span><strong>{{ snapshot.configuration.count ?? '—' }}<small>项参数</small></strong><p class="policy-help">{{ snapshot.configuration.available ? '仅统计回复策略与连接页面开放的参数。' : '部分配置无法读取，待重启总数尚未确认。' }}</p></div>
      </div>
      <section class="policy-card runtime-docker">
        <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Box /></el-icon><div><h2>本地 Docker 与回调隧道</h2><p>检查本机 Docker 引擎和 frpc 容器，及时发现回调转发所需的运行环境异常。</p></div></div><el-tag :type="dockerStatusType">{{ docker?.status === 'healthy' ? '运行正常' : docker?.status === 'degraded' ? '需要检查' : '状态待确认' }}</el-tag></div>
        <el-alert v-if="!docker" title="服务尚未加载 Docker 状态检测，请重启客服服务后刷新本页。" type="warning" :closable="false" show-icon />
        <template v-else>
          <el-alert v-if="docker.status !== 'healthy'" :title="docker.message" :type="docker.status === 'degraded' ? 'error' : 'warning'" :closable="false" show-icon class="error-alert" />
          <div class="runtime-docker-grid">
            <article><div class="runtime-docker-title"><strong>Docker 引擎</strong><el-tag :type="docker.engine === 'running' ? 'success' : docker.engine === 'unknown' ? 'warning' : 'danger'">{{ dockerEngineLabels[docker.engine] }}</el-tag></div><p>{{ docker.engine === 'running' ? '本机 Docker Linux 引擎可连接。' : docker.engine === 'cli_missing' ? '客服服务未找到 Docker 命令，请检查安装与服务账户环境。' : docker.engine === 'unavailable' ? '请确认 Docker Desktop 已启动，且服务账户有访问权限。' : '当前连接状态尚未确认。' }}</p></article>
            <article><div class="runtime-docker-title"><strong>frpc 回调转发容器</strong><el-tag :type="docker.frpc.status === 'running' ? (docker.frpc.health === 'unhealthy' ? 'danger' : 'success') : docker.frpc.status === 'unknown' ? 'warning' : 'danger'">{{ containerLabels[docker.frpc.status] }}</el-tag></div><dl><div><dt>容器健康检查</dt><dd>{{ containerHealth }}</dd></div><div><dt>最近启动</dt><dd>{{ date(docker.frpc.started_at) }}</dd></div><div v-if="docker.frpc.status === 'exited' || docker.frpc.status === 'dead'"><dt>退出码</dt><dd>{{ docker.frpc.exit_code ?? '—' }}</dd></div></dl></article>
          </div>
          <div class="runtime-docker-footer"><span>最近检测：{{ date(docker.checked_at) }}（北京时间），检测间隔 15 秒。</span><span>容器运行状态不代表公网已连通；公网回调请在 GeWe 中检测。</span></div>
        </template>
      </section>
      <section class="policy-card runtime-aidnet">
        <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Monitor /></el-icon><div><h2>AidNet 软件</h2><p>检查 AidNet 主程序及它自带的连接进程，区分软件未启动与连接进程异常。</p></div></div><el-tag :type="aidnetStatusType">{{ aidnet?.status === 'healthy' ? '进程运行正常' : aidnet?.status === 'degraded' ? '需要检查' : '状态待确认' }}</el-tag></div>
        <el-alert v-if="!aidnet" title="服务尚未加载 AidNet 状态检测，请重启客服服务后刷新本页。" type="warning" :closable="false" show-icon />
        <template v-else>
          <el-alert v-if="aidnet.status !== 'healthy'" :title="aidnet.message" :type="aidnet.status === 'degraded' ? 'error' : 'warning'" :closable="false" show-icon class="error-alert" />
          <div class="runtime-docker-grid">
            <article><div class="runtime-docker-title"><strong>AidNet 主程序</strong><el-tag :type="aidnet.app === 'running' ? 'success' : aidnet.app === 'stopped' ? 'danger' : 'warning'">{{ processLabels[aidnet.app] }}</el-tag></div><p>查看本机 AidNet 软件是否已启动。</p></article>
            <article><div class="runtime-docker-title"><strong>AidNet 内置连接进程</strong><el-tag :type="aidnet.frpc === 'running' ? 'success' : aidnet.frpc === 'stopped' ? 'danger' : 'warning'">{{ processLabels[aidnet.frpc] }}</el-tag></div><p>仅识别属于 AidNet 的 frpc，独立于 Docker 中的客服回调转发容器。</p></article>
          </div>
          <div class="runtime-docker-footer"><span>最近检测：{{ date(aidnet.checked_at) }}（北京时间），检测间隔 15 秒。</span><span>进程运行不代表微信节点已在线；节点连接状态请在 AidNet 或 GeWe 中确认。</span></div>
        </template>
      </section>
      <section class="policy-card">
        <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Monitor /></el-icon><div><h2>服务与配置检查</h2><p>检查当前进程、调度器和本地数据库；外部服务连接可在“模型与连接”中单独测试。</p></div></div></div>
        <div class="runtime-checks"><div><span>商品数据库</span><el-tag :type="snapshot.service.product_database_available ? 'success' : 'danger'">{{ snapshot.service.product_database_available ? '可读取' : '不可读取或未初始化' }}</el-tag></div><div><span>任务状态库</span><el-tag :type="snapshot.tasks.available ? 'success' : 'danger'">{{ snapshot.tasks.available ? '可读取' : '暂不可读取' }}</el-tag></div></div>
        <div class="runtime-scopes"><button v-for="scope in snapshot.configuration.scopes" :key="scope.page" @click="emit('navigate', scope.page)"><div><strong>{{ scopes[scope.page] }}</strong><small>{{ !scope.available ? '配置无法读取，请检查文件格式或权限' : scope.count ? `${scope.count} 项参数待重启生效` : '配置与启动时运行值一致' }}</small></div><el-icon><ArrowRight /></el-icon></button></div>
        <el-alert v-if="!snapshot.configuration.available" title="请先修复无法读取的配置，确认格式和权限正常后再重启服务。" type="warning" :closable="false" show-icon />
        <div v-else-if="(snapshot.configuration.count ?? 0) > 0" class="policy-restart-help"><h3>让已保存配置生效</h3><p>在管理员 PowerShell 中执行以下命令，完成后重新登录后台。</p><code>powershell -NoProfile -ExecutionPolicy Bypass -File "D:\瀚思AI客服\scripts\restart_app.ps1"</code></div>
      </section>
      <section class="policy-card">
        <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Timer /></el-icon><div><h2>任务状态分布</h2><p>统计任务库中保留的全部任务，包含历史任务。最近任务更新：{{ date(snapshot.tasks.last_updated) }}。</p></div></div><el-tag type="info">共 {{ snapshot.tasks.total ?? '—' }} 个任务</el-tag></div>
        <el-alert v-if="!snapshot.tasks.available" title="任务库暂不可读取，数量未知。请检查文件、权限或数据库占用情况。" type="warning" :closable="false" show-icon />
        <div class="runtime-task-grid"><div v-for="state in states" :key="state.key"><span>{{ state.label }}</span><strong>{{ snapshot.tasks.counts?.[state.key] ?? '—' }}</strong><small>{{ state.help }}</small></div></div>
        <el-alert v-if="snapshot.tasks.counts?.other" :title="`${snapshot.tasks.counts.other} 个任务使用未识别状态，已计入总数。`" type="warning" :closable="false" />
        <p class="policy-help">处理异常可能退回“等待静默”后重试；“已阻断”并不等同于执行失败。刷新本页只读取统计。</p>
      </section>
      <section class="policy-card">
        <div class="policy-card-heading"><div class="policy-section-title"><el-icon><Warning /></el-icon><div><h2>最近异常</h2><p>本次服务启动后采集的 ERROR / CRITICAL 记录，最多展示最近 {{ snapshot.errors.limit }} 条；重启后重新计数。摘要仅展示分类。</p></div></div><el-tag :type="snapshot.errors.total ? 'warning' : 'info'">本次启动 {{ snapshot.errors.total }} 条</el-tag></div>
        <div v-if="!snapshot.errors.items.length" class="runtime-no-errors">本次服务启动后暂未记录异常。此处不包含启动前的历史日志。</div>
        <div v-else class="runtime-errors"><article v-for="event in snapshot.errors.items" :key="event.id"><div><el-tag :type="event.level === 'critical' ? 'danger' : 'warning'" size="small">{{ event.category }}</el-tag><time>{{ date(event.occurred_at) }}</time></div><p>{{ event.summary }}</p></article></div>
        <p class="policy-help">查看详细原因时，请在服务器本机按对应时间查阅日志。</p>
      </section>
    </template>
  </div>
</template>
