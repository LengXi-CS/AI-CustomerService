<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ChatDotRound, Refresh, Search, ArrowDown } from '@element-plus/icons-vue'
import { api, ApiError, type ArchivedMessage, type ChatKind, type ChatMessagesPage, type ChatRole, type ChatRoom, type ChatRoomsPage } from './api'
import ChatBubble from './ChatBubble.vue'

const props = defineProps<{ csrf: string; active: boolean }>()
const emit = defineEmits<{ unauthorized: [] }>()
const roomQuery = ref('')
const rooms = ref<ChatRoom[]>([])
const roomSummary = ref<ChatRoomsPage | null>(null)
const roomCursor = ref<number | null>(null)
const roomSnapshot = ref<number | null>(null)
const roomLoading = ref(false)
const roomError = ref('')
const selected = ref<ChatRoom | null>(null)
const messages = ref<ArchivedMessage[]>([])
const initialLoading = ref(false)
const olderLoading = ref(false)
const newerLoading = ref(false)
const messageError = ref('')
const matchingCount = ref(0)
const olderCursor = ref<number | null>(null)
const snapshotId = ref<number | null>(null)
const newerCursor = ref<number | null>(null)
const hasOlder = ref(false)
const hasNewer = ref(false)
const newCount = ref(0)
const automatic = ref(false)
const scroller = ref<HTMLElement>()
const details = ref<ArchivedMessage | null>(null)
const roles: Record<ChatRole, string> = {customer:'客户',staff:'客服',assistant:'机器人',unknown:'未知身份'}
const kinds: Record<ChatKind, string> = {text:'文字',image:'图片',file:'文件 / 卡片',voice:'语音',video:'视频',other:'其他'}
const draft = reactive({ query:'', role:'', kind:'', dates:[] as string[] })
const applied = ref({query:'',role:null as string | null,kind:null as string | null,from_date:null as string | null,to_date:null as string | null})
const loadingMessages = computed(() => initialLoading.value || olderLoading.value || newerLoading.value)
const filtered = computed(() => Object.values(applied.value).some(Boolean))
let roomGeneration = 0
let messageGeneration = 0
let roomController: AbortController | undefined
const messageControllers = new Set<AbortController>()
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

