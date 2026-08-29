export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };

export const DEFAULT_AGENT_API_BASE = '/api/v1';

export type AgentTask = 'build-library' | 'assess' | 'generate' | 'feedback';

export type ApiQueryValue =
  | string
  | number
  | boolean
  | readonly (string | number | boolean)[]
  | null
  | undefined;

export interface JsonRequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  query?: Record<string, ApiQueryValue>;
}

export interface StructuredApiErrorPayload {
  code?: string;
  error_code?: string;
  error_message?: string;
  message?: string;
  request_id?: string;
  detail?: unknown;
  details?: unknown;
  [key: string]: unknown;
}

export class AgentApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;
  readonly requestId: string | null;
  readonly payload: unknown;

  constructor(
    status: number,
    code: string,
    message: string,
    details: unknown = null,
    requestId: string | null = null,
    payload: unknown = null,
  ) {
    super(message);
    this.name = 'AgentApiError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
    this.payload = payload;
  }
}

export function getAgentApiBase(): string {
  const configured = typeof process !== 'undefined' ? process.env.NEXT_PUBLIC_AGENT_API_BASE?.trim() : undefined;
  const base = configured || DEFAULT_AGENT_API_BASE;
  return base.replace(/\/+$/, '') || '/';
}

function pathSegment(value: string | number): string {
  return encodeURIComponent(String(value));
}

function buildRequestUrl(path: string, query?: Record<string, ApiQueryValue>): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const base = getAgentApiBase();
  const url = base === '/' ? normalizedPath : `${base}${normalizedPath}`;

  if (!query) return url;

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value) params.append(key, String(item));
    } else {
      params.set(key, String(value));
    }
  }

  const encodedQuery = params.toString();
  if (!encodedQuery) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${encodedQuery}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

interface ParsedApiError {
  code: string;
  message: string;
  details: unknown;
  requestId: string | null;
}

function parseApiError(payload: unknown, status: number): ParsedApiError {
  const root = asRecord(payload);
  const detail = root?.detail;
  const detailRecord = asRecord(detail);
  const code =
    asString(root?.error_code) ??
    asString(root?.code) ??
    asString(detailRecord?.error_code) ??
    asString(detailRecord?.code) ??
    `HTTP_${status}`;
  const requestId = asString(root?.request_id) ?? asString(detailRecord?.request_id) ?? null;
  const details = detailRecord?.details ?? root?.details ?? detail ?? root?.error ?? payload;
  const message =
    asString(root?.message) ??
    asString(root?.error_message) ??
    asString(detailRecord?.message) ??
    asString(detail) ??
    code;

  return { code, message, details, requestId };
}

