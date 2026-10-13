export interface GroupPolicy {
  group_id: string
  group_name: string
  mode: 'disabled' | 'shadow' | 'active'
  require_customer_mention: boolean
  allow_images: boolean
  allow_files: boolean
  owner_id: string
  customer_service_ids: string[]
  bot_aliases: string[]
  reply_to_staff_when_mentioned: boolean
  native_quote_replies: boolean
  associate_customer_demands: boolean
}

export interface GroupSnapshot {
  revision: string
  global_enabled: boolean
  groups: GroupPolicy[]
  apply_mode: string
}

export interface SootheValues {
  soothe_mode: 'model' | 'fixed'
  soothe_fixed_reply: string
}

export interface SootheSnapshot {
  revision: string
  values: SootheValues
  apply_mode: 'next_policy_read'
}

export interface RuntimeReplyValues {
  conversation_quiet_window_seconds: number
  conversation_poll_interval_seconds: number
  attachment_dispatch_interval_seconds: number
  routing_normal_confidence_threshold: number
  routing_human_confidence_threshold: number
  owner_notify_enabled: boolean
}

export interface RuntimeReplySnapshot {
  history_pending?: boolean
  revision: string
  values: RuntimeReplyValues
  active_values: RuntimeReplyValues
  fields: Record<keyof RuntimeReplyValues, {
    source: 'environment' | 'dotenv' | 'default'
    source_key: string | null
    editable: boolean
    status: 'environment_override' | 'pending_restart' | 'effective'
    pending_restart: boolean
  }>
  apply_mode: 'restart'
  restart_required: boolean
}

export interface ConnectionValues {
  llm_base_url: string
  llm_model: string
  llm_vision_model: string
  fastgpt_api_url: string
  fastgpt_dataset_id: string
  gewe_api_url: string
  gewe_app_id: string
  lark_app_id: string
  lark_bitable_app_token: string
}
export type ConnectionSecret = 'llm_api_key' | 'fastgpt_api_key' | 'gewe_token' | 'lark_app_secret'
export type ServiceName = 'llm' | 'fastgpt' | 'gewe' | 'lark'
export interface SecretEdit { action: 'keep' | 'set' | 'clear'; value: string }
export interface ConnectionSource {
  source: 'environment' | 'dotenv' | 'default'
  source_key: string | null
  editable: boolean
  pending_restart: boolean
}
export interface ConnectionSnapshot {
  history_pending?: boolean
  revision: string
  values: ConnectionValues
  active_values: ConnectionValues
  fields: Record<keyof ConnectionValues, ConnectionSource>
  secrets: Record<ConnectionSecret, ConnectionSource & { configured: boolean; active_configured: boolean }>
  restart_required: boolean
  fastgpt_compatibility_key_configured: boolean
}
export interface ProbeResult { service: ServiceName; level: 'passed' | 'partial' | 'failed'; message: string }

export type TaskState = 'waiting_quiet' | 'processing' | 'human_active' | 'responded' | 'closed' | 'blocked' | 'other'
export interface RuntimeStatusSnapshot {
  checked_at: string
  service: {
    status: 'starting' | 'healthy' | 'degraded' | 'stopped'
    started_at: string | null
    uptime_seconds: number | null
    poller: 'starting' | 'running' | 'stopped'
    product_database_available: boolean
  }
  tasks: { available: boolean; counts: Record<TaskState, number> | null; total: number | null; last_updated: string | null }
  configuration: {
    available: boolean; count: number | null
    scopes: { page: 'reply' | 'connections'; available: boolean; count: number | null }[]
  }
  errors: {
    total: number; limit: number; since: string | null
    items: { id: number; occurred_at: string; level: 'error' | 'critical'; category: string; summary: string }[]
  }
}

export type BackupTarget = 'groups' | 'environment'
export interface HistoryEntry {
  id: string; created_at: string; target: BackupTarget
  scope: 'groups' | 'soothe' | 'reply' | 'connections' | 'environment'
  operation: 'save' | 'restore'; status: 'applied' | 'failed' | 'prepared'
  fields: { key: string; label: string }[]
  backup_id: string | null; source_backup_id: string | null
}
export interface PageResult<T> { items: T[]; next_cursor: string | null }
export interface BackupEntry { backup_id: string; created_at: string; size_bytes: number }
export interface RestorePreview {
  target: BackupTarget; backup_id: string; created_at: string
  current_revision: string; backup_revision: string; plan_revision: string
  can_restore: boolean; apply_mode: 'restart' | 'next_policy_read'
  changes: { field: string; label: string; before: string; after: string; secret: boolean; blocked: boolean }[]
}
export interface RestoreResult {
  revision: string; changed: boolean; backup_id: string | null; history_pending: boolean
  apply_mode: 'restart' | 'next_policy_read'
}

export type ChatRole = 'customer' | 'staff' | 'assistant' | 'unknown'
export type ChatKind = 'text' | 'image' | 'file' | 'voice' | 'video' | 'other'
export interface ChatRoom {
  chat_id: string; chat_name: string; message_count: number; last_id: number
  last_at: string | null; last_kind: ChatKind; preview: string; last_role: ChatRole
}
export interface ChatRoomsPage {
  items: ChatRoom[]; snapshot_id: number; total_rooms: number; total_messages: number
  matched_rooms: number; recording_enabled: boolean; next_cursor: number | null
}
export interface ArchivedMessage {
  id: number; chat_id: string; message_id: string; direction: 'inbound' | 'outbound'
  sender_id: string; sender_name: string; sender_role: ChatRole; kind: ChatKind; text: string
  occurred_at: string | null; recorded_at: string | null
  quoted_msg_id: string; quoted_type: string; quoted_text: string
  mentioned_bot: boolean; mentions: string[]; channel_used: string; task_id: string
  media: {
    status: 'available' | 'missing' | 'unrecorded' | 'unavailable'
    available: boolean; inline_image: boolean; name: string; size_bytes: number
    url: string | null; download_url: string | null; mime_type?: string
  }
}
export interface ChatMessagesPage {
  items: ArchivedMessage[]; snapshot_id: number; total_matching: number
  older_cursor: number | null; has_older: boolean; newer_cursor: number; has_newer: boolean
}

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message) }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/admin${path}`, {
    ...options,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...options.headers },
  })
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    const message = typeof body.detail === 'string' ? body.detail
      : Array.isArray(body.detail) ? body.detail.map((error: { loc: string[]; msg: string }) =>
        `${error.loc.filter((item) => item !== 'body').join('.')}：${error.msg}`).join('；')
      : '请求失败，请稍后重试'
    throw new ApiError(response.status, message)
  }
  return body as T
}