function errorText(exc: unknown) {
  if (exc instanceof ApiError && exc.status === 401) emit('unauthorized')
  return exc instanceof ApiError && exc.status === 404 ? '服务尚未加载聊天记录接口，请重启服务后刷新。' : exc instanceof Error ? exc.message : '读取失败，请重试'
}
function headers() { return {'X-CSRF-Token': props.csrf} }
function date(value: string | null, short = false) {
  if (!value) return '时间未记录'
  return new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false,...(short ? {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'} : {})})
}
function day(value: string | null) {
  return value ? new Date(value).toLocaleDateString('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}) : '时间未记录'
}
function clearTimer() { if (timer !== undefined) clearTimeout(timer); timer = undefined }
function schedule() {
  clearTimer()
  if (!disposed && props.active && automatic.value && !document.hidden)
    timer = setTimeout(async () => { if (!loadingMessages.value) { if (selected.value) await loadNewer(); else await loadRooms() }; schedule() }, 15000)
}
function cancelMessages() {
  messageGeneration++; for (const controller of messageControllers) controller.abort(); messageControllers.clear()
  initialLoading.value = false; olderLoading.value = false; newerLoading.value = false
}
function pause() { clearTimer(); roomGeneration++; roomController?.abort(); roomLoading.value = false; cancelMessages() }
async function loadRooms(more = false) {
  if (!props.active || disposed) return
  if (more && roomLoading.value) return
  roomController?.abort(); roomController = new AbortController()
  const current = ++roomGeneration
  roomLoading.value = true; roomError.value = ''
  try {
    const result = await api<ChatRoomsPage>('/chats/rooms', {method:'POST',headers:headers(),signal:roomController.signal,
      body:JSON.stringify({query:roomQuery.value, ...(more ? {before_id:roomCursor.value,snapshot_id:roomSnapshot.value} : {})})})
    if (current !== roomGeneration) return
    rooms.value = more ? [...rooms.value,...result.items] : result.items
    roomSummary.value = result; roomCursor.value = result.next_cursor; roomSnapshot.value = result.snapshot_id
    if (!selected.value && result.items.length) await selectRoom(result.items[0]!)
    else if (selected.value) selected.value = rooms.value.find(room => room.chat_id === selected.value?.chat_id) ?? selected.value
  } catch (exc) { if (current === roomGeneration) roomError.value = errorText(exc) }
  finally { if (current === roomGeneration) roomLoading.value = false }
}
function bottom() { const element = scroller.value; if (element) element.scrollTop = element.scrollHeight; newCount.value = 0 }
function nearBottom() { const element = scroller.value; return !element || element.scrollHeight - element.scrollTop - element.clientHeight < 80 }
function merged(items: ArchivedMessage[]) { return [...new Map([...messages.value,...items].map(item => [item.id,item])).values()].sort((a,b)=>a.id-b.id) }
async function requestMessages(extra: Record<string,unknown>, current: number) {
  const controller = new AbortController(); messageControllers.add(controller)
  try {
    const result = await api<ChatMessagesPage>('/chats/messages', {method:'POST',headers:headers(),signal:controller.signal,
      body:JSON.stringify({chat_id:selected.value?.chat_id,...applied.value,...extra})})
    return current === messageGeneration ? result : null
  } finally { messageControllers.delete(controller) }
}
async function selectRoom(room: ChatRoom) { selected.value = room; details.value = null; await loadInitial() }
async function loadInitial() {
  if (!selected.value || !props.active || disposed) return
  cancelMessages(); const current = messageGeneration
  messages.value = []; matchingCount.value = 0; newCount.value = 0; hasOlder.value = false; hasNewer.value = false
  olderCursor.value = null; newerCursor.value = null; snapshotId.value = null
  initialLoading.value = true; messageError.value = ''
  try {
    const result = await requestMessages({},current)
    if (!result) return
    messages.value = result.items; matchingCount.value = result.total_matching
    olderCursor.value = result.older_cursor; hasOlder.value = result.has_older
    snapshotId.value = result.snapshot_id; newerCursor.value = result.newer_cursor
    await nextTick(); if (current === messageGeneration) bottom()
  } catch (exc) { if (current === messageGeneration) messageError.value = errorText(exc) }
  finally { if (current === messageGeneration) initialLoading.value = false }
}
async function loadOlder() {
  if (loadingMessages.value || !hasOlder.value || olderCursor.value === null) return
  const current = messageGeneration, oldTop = scroller.value?.scrollTop ?? 0, oldHeight = scroller.value?.scrollHeight ?? 0
  olderLoading.value = true; messageError.value = ''
  try {
    const result = await requestMessages({before_id:olderCursor.value,snapshot_id:snapshotId.value}, current)
    if (!result) return
    messages.value = merged(result.items); olderCursor.value = result.older_cursor; hasOlder.value = result.has_older
    await nextTick(); if (current === messageGeneration && scroller.value) scroller.value.scrollTop = oldTop + scroller.value.scrollHeight - oldHeight
  } catch (exc) { if (current === messageGeneration) messageError.value = errorText(exc) }
  finally { if (current === messageGeneration) olderLoading.value = false }
}
async function loadNewer() {
  if (!selected.value || !props.active || disposed || loadingMessages.value) return
  if (newerCursor.value === null) { await loadInitial(); return }
  const current = messageGeneration, follow = nearBottom(), oldCount = messages.value.length
  newerLoading.value = true; messageError.value = ''
  try {
    const result = await requestMessages({after_id:newerCursor.value},current)
    if (!result) return
    messages.value = merged(result.items); newerCursor.value = result.newer_cursor; hasNewer.value = result.has_newer
    matchingCount.value = result.total_matching
    await nextTick()
    if (current !== messageGeneration) return
    if (follow) bottom(); else newCount.value += messages.value.length - oldCount
  } catch (exc) { if (current === messageGeneration) messageError.value = errorText(exc) }
  finally { if (current === messageGeneration) newerLoading.value = false }
}
async function applyFilters() {
  applied.value = {query:draft.query,role:draft.role || null,kind:draft.kind || null,from_date:draft.dates?.[0] || null,to_date:draft.dates?.[1] || null}
  await loadInitial()
}
async function resetFilters() { Object.assign(draft,{query:'',role:'',kind:'',dates:[]}); await applyFilters() }
async function resume() { await loadRooms(); if (selected.value) await loadNewer(); schedule() }
function visibility() { if (document.hidden) pause(); else if (props.active) void resume() }
watch(() => props.active, active => { if (active) void resume(); else pause() })
watch(automatic,schedule)
onMounted(() => { document.addEventListener('visibilitychange',visibility); if (props.active) void resume() })
onBeforeUnmount(() => { disposed = true; pause(); document.removeEventListener('visibilitychange',visibility) })
</script>

<template>
  <div class="content chat-archive-content">
    <div class="page-heading"><div><span class="eyebrow">CONVERSATION ARCHIVE</span><h1>聊天记录</h1><p>还原已留存的群聊，查看客户需求、客服协作与机器人回复。</p></div><el-tag type="info">只读浏览</el-tag></div>
    <div class="chat-archive-summary"><span>已归档 <strong>{{ roomSummary?.total_rooms ?? '—' }}</strong> 个群 · <strong>{{ roomSummary?.total_messages ?? '—' }}</strong> 条消息</span><span>{{ roomSummary ? roomSummary.recording_enabled ? '归档设置已开启' : '归档设置已关闭，仍可查看历史' : '正在读取归档状态' }}</span></div>
    <el-alert v-if="roomError" :title="roomError" type="error" show-icon :closable="false" class="error-alert"><template v-if="roomSummary">以下保留上次读取结果，请刷新重试。</template></el-alert>
    <div class="chat-shell">
      <aside class="chat-room-panel">
        <div class="chat-room-heading"><h2>归档群聊</h2><el-button text :icon="Refresh" aria-label="刷新归档群列表" :loading="roomLoading" @click="loadRooms()" /></div>
        <form class="chat-room-search" @submit.prevent="loadRooms()"><el-input v-model="roomQuery" placeholder="搜索群名称或群 ID" aria-label="搜索归档群" :prefix-icon="Search" maxlength="128" clearable /><el-button :icon="Search" native-type="submit" aria-label="搜索群列表" /></form>
        <div class="chat-room-list" v-loading="roomLoading"><button v-for="room in rooms" :key="room.chat_id" class="chat-room" :class="{selected:selected?.chat_id === room.chat_id}" :aria-label="`打开${room.chat_name || room.chat_id}的聊天记录`" @click="selectRoom(room)"><span class="chat-room-icon"><el-icon><ChatDotRound /></el-icon></span><div><div class="chat-room-title"><strong :title="room.chat_name || room.chat_id">{{ room.chat_name || '未命名群' }}</strong><small>{{ room.message_count }} 条</small></div><small class="chat-room-id">{{ room.chat_id }}</small><p>{{ room.preview }}</p><time>{{ date(room.last_at,true) }}</time></div></button><div v-if="!rooms.length && !roomLoading" class="chat-list-empty">{{ roomError ? '归档暂不可读取' : roomQuery ? '没有匹配的归档群' : '暂未留存群聊记录' }}</div><el-button v-if="roomCursor" text :loading="roomLoading" @click="loadRooms(true)">加载更多群聊</el-button></div>
      </aside>
      <section class="chat-conversation">
        <template v-if="selected">
          <div class="chat-conversation-heading"><div><h2>{{ selected.chat_name || '未命名群' }}</h2><small>{{ selected.chat_id }} · {{ filtered ? '筛选匹配' : '已留存' }} {{ matchingCount }} 条</small></div><div><span>每 15 秒刷新</span><el-switch v-model="automatic" aria-label="自动刷新聊天记录" /><el-button :icon="Refresh" :loading="newerLoading" :disabled="initialLoading || olderLoading" @click="loadNewer">{{ hasNewer ? '继续加载新记录' : '刷新聊天' }}</el-button></div></div>
          <form class="chat-filter-bar" @submit.prevent="applyFilters"><el-input v-model="draft.query" placeholder="搜索消息、引用或文件名" aria-label="搜索聊天内容" :prefix-icon="Search" maxlength="200" clearable /><el-select v-model="draft.role" placeholder="全部身份" aria-label="筛选发送者身份"><el-option label="全部身份" value="" /><el-option v-for="role in ['customer','staff','assistant'] as const" :key="role" :label="roles[role]" :value="role" /></el-select><el-select v-model="draft.kind" placeholder="全部类型" aria-label="筛选消息类型"><el-option label="全部类型" value="" /><el-option v-for="(label,kind) in kinds" :key="kind" :label="label" :value="kind" /></el-select><el-date-picker v-model="draft.dates" type="daterange" value-format="YYYY-MM-DD" start-placeholder="开始日期" end-placeholder="结束日期" range-separator="至" aria-label="筛选聊天日期" /><div><el-button type="primary" native-type="submit" :disabled="loadingMessages">筛选记录</el-button><el-button :disabled="loadingMessages" @click="resetFilters">重置</el-button></div></form>
          <el-alert v-if="messageError" :title="messageError" type="error" show-icon :closable="false" class="chat-read-error"><template v-if="messages.length">已显示记录仍保留，最新状态尚未确认。</template></el-alert>
          <div ref="scroller" class="chat-timeline" v-loading="initialLoading" @scroll="nearBottom() && (newCount = 0)">
            <div class="chat-older"><el-button v-if="hasOlder" text :loading="olderLoading" :disabled="initialLoading || newerLoading" @click="loadOlder">加载更早记录</el-button><span v-else-if="messages.length">已到达{{ filtered ? '筛选范围内的' : '' }}最早记录</span></div>
            <template v-for="(message,index) in messages" :key="message.id"><div v-if="index === 0 || day(message.occurred_at) !== day(messages[index-1]?.occurred_at ?? null)" class="chat-day">{{ day(message.occurred_at) }}</div><ChatBubble :message="message" @details="details = $event" /></template>
            <div v-if="!messages.length && !initialLoading" class="chat-timeline-empty"><el-icon><ChatDotRound /></el-icon><p>{{ messageError ? '聊天记录暂不可读取' : filtered ? '没有匹配的聊天记录' : '该群暂无可显示的记录' }}</p></div>
          </div>
          <div class="chat-view-footer"><span>按消息接收顺序展示 · 北京时间</span><el-button v-if="newCount" size="small" :icon="ArrowDown" @click="bottom">{{ newCount }} 条新记录，查看最新</el-button><span v-else>已加载 {{ messages.length }} 条</span></div>
        </template>
        <div v-else class="chat-timeline-empty"><el-icon><ChatDotRound /></el-icon><h2>选择一个归档群</h2><p>左侧展示已留存的群聊，包含历史上已移除的群。</p></div>
      </section>
    </div>
    <p class="chat-archive-note">展示实际留存内容，不标记送达或已读状态。已缺失的图片和文件保留消息占位，不自动重新下载。</p>
    <el-drawer :model-value="details !== null" title="消息详情" size="min(420px,100vw)" @close="details = null"><template v-if="details"><dl class="chat-message-details"><dt>发送者</dt><dd>{{ details.sender_name || details.sender_id || '未记录' }}</dd><dt>发送者 ID</dt><dd>{{ details.sender_id || '未记录' }}</dd><dt>消息身份</dt><dd>{{ roles[details.sender_role] }}</dd><dt>消息类型</dt><dd>{{ kinds[details.kind] }}</dd><dt>消息发生时间</dt><dd>{{ date(details.occurred_at) }}</dd><dt>记录时间</dt><dd>{{ date(details.recorded_at) }}</dd><dt>平台消息 ID</dt><dd>{{ details.message_id || '未记录' }}</dd><dt>引用消息 ID</dt><dd>{{ details.quoted_msg_id || '未记录' }}</dd><dt>回复来源</dt><dd>{{ details.channel_used || '未记录' }}</dd><dt>关联任务 ID</dt><dd>{{ details.task_id || '未记录' }}</dd><dt>归档序号</dt><dd>{{ details.id }}</dd></dl></template></el-drawer>
  </div>
</template>
