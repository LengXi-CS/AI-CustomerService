<script setup lang="ts">
import { computed, ref } from 'vue'
import { Document, Picture, Microphone, VideoCamera, Download, MoreFilled } from '@element-plus/icons-vue'
import type { ArchivedMessage } from './api'

const props = defineProps<{ message: ArchivedMessage }>()
const emit = defineEmits<{ details: [message: ArchivedMessage] }>()
const broken = ref(false)
const roles = { customer: '客户', staff: '客服', assistant: '机器人', unknown: '未知身份' }
const kinds = { text: '文字消息', image: '图片消息', file: '文件 / 卡片消息', voice: '语音消息', video: '视频消息', other: '其他消息' }
const sender = computed(() => props.message.sender_name || (props.message.sender_role === 'assistant' ? '机器人' : props.message.sender_id || roles[props.message.sender_role]))
const when = computed(() => props.message.occurred_at ? new Date(props.message.occurred_at).toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '时间未记录')
const size = computed(() => {
  const bytes = props.message.media.size_bytes
  return bytes ? bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB` : ''
})
const missing = computed(() => props.message.media.status === 'unavailable' ? '附件暂不可读取' : props.message.kind === 'image' ? '图片未留存或已清理' : '未留存可打开的原文件')
</script>

<template>
  <article class="chat-message" :class="{outgoing: message.direction === 'outbound', staff: message.sender_role === 'staff'}">
    <span class="chat-avatar" :class="message.sender_role">{{ message.sender_role === 'assistant' ? '机' : sender.slice(0,1) }}</span>
    <div class="chat-message-main">
      <div class="chat-message-meta"><strong :title="sender">{{ sender }}</strong><span class="chat-role" :class="message.sender_role">{{ roles[message.sender_role] }}</span><time>{{ when }}</time><el-button text :icon="MoreFilled" aria-label="查看消息详情" title="查看消息详情" @click="emit('details', message)" /></div>
      <div class="chat-bubble">
        <div v-if="message.quoted_text || message.quoted_msg_id" class="chat-quote"><span>引用消息</span><p>{{ message.quoted_text || '引用内容未留存' }}</p></div>
        <div v-if="message.mentioned_bot || message.mentions.length" class="chat-mentions">{{ message.mentioned_bot ? '@机器人' : '' }}<span v-if="message.mentions.length">{{ message.mentioned_bot ? ' · ' : '提及：' }}{{ message.mentions.join('、') }}</span></div>
        <p v-if="message.text" class="chat-message-text">{{ message.text }}</p>
        <template v-if="message.kind !== 'text'">
          <div v-if="message.kind === 'image' && message.media.inline_image && message.media.url && !broken" class="chat-picture"><el-image :src="message.media.url" :preview-src-list="[message.media.url]" preview-teleported fit="contain" lazy @error="broken = true" /><a v-if="message.media.download_url" :href="message.media.download_url" download>下载图片</a></div>
          <div v-else class="chat-attachment"><el-icon><Picture v-if="message.kind === 'image'" /><Microphone v-else-if="message.kind === 'voice'" /><VideoCamera v-else-if="message.kind === 'video'" /><Document v-else /></el-icon><div><strong>{{ message.media.name !== '历史附件' ? message.media.name : kinds[message.kind] }}</strong><small>{{ message.media.available && !broken ? size || '本地附件' : missing }}</small><a v-if="message.media.download_url && !broken" :href="message.media.download_url" download><el-icon><Download /></el-icon>下载附件</a></div></div>
        </template>
        <p v-else-if="!message.text" class="chat-message-placeholder">消息正文未留存</p>
      </div>
    </div>
  </article>
</template>
