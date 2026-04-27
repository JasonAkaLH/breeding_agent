export type ChatMode = 'chat' | 'sql_query';

export interface SubmitMessageRequest {
  account_id: string;
  content: string;
  routing_mode: 'auto' | string;
  capability_id: string | null;
  client_message_id?: string | null;
  metadata: Record<string, unknown>;
}

export interface MessageAcceptedResponse {
  conversation_id: string;
  message_id: string;
  task_id: string;
  status: 'accepted' | string;
}

export interface TaskSummaryResponse {
  task_id: string;
  conversation_id: string;
  status: string;
  root_node_id: string | null;
  active_node_count: number;
  completed_node_count: number;
  failed_node_count: number;
  cancel_requested: boolean;
  created_at: string | null;
  updated_at: string | null;
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
}

export interface TaskArtifactsResponse {
  task_id: string;
  artifacts: ArtifactResponse[];
}

export interface CancelTaskResponse {
  task_id: string;
  status: string;
  accepted: boolean;
}

export interface CapabilityResponse {
  capability_id: string;
  name: string;
  description: string;
  version: string;
  status: string;
}

export interface CapabilityListResponse {
  capabilities: CapabilityResponse[];
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
