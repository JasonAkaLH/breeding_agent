export interface KnowledgeMetadata {
  title: string;
  authors: string[];
  year: number | null;
  doi: string | null;
  keywords: string[];
  summary: string;
}

export interface KnowledgeLibrary {
  library_id: string;
  owner_username: string;
  name: string;
  description: string;
  role: 'owner' | 'reader';
  status: string;
  revision: number;
  index_revision: number;
  published_index_revision?: number;
  publication_state?: string;
  cleanup_pending?: boolean;
  error_code?: string | null;
  index_status?: string;
  cleanup_status?: string;
  error?: string | null;
}

export interface KnowledgeDocument extends KnowledgeMetadata {
  document_id: string;
  library_id: string;
  filename: string;
  sha256: string;
  source_format?: 'markdown' | 'pdf';
  text_ready?: boolean;
  status: string;
  revision: number;
  cleanup_status?: string;
  publication_state?: string;
  cleanup_pending?: boolean;
  error_code?: string | null;
  error?: string | null;
}

export interface KnowledgeImport extends KnowledgeMetadata {
  import_id: string;
  library_id: string;
  filename: string;
  sha256: string;
  source_format?: 'markdown' | 'pdf';
  text_ready?: boolean;
  status: string;
  revision: number;
  replacement_document_id?: string | null;
  publication_state?: string;
  cleanup_pending?: boolean;
  error_code?: string | null;
  error?: string | null;
}

export interface KnowledgeGrant {
  grant_id: string;
  library_id: string;
  subject_type: 'user';
  subject_id: string;
  role: 'reader';
}

export interface KnowledgeSelection {
  mode: 'auto' | 'selected';
  library_ids: string[];
  revision?: number;
  unavailable_library_ids?: string[];
}

export interface KnowledgeBatchResult {
  results: Array<{ import_id?: string; document_id?: string; filename?: string; status: string; error?: string | null; duplicate_type?: string; unchanged?: boolean }>;
}