export async function requestJson<T>(path: string, options: JsonRequestOptions = {}): Promise<T> {
  const { body, query, headers, ...requestInit } = options;
  const requestHeaders = new Headers(headers);
  requestHeaders.set('Accept', 'application/json');
  if (body !== undefined && !requestHeaders.has('Content-Type')) {
    requestHeaders.set('Content-Type', 'application/json');
  }

  let response: Response;
  try {
    response = await fetch(buildRequestUrl(path, query), {
      ...requestInit,
      headers: requestHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof AgentApiError) throw error;
    const message = error instanceof Error ? error.message : 'Network request failed.';
    throw new AgentApiError(0, 'NETWORK_ERROR', message, error);
  }

  const rawText = await response.text();
  let payload: unknown = null;
  if (rawText.trim()) {
    try {
      payload = JSON.parse(rawText) as unknown;
    } catch {
      payload = rawText;
    }
  }

  if (!response.ok) {
    const parsed = parseApiError(payload, response.status);
    throw new AgentApiError(
      response.status,
      parsed.code,
      parsed.message,
      parsed.details,
      parsed.requestId,
      payload,
    );
  }

  if (!rawText.trim()) return undefined as T;
  if (typeof payload === 'string' && payload === rawText) {
    throw new AgentApiError(
      response.status,
      'INVALID_JSON_RESPONSE',
      'Agent API returned invalid JSON.',
      { raw: rawText },
      null,
      payload,
    );
  }
  return payload as T;
}

function stableSerialize(value: unknown, seen = new WeakSet<object>()): string {
  if (value === null) return 'null';
  if (value === undefined || typeof value === 'function' || typeof value === 'symbol') return 'null';
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') return Number.isFinite(value) ? JSON.stringify(value) : 'null';
  if (value instanceof Date) return JSON.stringify(value.toISOString());

  if (Array.isArray(value)) {
    if (seen.has(value)) return '"[Circular]"';
    seen.add(value);
    const serialized = `[${value.map((item) => stableSerialize(item, seen)).join(',')}]`;
    seen.delete(value);
    return serialized;
  }

  if (typeof value === 'object') {
    if (seen.has(value)) return '"[Circular]"';
    seen.add(value);
    const object = value as Record<string, unknown>;
    const entries = Object.keys(object)
      .filter((key) => {
        const item = object[key];
        return item !== undefined && typeof item !== 'function' && typeof item !== 'symbol';
      })
      .sort()
      .map((key) => `${JSON.stringify(key)}:${stableSerialize(object[key], seen)}`);
    seen.delete(value);
    return `{${entries.join(',')}}`;
  }

  return JSON.stringify(String(value));
}

function hash32(value: string, seed: number): number {
  let hash = seed >>> 0;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function hashHex(value: string): string {
  const first = hash32(value, 2166136261).toString(16).padStart(8, '0');
  const second = hash32(value, 3735928559).toString(16).padStart(8, '0');
  return `${first}${second}`;
}

export function createIdempotencyKey(scope: string, input?: unknown): string {
  return `web-${hashHex(stableSerialize({ scope, input: input ?? null }))}`;
}

export interface MemorySync {
  status: 'succeeded' | 'failed' | string;
  profile_memory_id?: number;
  decision_memory_ids?: number[];
  verified_count?: number;
  decision_candidate_count?: number;
  error_code?: string;
}

export interface BloggerProfile {
  name: string;
  platform: string;
  content_types: string[];
  style: string;
  follower_band: string;
  monetization_types: string[];
  routes?: string | null;
  viral_topic?: string | null;
  frequency?: string | null;
}

export interface Blogger extends BloggerProfile {
  id: number;
  profile_state: string;
  deleted_at?: string | null;
  memory_sync?: MemorySync;
}

export type CreateBloggerInput = BloggerProfile;
export type UpdateBloggerInput = Partial<BloggerProfile> & { suit_type?: string | null };

export type AssetLibrary = 'knowledge' | 'material' | 'algorithm';

export interface AssetSource {
  title: string;
  url: string | null;
  publisher: string | null;
}

export interface Asset {
  id: number;
  lib_type: AssetLibrary;
  category: string;
  title: string;
  content: string;
  tags: string[];
  source_type: string;
  credibility: number;
  origin?: string;
  manual_locked?: boolean;
  decision_id?: number | null;
  similarity: number | null;
  sources: AssetSource[];
  created_at?: string;
  updated_at?: string;
}

export interface CreateAssetInput {
  lib_type: AssetLibrary;
  category: string;
  title: string;
  content: string;
  tags: string[];
  source_type: string;
  source_url?: string | null;
  source_title?: string | null;
  publisher?: string | null;
  verified_at?: string | null;
  credibility: number;
  idempotency_key?: string;
}

export type UpdateAssetInput = Partial<Omit<CreateAssetInput, 'idempotency_key'>>;

export interface AssetListParams {
  q?: string;
  lib_type?: AssetLibrary;
  category?: string;
  tags?: string[];
  source_type?: string;
  source?: string;
  min_credibility?: number;
  max_credibility?: number;
  page?: number;
  page_size?: number;
}

export type BuildRunStatus = 'pending' | 'running' | 'succeeded' | 'failed' | string;

export interface BuildRun {
  id: number;
  status: BuildRunStatus;
  output_summary: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  memory_sync?: MemorySync;
}

export interface BuildRunInput {
  idempotency_key?: string;
}

export type AssessmentStatus = 'pending' | 'running' | 'succeeded' | 'failed';

export interface AssessmentEvidenceRef {
  evidence_type: string;
  asset_id?: number | null;
  source_document_id?: number | null;
  from_asset_id?: number | null;
  to_asset_id?: number | null;
  claim?: string | null;
  [key: string]: unknown;
}

export interface AssessmentEvidence {
  id: number;
  assessment_id: number;
  indicator_id?: number;
  evidence_type: string;
  asset_id: number | null;
  source_document_id: number | null;
  claim: string;
  created_at: string;
}

export interface AssessmentIndicator {
  id: number;
  assessment_id: number;
  ordinal: number;
  name: string;
  meaning: string;
  score_logic: string;
  business_meaning: string;
  weight: number;
  weight_reason: string;
  score: number;
  reason: string;
  evidence: AssessmentEvidenceRef[];
  evidences: AssessmentEvidence[];
  created_at: string;
}

export interface AssessmentSummary {
  id: number;
  blogger_id: number;
  task_id: string | null;
  status: AssessmentStatus;
  idempotency_key: string;
  snapshot_hash: string | null;
  summary: string | null;
  overall_score: number | null;
  decision_id: number | null;
  prompt_version: string;
  model_name: string;
  error_code: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface Assessment extends AssessmentSummary {
  input_snapshot: JsonValue | null;
  library_analysis: JsonValue | null;
  feature_readiness: JsonValue | null;
  suggestions: JsonValue | null;
  indicators: AssessmentIndicator[];
  evidence: AssessmentEvidence[];
}

export interface CreateAssessmentInput {
  idempotency_key?: string;
  task_id?: string | null;
  prompt_version?: string;
}

export interface AssessmentListParams {
  page?: number;
  page_size?: number;
}

export interface OutputAssetReference {
  id: number;
  output_id: number;
  asset_id: number;
  usage_type: string;
  claim: string;
}

export interface OutputPlaceReference {
  id: number;
  output_id: number;
  place_id: number;
  role: string;
  sequence: number;
  claim: string;
}

export type OutputKind = 'script' | 'storyboard' | 'route_rec';
export type OutputStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'draft' | 'deleted';

export interface OutputSummary {
  id: number;
  blogger_id: number;
  task_id: string | null;
  idempotency_key: string | null;
  type: OutputKind;
  category: string;
  title: string;
  status: OutputStatus;
  assessment_id: number | null;
  parent_output_id: number | null;
  version: number;
  manual_locked: boolean;
  decision_id: number | null;
  prompt_version: string;
  model_name: string;
  error_code: string | null;
  error_message: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ScriptOutput extends OutputSummary {
  type: 'script';
  content_json: JsonValue | string;
  assets: OutputAssetReference[];
  places: OutputPlaceReference[];
}

export interface GenerateScriptInput {
  assessment_id: number;
  category?: string;
  title?: string;
  topic?: string;
  user_instruction?: string;
  platform?: string;
  content_type?: string;
  place_ids?: number[];
  idempotency_key?: string;
}

export interface ScriptOutputListParams {
  status?: OutputStatus;
  page?: number;
  page_size?: number;
}

export async function listBloggers(): Promise<Blogger[]> {
  return requestJson<Blogger[]>('/bloggers', { method: 'GET' });
}

export async function getBlogger(bloggerId: number): Promise<Blogger> {
  return requestJson<Blogger>(`/bloggers/${pathSegment(bloggerId)}`, { method: 'GET' });
}

export async function createBlogger(input: CreateBloggerInput): Promise<Blogger> {
  return requestJson<Blogger>('/bloggers', { method: 'POST', body: input });
}

export async function updateBlogger(bloggerId: number, input: UpdateBloggerInput): Promise<Blogger> {
  return requestJson<Blogger>(`/bloggers/${pathSegment(bloggerId)}`, { method: 'PUT', body: input });
}

export async function deleteBlogger(bloggerId: number): Promise<{ deleted: boolean; blogger_id: number; deleted_at: string | null }> {
  return requestJson(`/bloggers/${pathSegment(bloggerId)}`, { method: 'DELETE' });
}

export async function listAssets(bloggerId: number, params: AssetListParams = {}): Promise<Asset[]> {
  return requestJson<Asset[]>(`/bloggers/${pathSegment(bloggerId)}/assets`, {
    method: 'GET',
    query: { ...params },
  });
}

export async function getAsset(bloggerId: number, assetId: number): Promise<Asset> {
  return requestJson<Asset>(
    `/bloggers/${pathSegment(bloggerId)}/assets/${pathSegment(assetId)}`,
    { method: 'GET' },
  );
}

export async function createAsset(bloggerId: number, input: CreateAssetInput): Promise<Asset> {
  const body = {
    ...input,
    idempotency_key:
      input.idempotency_key ?? createIdempotencyKey(`asset:create:${bloggerId}`, input),
  };
  return requestJson<Asset>(`/bloggers/${pathSegment(bloggerId)}/assets`, {
    method: 'POST',
    body,
  });
}

export async function updateAsset(
  bloggerId: number,
  assetId: number,
  input: UpdateAssetInput,
): Promise<Asset> {
  return requestJson<Asset>(
    `/bloggers/${pathSegment(bloggerId)}/assets/${pathSegment(assetId)}`,
    { method: 'PUT', body: input },
  );
}

export async function deleteAsset(
  bloggerId: number,
  assetId: number,
): Promise<{ deleted: boolean; asset_id: number; deleted_at: string | null }> {
  return requestJson(`/bloggers/${pathSegment(bloggerId)}/assets/${pathSegment(assetId)}`, {
    method: 'DELETE',
  });
}

export async function createBuildRun(bloggerId: number, input: BuildRunInput = {}): Promise<BuildRun> {
  const body = {
    idempotency_key:
      input.idempotency_key ?? createIdempotencyKey(`build-run:create:${bloggerId}`, input),
  };
  return requestJson<BuildRun>(`/bloggers/${pathSegment(bloggerId)}/build-runs`, {
    method: 'POST',
    body,
  });
}

export async function createAssessment(
  bloggerId: number,
  input: CreateAssessmentInput = {},
): Promise<Assessment> {
  const body = {
    ...input,
    idempotency_key:
      input.idempotency_key ?? createIdempotencyKey(`assessment:create:${bloggerId}`, input),
  };
  return requestJson<Assessment>(`/bloggers/${pathSegment(bloggerId)}/assessments`, {
    method: 'POST',
    body,
  });
}

export async function listAssessments(
  bloggerId: number,
  params: AssessmentListParams = {},
): Promise<AssessmentSummary[]> {
  return requestJson<AssessmentSummary[]>(`/bloggers/${pathSegment(bloggerId)}/assessments`, {
    method: 'GET',
    query: { ...params },
  });
}

export async function getAssessment(bloggerId: number, assessmentId: number): Promise<Assessment> {
  return requestJson<Assessment>(
    `/bloggers/${pathSegment(bloggerId)}/assessments/${pathSegment(assessmentId)}`,
    { method: 'GET' },
  );
}

export async function retryAssessment(bloggerId: number, assessmentId: number): Promise<Assessment> {
  return requestJson<Assessment>(
    `/bloggers/${pathSegment(bloggerId)}/assessments/${pathSegment(assessmentId)}/retry`,
    { method: 'POST' },
  );
}

export async function listAssessmentEvidence(
  bloggerId: number,
  assessmentId: number,
): Promise<AssessmentEvidence[]> {
  return requestJson<AssessmentEvidence[]>(
    `/bloggers/${pathSegment(bloggerId)}/assessments/${pathSegment(assessmentId)}/evidence`,
    { method: 'GET' },
  );
}

export async function generateScript(
  bloggerId: number,
  input: GenerateScriptInput,
): Promise<ScriptOutput> {
  const body = {
    ...input,
    idempotency_key:
      input.idempotency_key ?? createIdempotencyKey(`script-output:create:${bloggerId}`, input),
  };
  return requestJson<ScriptOutput>(
    `/bloggers/${pathSegment(bloggerId)}/outputs/generate/script`,
    { method: 'POST', body },
  );
}

export async function listScriptOutputs(
  bloggerId: number,
  params: ScriptOutputListParams = {},
): Promise<OutputSummary[]> {
  return requestJson<OutputSummary[]>(`/bloggers/${pathSegment(bloggerId)}/outputs`, {
    method: 'GET',
    query: { ...params, type: 'script' },
  });
}

export async function getScriptOutput(bloggerId: number, outputId: number): Promise<ScriptOutput> {
  return requestJson<ScriptOutput>(
    `/bloggers/${pathSegment(bloggerId)}/outputs/${pathSegment(outputId)}`,
    { method: 'GET' },
  );
}

export async function retryScriptOutput(bloggerId: number, outputId: number): Promise<ScriptOutput> {
  return requestJson<ScriptOutput>(
    `/bloggers/${pathSegment(bloggerId)}/outputs/${pathSegment(outputId)}/retry`,
    { method: 'POST' },
  );
}

export interface ScriptRevisionInput {
  title?: string;
  content_json: Record<string, unknown>;
}

export async function reviseScriptOutput(
  bloggerId: number,
  outputId: number,
  input: ScriptRevisionInput,
): Promise<ScriptOutput> {
  return requestJson<ScriptOutput>(
    `/bloggers/${pathSegment(bloggerId)}/outputs/${pathSegment(outputId)}/revisions`,
    { method: 'POST', body: input },
  );
}

export interface ChatTurn {
  role: 'user' | 'assistant';
  content: string;
}

export interface ChatRequestInput {
  message: string;
  conversation?: ChatTurn[];
  request_id?: string;
}

export interface ChatReply {
  reply: string;
  request_id?: string;
  model?: string;
}

export async function chatWithAgent(bloggerId: number, input: ChatRequestInput): Promise<ChatReply> {
  return requestJson<ChatReply>(
    `/bloggers/${pathSegment(bloggerId)}/chat`,
    { method: 'POST', body: input },
  );
}

/** 兼容当前页面调用面；真实页面应优先使用上面的领域方法。 */
export async function runAgentTask<T>(
  task: AgentTask,
  payload: unknown,
  _fallback?: T,
): Promise<T> {
  void _fallback;
  return requestJson<T>(`/agent/${task}`, { method: 'POST', body: payload });
}

export const agentApi = {
  requestJson,
  getAgentApiBase,
  createIdempotencyKey,
  listBloggers,
  getBlogger,
  createBlogger,
  updateBlogger,
  deleteBlogger,
  listAssets,
  getAsset,
  createAsset,
  updateAsset,
  deleteAsset,
  createBuildRun,
  createAssessment,
  listAssessments,
  getAssessment,
  retryAssessment,
  listAssessmentEvidence,
  generateScript,
  listScriptOutputs,
  getScriptOutput,
  retryScriptOutput,
  reviseScriptOutput,
  chatWithAgent,
  runAgentTask,
};
