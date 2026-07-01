// Core types mirroring the backend OpenAPI 3.1 schemas.
// Faithfully reflects the FastAPI pydantic models in backend/app/domains/*/models.py.
// Normally regenerated via `npm run gen:api` (openapi-typescript); hand-maintained here.
//
// Backend contract facts (all verified against source):
// - List endpoints return a BARE JSON array `[]` (response_model=list[<Out>]).
//   There is NO {items, total} wrapper; pagination is via limit/offset query only.
// - All entity IDs are uuid.UUID (serialized as canonical UUID string).
//   The only non-UUID keys: /models/{alias} (str), /prompts/{id}/diff (int versions).
// - Decimal fields (costs) serialize as strings; datetime as ISO-8601 strings.

// ---------- Common ----------

/** Generic error envelope returned by the backend (errors.spec.md§2). */
export interface ApiErrorBody {
  error: string;
  message: string;
  detail?: unknown;
}

/** Pagination query params shared by all list endpoints. */
export interface PageQuery {
  limit?: number;
  offset?: number;
}

/** A UUID string (canonical 8-4-4-4-12 form). */
export type UUID = string;

// ---------- Auth ----------

export interface UserCreate {
  email: string;
  username: string;
  full_name?: string;
  password: string;
}

export interface UserOut {
  id: UUID;
  email: string;
  username: string;
  full_name: string | null;
  is_active: boolean;
  role: "admin" | "user";
  created_at: string;
}

export interface Token {
  access_token: string;
  refresh_token: string;
  token_type: string; // "bearer"
  expires_in: number; // access token validity seconds
}

export interface RefreshRequest {
  refresh_token: string;
}

// ---------- Prompts ----------

export interface PromptCreate {
  name: string;
  description?: string;
  content: string;
  variables: string[];
}

export interface PromptUpdate {
  name?: string;
  description?: string;
  is_active?: boolean;
}

export interface PromptVersionCreate {
  content: string;
  variables: string[];
  change_note?: string;
}

export interface PromptVersionOut {
  id: UUID;
  prompt_id: UUID;
  version_num: number;
  content: string;
  variables: string[];
  change_note: string | null;
  created_by: string | null;
  created_at: string;
}

export interface PromptOut {
  id: UUID;
  name: string;
  description: string | null;
  current_version_id: UUID | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  versions: PromptVersionOut[];
}

export interface DiffResult {
  from_version: number;
  to_version: number;
  added_lines: string[];
  removed_lines: string[];
  unified_diff: string[];
}

// ---------- Agents ----------

export type ToolType =
  | "search"
  | "calculator"
  | "http"
  | "code"
  | "rag"
  | "custom";

export interface ToolDef {
  name: string;
  type: ToolType;
  description?: string;
  config: Record<string, unknown>;
}

export interface AgentCreate {
  name: string;
  description?: string;
  system_prompt?: string;
  model_alias: string;
  tools: ToolDef[];
  max_turns: number;
  temperature: number;
}

