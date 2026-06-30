// Core types mirroring the backend OpenAPI 3.1 schemas.
// Normally generated via `npm run gen:api` (openapi-typescript).
// Hand-maintained here as a fallback so the UI is fully typed.

// ---------- Generic list wrapper ----------

export interface ListResponse<T> {
  items: T[];
  total: number;
}

// ---------- Prompts ----------

export interface PromptCreate {
  name: string;
  description?: string;
  content: string;
  variables?: string[];
}

export interface PromptUpdate {
  name?: string;
  description?: string;
}

export interface PromptVersionOut {
  id: number;
  version_num: number;
  content: string;
  variables: string[];
  created_at: string;
}

export interface PromptOut {
  id: number;
  name: string;
  description: string;
  current_version: PromptVersionOut | null;
  version_count: number;
  created_at: string;
  updated_at: string;
}

export interface PromptVersionCreate {
  content: string;
  variables?: string[];
}

export interface PromptDiff {
  additions: number;
  deletions: number;
  diff: string;
}

// ---------- Agents ----------

export interface AgentCreate {
  name: string;
  description?: string;
  system_prompt: string;
  model_alias: string;
  tools?: string[];
}

export interface AgentOut {
  id: number;
  name: string;
  description: string;
  system_prompt: string;
  model_alias: string;
  tools: string[];
  created_at: string;
}

export interface AgentExecuteRequest {
  input: Record<string, unknown>;
  stream?: boolean;
}

export interface ExecutionResult {
  trace_id: string;
  status: "success" | "failed" | "timeout";
  output: Record<string, unknown>;
  token_usage: Record<string, number>;
  latency_ms: number;
}

// ---------- Workflows ----------

export interface AgentNode {
  id: string;
  name: string;
  agent_id?: number;
  system_prompt?: string;
  model_config_id?: number;
  tools: string[];
  next_nodes: string[];
  condition?: string;
}

export interface WorkflowDef {
  name: string;
  nodes: AgentNode[];
  entry_node: string;
  max_rounds?: number;
}

export interface WorkflowOut {
  id: number;
  name: string;
  nodes: AgentNode[];
  entry_node: string;
  max_rounds: number;
  created_at: string;
}

export interface ExecutionTrace {
  trace_id: string;
  workflow_id: number;
  status: "running" | "success" | "failed" | "timeout";
  spans: Record<string, unknown>[];
  token_usage: Record<string, number>;
  latency_ms: number;
  created_at: string;
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
  id: number;
  name: string;
  description: string;
  embedding_model: string;
  chunk_size: number;
  chunk_overlap: number;
  doc_count: number;
  created_at: string;
}

export interface DocumentOut {
  id: number;
  kb_id: number;
  filename: string;
  file_size: number;
  chunk_count: number;
  status: "pending" | "processing" | "done" | "error";
  created_at: string;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  threshold?: number;
}

export interface SearchResult {
  chunk_id: number;
  doc_id: number;
  content: string;
  score: number;
  metadata: Record<string, unknown>;
}

export interface SearchResponse {
  results: SearchResult[];
}

// ---------- Models ----------

export type ProviderName = "openai" | "anthropic" | "azure" | "local";
export type RoutingStrategy = "direct" | "round_robin" | "least_cost" | "latency";

export interface ModelConfigCreate {
  alias: string;
  provider_name: ProviderName;
  model_id: string;
  temperature?: number;
  max_tokens?: number;
  cost_per_1k_input?: number;
  cost_per_1k_output?: number;
  routing_strategy?: RoutingStrategy;
  fallback_models?: string[];
  quota_daily?: number;
}

export interface ModelConfigOut {
  id: number;
  alias: string;
  provider_name: ProviderName;
  model_id: string;
  temperature: number;
  max_tokens: number;
  cost_per_1k_input: number;
  cost_per_1k_output: number;
  routing_strategy: RoutingStrategy;
  fallback_models: string[];
  quota_daily: number;
  enabled: boolean;
  created_at: string;
}

export interface Message {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
}

export interface ChatRequest {
  messages: Message[];
  temperature?: number;
  max_tokens?: number;
  stream?: boolean;
}

export interface ChatResponse {
  id: string;
  content: string;
  model: string;
  usage: Record<string, number>;
  latency_ms: number;
}

// ---------- Analytics ----------

export interface ConversationOut {
  id: number;
  session_id: string;
  user_id: number;
  agent_id: number | null;
  model_alias: string;
  message_count: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  latency_ms: number;
  quality_score: number | null;
  user_rating: number | null;
  created_at: string;
}

export interface DailyStat {
  date: string;
  conversations: number;
  tokens: number;
  cost_usd: number;
}

export interface DashboardMetrics {
  total_conversations: number;
  total_tokens: number;
  total_cost_usd: number;
  avg_latency_ms?: number;
  avg_quality_score?: number;
  model_distribution?: Record<string, number>;
  daily_stats?: DailyStat[];
}

// ---------- Evals ----------

export interface EvalRule {
  type: "exact_match" | "contains" | "json_schema" | "regex";
  target: string;
  expected: unknown;
  case_sensitive?: boolean;
}

export interface EvalJudge {
  criteria: string;
  model_alias?: string;
  scale?: number;
  threshold?: number;
}

export interface EvalRunCreate {
  prompt_version_id: number;
  model_alias: string;
  cases: string[];
  rules?: EvalRule[];
  judges?: EvalJudge[];
}

export interface EvalRunOut {
  id: string;
  prompt_version_id: number;
  model_alias: string;
  status: "pending" | "running" | "done" | "failed";
  pass_rate: number;
  avg_score: number;
  results: Record<string, unknown>[];
  created_at: string;
}
