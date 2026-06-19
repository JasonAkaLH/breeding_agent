export type ChatMode = 'chat';
export type ReasoningEffort = 'minimal' | 'high' | 'max';
export type ModelEdition = string;

export interface UserResponse {
  username: string;
}

export interface AuthUserResponse {
  user: UserResponse;
}

export interface AuthTokenResponse extends AuthUserResponse {
  access_token: string;
}

export interface LogoutResponse {
  logged_out: boolean;
}

export interface ModelEditionOption {
  value: ModelEdition;
  label: string;
}

export interface ModelEditionsResponse {
  default_model_edition: ModelEdition | null;
  options: ModelEditionOption[];
}

export interface SubmitMessageRequest {
  conversation_id: string;
  content: string;
  routing_mode: 'auto' | string;
  capability_id: string | null;
  client_message_id?: string | null;
  model_edition?: ModelEdition | null;
  metadata: Record<string, unknown>;
}

export interface CapabilityResponse {
  capability_id: string;
  name: string;
  display_name?: string;
  description: string;
  version: string;
  status: string;
  kind: string;
  source: string;
  source_path: string;
}

export interface CapabilityListResponse {
  capabilities: CapabilityResponse[];
}

export interface MessageAcceptedResponse {
  conversation_id: string;
  message_id: string;
  task_id: string;
  status: 'accepted' | string;
  action?: 'task_accepted' | 'interrupt_resumed' | 'interrupt_clarification_answer' | 'interrupt_mixed_processed' | 'interrupt_schema_switched' | string | null;
  interrupt_id?: string | null;
  assistant_message?: string | null;
  answer_payload?: Record<string, unknown> | null;
}

export interface UploadPreviewResponse {
  row_count?: number | null;
  columns: string[];
  shape?: string | null;
  source_encoding?: string | null;
  original_columns?: string[];
  column_normalizations?: Array<Record<string, unknown>>;
  column_count?: number | null;
  columns_truncated?: boolean | null;
  column_normalization_count?: number | null;
  column_normalizations_truncated?: boolean | null;
  normalized_content_type?: string | null;
  char_count?: number | null;
  line_count?: number | null;
  size_bytes?: number | null;
  file_type?: string | null;
  requires_sheet_selection?: boolean | null;
  selected_sheet?: string | null;
  excel_sheets?: Array<Record<string, unknown>>;
  excel_sheet_count?: number | null;
  excel_sheets_truncated?: boolean | null;
}

export interface UploadFileResponse {
  upload_id: string;
  conversation_id: string;
  filename: string;
  content_type: string;
  file_type: 'json' | 'csv' | 'spreadsheet' | 'text' | 'image' | 'pdf' | 'vcf' | string;
  size_bytes: number;
  sha256: string;
  expires_at: string;
  preview: UploadPreviewResponse;
}

export interface UploadListResponse {
  conversation_id: string;
  uploads: UploadFileResponse[];
}

export interface DeleteUploadResponse {
  upload_id: string;
  deleted: boolean;
}

export interface TaskSummaryResponse {
  task_id: string;
  conversation_id: string;
  status: string;
  root_node_id: string | null;
  summary: string | null;
  requested_capability_id: string | null;
  active_node_count: number;
  completed_node_count: number;
  failed_node_count: number;
  cancel_requested: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface TaskListResponse {
  conversation_id: string;
  tasks: TaskSummaryResponse[];
}

export interface ConversationSummaryResponse {
  conversation_id: string;
  username: string;
  status: string;
  current_task_id: string | null;
  title: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ConversationListResponse {
  conversations: ConversationSummaryResponse[];
}

export interface RenameConversationRequest {
  conversation_id: string;
  title: string;
}

export interface MessageResponse {
  message_id: string;
  conversation_id: string;
  role: 'user' | 'assistant' | 'system' | string;
  content: string;
  task_id: string | null;
  stream_status: string | null;
  created_at: string | null;
  message_type?: string;
  metadata?: Record<string, unknown>;
  updated_at?: string | null;
  artifacts?: ArtifactResponse[];
}

export interface ConversationMessagesResponse {
  conversation_id: string;
  messages: MessageResponse[];
}

export interface DeleteConversationResponse {
  conversation_id: string;
  deleted: boolean;
  cancelled_task_ids: string[];
  deleted_counts: Record<string, number>;
  delete_status?: 'completed' | 'failed' | string;
  runner_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error_code?: string | null;
}

export interface TaskNodeResponse {
  node_id: string;
  capability_id: string;
  status: string;
  criticality: string;
  dependency_type: string;
  assigned_instance_id: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskEdgeResponse {
  from_node_id: string;
  to_node_id: string;
  edge_type: string;
  condition: string | null;
}

export interface TaskGraphResponse {
  task_id: string;
  nodes: TaskNodeResponse[];
  edges: TaskEdgeResponse[];
}

export interface ArtifactResponse {
  artifact_id: string;
  producer_node_id: string;
  artifact_type: string;
  storage_ref: string;
  summary: string | null;
  is_complete: boolean;
  created_at: string | null;
  filename?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  download_url?: string | null;
  source_file_count?: number | null;
  archive_format?: string | null;
  retention_status?: string | null;
}

export interface TaskArtifactsResponse {
  task_id: string;
  artifacts: ArtifactResponse[];
}

export interface InterruptResponse {
  interrupt_id: string;
  conversation_id: string;
  task_id: string;
  node_id: string;
  question: string;
  reason_code: string;
  required_fields: Record<string, unknown>;
  status: string;
}

export interface TaskInterruptsResponse {
  task_id: string;
  interrupts: InterruptResponse[];
}

export interface CancelTaskResponse {
  task_id: string;
  status: string;
  accepted: boolean;
}

export interface TaskEventEnvelope {
  event_id: string;
  conversation_id: string;
  task_id: string;
  node_id: string | null;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string | null;
}