export interface AgentOut {
  id: UUID;
  name: string;
  description: string | null;
  system_prompt: string | null;
  model_alias: string;
  tools: Record<string, unknown>[];
  max_turns: number;
  temperature: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface ExecuteRequest {
  input: string;
  max_turns?: number;
  context?: Record<string, unknown>;
}

export interface ExecutionTrace {
  turn: number;
  thought: string;
  action: string | null;
  observation: string | null;
  tokens: number;
}

export interface ExecutionResult {
  agent_id: UUID | null;
  workflow_id: UUID | null;
  final_answer: string;
  traces: ExecutionTrace[];
  total_tokens: number;
  success: boolean;
  error: string | null;
}

// ---------- Workflows ----------

export interface AgentNode {
  id: string;
  agent_id: UUID | null;
  name: string;
  inputs: Record<string, unknown>;
  is_entry: boolean;
  is_exit: boolean;
}

export interface WorkflowEdge {
  source: string;
  target: string;
  condition?: string;
}

export interface WorkflowDef {
  name: string;
  description?: string;
  nodes: AgentNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowOut {
  id: UUID;
  name: string;
  description: string | null;
  nodes: Record<string, unknown>[];
  edges: Record<string, unknown>[];
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

// ---------- Knowledge Base ----------

export interface KnowledgeBaseCreate {
  name: string;
  description?: string;
  embedding_model?: string;
  chunk_size?: number;
  chunk_overlap?: number;
}

export interface KnowledgeBaseOut {
  id: UUID;
  name: string;
  description: string | null;
  embedding_model: string;
  chunk_size: number;
  chunk_overlap: number;
  created_at: string;
  updated_at: string;
}

export interface DocumentOut {
  id: UUID;
  knowledge_base_id: UUID;
  title: string;
  source_uri: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  chunk_count: number;
  status: "pending" | "processing" | "ready" | "failed";
  created_at: string;
  updated_at: string;
}

export interface SearchQuery {
  query: string;
  top_k?: number;
  score_threshold?: number;
}

export interface SearchResult {
  chunk_id: UUID;
  document_id: UUID;
  content: string;
  score: number;
  metadata: Record<string, unknown>;
}

export interface RAGQuery {
  question: string;
  top_k?: number;
}

// ---------- Models ----------

export type ModelProvider =
  | "openai"
  | "anthropic"
  | "local"
  | "azure_openai"
  | "custom";

export type RoutingStrategy =
  | "direct"
  | "round_robin"
  | "least_cost"
  | "latency";

export interface ModelConfigCreate {
  alias: string;
  provider?: ModelProvider;
  model_name: string;
  api_base?: string;
  api_key_env?: string;
  max_tokens?: number;
  temperature?: number;
  cost_per_1k_input: string; // Decimal as string
  cost_per_1k_output: string; // Decimal as string
  priority?: number;
  is_active?: boolean;
}

export interface ModelConfigUpdate {
  model_name?: string;
  api_base?: string;
  api_key_env?: string;
  max_tokens?: number;
  temperature?: number;
  cost_per_1k_input?: string;
  cost_per_1k_output?: string;
  is_active?: boolean;
  priority?: number;
}

export interface ModelConfigOut {
  id: UUID;
  alias: string;
  provider: string;
  model_name: string;
  api_base: string | null;
  api_key_env: string | null;
  max_tokens: number;
  temperature: number;
  cost_per_1k_input: string; // Decimal as string
  cost_per_1k_output: string; // Decimal as string
  is_active: boolean;
  priority: number;
  created_at: string;
  updated_at: string;
}

export interface ChatMessage {
  role: string;
  content: string;
}

export interface ChatRequest {
  messages: ChatMessage[];
  temperature?: number;
  max_tokens?: number;
  strategy?: RoutingStrategy;
}

export interface ChatResponse {
  content: string;
  model: string;
  alias: string;
  usage: Record<string, unknown>;
  cost: string; // Decimal as string
  fallback_used: boolean;
}

// ---------- Analytics ----------

export interface MessageOut {
  id: UUID;
  conversation_id: UUID;
  role: string;
  content: string;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number | null;
  model_alias: string | null;
  created_at: string;
}

export interface ConversationOut {
  id: UUID;
  user_id: UUID | null;
  agent_id: UUID | null;
  model_alias: string | null;
  title: string | null;
  total_tokens: number;
  total_cost: string; // Decimal as string
  created_at: string;
  updated_at: string;
  messages: MessageOut[];
}

export interface DashboardMetrics {
  total_conversations: number;
  total_messages: number;
  total_tokens: number;
  total_cost: string; // Decimal as string
  avg_messages_per_conversation: number;
  avg_latency_ms: number;
  active_models: Record<string, unknown>[];
  conversations_last_7d: Record<string, unknown>[];
}

// ---------- Evals ----------

export type JudgeType = "exact" | "contains" | "llm" | "semantic";

export type EvalStatus =
  | "pending"
  | "running"
  | "passed"
  | "failed"
  | "error";

export interface EvalCaseInput {
  name?: string;
  input: string;
  expected?: string;
  metadata?: Record<string, unknown>;
}

export interface EvalRuleInput {
  name: string;
  judge_type?: JudgeType;
  expected?: string;
  config?: Record<string, unknown>;
}

export interface EvalRunCreate {
  name: string;
  description?: string;
  rules?: EvalRuleInput[];
  cases: EvalCaseInput[];
  judge_type?: JudgeType;
}

export interface CaseResult {
  case_name: string | null;
  input: string;
  expected: string | null;
  actual: string | null;
  passed: boolean;
  score: number;
  reason: string | null;
}

export interface EvalRunOut {
  id: UUID;
  name: string;
  description: string | null;
  rules: Record<string, unknown>[];
  cases: Record<string, unknown>[];
  judge_type: string;
  status: EvalStatus;
  results: Record<string, unknown>[] | null;
  pass_count: number;
  fail_count: number;
  score: number | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}
